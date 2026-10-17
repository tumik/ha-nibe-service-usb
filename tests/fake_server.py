"""A pure-asyncio fake nibe-service-usb gateway (protocol v1) for tests.

Implements the hello / auth handshake byte-exactly (proofs are computed here
with ``hmac``/``hashlib`` on purpose, independent of ``client.py``) plus the
commands Home Assistant uses. Every request is recorded in ``FakeServer.calls``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

TEST_KEY = bytes(range(32))
SERVER_ID = "6f1c2a9e-4b7d-4c1e-9a3f-0d5e8b7a1c42"
DEFAULT_PORT = 9477


def _meta(
    name: str,
    kind: str,
    unit: str | None = None,
    precision: int | None = None,
    tier: str = "core",
    device_class: str | None = None,
    state_class: str | None = None,
    description: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "unit": unit,
        "precision": precision,
        "tier": tier,
        "device_class": device_class,
        "state_class": state_class,
        "description": description,
    }
    if options is not None:
        meta["options"] = list(options)
    return meta


def default_snapshot() -> dict[str, dict[str, Any]]:
    """name -> metadata + ``value`` (``ts`` is added when serving)."""
    now = time.time()

    def entry(meta: dict[str, Any], value: Any) -> dict[str, Any]:
        return {**meta, "value": value, "ts": now}

    return {
        "bt1_outdoor": entry(
            _meta(
                "Outdoor temperature (BT1)",
                "temperature",
                "°C",
                1,
                "core",
                "temperature",
                "measurement",
                "Outdoor air temperature",
            ),
            4.7,
        ),
        "degree_minutes": entry(
            _meta("Degree minutes", "number", "DM", 1, "core", None, "measurement", "Current degree minutes"),
            -12.0,
        ),
        "hw_comfort": entry(
            _meta(
                "Hot water comfort mode",
                "enum",
                None,
                None,
                "core",
                "enum",
                None,
                "Hot water comfort mode",
                options=["eco", "normal", "lux"],
            ),
            "normal",
        ),
        "op_priority": entry(
            _meta(
                "Operating priority",
                "enum",
                None,
                None,
                "core",
                "enum",
                None,
                "What the pump is doing now",
                options=["off", "hot_water", "heating"],
            ),
            "heating",
        ),
        "compressor_running": entry(
            _meta("Compressor running", "binary", None, None, "core", "running", None, None), True
        ),
        "any_alarm": entry(_meta("Any alarm", "binary", None, None, "core", "problem", None, None), False),
        "compressor_inhibit": entry(
            _meta(
                "Compressor inhibit time",
                "duration",
                "min",
                0,
                "core",
                "duration",
                "measurement",
                "Remaining compressor start inhibit",
            ),
            0,
        ),
        "comp_hours": entry(
            _meta(
                "Compressor operating time",
                "duration",
                "h",
                0,
                "diagnostic",
                "duration",
                "total_increasing",
                "Total compressor hours",
            ),
            1234,
        ),
        "active_alarms": entry(
            _meta("Active alarms", "alarm_list", None, None, "core", None, None, "List of active alarms"), []
        ),
        "set_start_comp": entry(
            _meta(
                "Start compressor DM", "number", "DM", 0, "settings", None, None, "Compressor start threshold"
            ),
            -60,
        ),
        "uptime": entry(
            _meta("Gateway uptime", "duration", "s", 0, "identity", "duration", None, "Pump uptime"), 100000
        ),
    }


def default_status() -> dict[str, Any]:
    now = time.time()
    return {
        "version": "0.1.0",
        "uptime": 100000.0,
        "pump": {"online": True, "port": "/dev/ttyUSB0", "last_ok": now, "errors": 0},
        "heating": {
            "mode": "auto",
            "active": False,
            "suspended_reason": None,
            "last_push": None,
            "next_push": None,
            "target_dm": None,
            "current_dm": -15,
        },
        "hot_water": {
            "mode": "normal",
            "effective_mode": "normal",
            "reason": "comfort",
            "periodic_increase": {
                "running": False,
                "remaining_seconds": 0,
                "hold": False,
                "enabled": True,
            },
        },
        "watchdog": {"ha_alive": True, "last_heartbeat": now, "timeout": 300, "reverted_at": None},
        "clients": 1,
        "identity": {
            "product": "F1226-6 R",
            "software_version": "7994R7",
            "base_version": 3,
            "product_code": "065 xxx",
            "clock_state": "unset",
            "clock_drift": 0.0,
            "uptime": 100000,
            "last_restart": 1760000000.0,
            "eeprom_store_calls": 10,
            "eeprom_write_failures": 0,
            "eeprom_write_failed": False,
            "last_health_check": 1760000100.0,
        },
    }


def _hmac_hex(key: bytes, first_b64: str, second_b64: str) -> str:
    return hmac.new(
        key, base64.b64decode(first_b64) + base64.b64decode(second_b64), hashlib.sha256
    ).hexdigest()


class Session:
    """One client connection."""

    def __init__(self, server: FakeServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        self.authenticated = False
        self.subscribed = False
        self.sensors_filter: list[str] = ["*"]
        self.client_name: str | None = None
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        async with self._send_lock:
            try:
                self.writer.write(json.dumps(msg).encode("utf-8") + b"\n")
                await self.writer.drain()
            except (ConnectionError, OSError):
                pass

    async def send_event(self, event: str, data: dict[str, Any]) -> None:
        await self.send({"event": event, "ts": time.time(), "data": data})

    async def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)

    async def _ok(self, req_id: Any, data: dict[str, Any]) -> None:
        if self.server.delay_results:
            await asyncio.sleep(self.server.delay_results)
        await self.send({"id": req_id, "ok": True, "data": data})

    async def _err(self, req_id: Any, code: str, message: str = "") -> None:
        if self.server.delay_results:
            await asyncio.sleep(self.server.delay_results)
        await self.send({"id": req_id, "ok": False, "error": {"code": code, "message": message}})

    async def run(self) -> None:
        srv = self.server
        await self.send_event(
            "hello",
            {
                "server": "nibe-service-usb",
                "version": "0.1.0",
                "proto": srv.proto,
                "server_id": srv.server_id,
                "nonce": self.nonce,
                "auth": "hmac-sha256",
            },
        )
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        raise ValueError("not an object")
                except ValueError:
                    await self.send(
                        {"id": None, "ok": False, "error": {"code": "invalid_params", "message": "bad json"}}
                    )
                    continue
                if not await self._handle(msg):
                    return
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            return
        finally:
            srv._sessions.discard(self)
            await self.close()

    async def _handle(self, msg: dict[str, Any]) -> bool:
        """Handle one request; returns False when the connection must be closed."""
        req_id = msg.get("id")
        cmd = msg.get("cmd")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            await self._err(req_id, "invalid_params", "params must be an object")
            return True
        self.server.calls.append((cmd, params))

        if not self.authenticated:
            if cmd != "auth":
                await self._err(req_id, "auth_required", "authenticate first")
                return False
            return await self._auth(req_id, params)

        srv = self.server
        if cmd == "auth":
            await self._err(req_id, "invalid_params", "already authenticated")
        elif cmd == "ping":
            if srv.hang_ping:
                return True
            await self._ok(req_id, {"uptime": 12345.6, "pump_online": True, "ha_alive": True})
        elif cmd == "subscribe":
            wanted = params.get("sensors", ["*"])
            self.subscribed = True
            self.sensors_filter = wanted
            await self._ok(
                req_id,
                {
                    "poll_interval": 600,
                    "sensors": {
                        name: copy.deepcopy(entry)
                        for name, entry in srv.snapshot.items()
                        if wanted == ["*"] or name in wanted
                    },
                },
            )
        elif cmd == "unsubscribe":
            self.subscribed = False
            await self._ok(req_id, {})
        elif cmd == "list_sensors":
            await self._ok(
                req_id,
                {
                    "sensors": {
                        name: {k: v for k, v in entry.items() if k not in ("value", "ts")}
                        for name, entry in srv.snapshot.items()
                    }
                },
            )
        elif cmd == "get_status":
            await self._ok(req_id, copy.deepcopy(srv.status))
        elif cmd == "set_hot_water_mode":
            mode = params.get("mode")
            if mode not in ("eco", "normal", "lux"):
                await self._err(req_id, "invalid_params", "mode must be one of eco, normal, lux")
            else:
                if "hw_comfort" in srv.snapshot:
                    srv.snapshot["hw_comfort"]["value"] = mode
                    srv.snapshot["hw_comfort"]["ts"] = time.time()
                srv.status["hot_water"]["mode"] = mode
                await self._ok(req_id, {"mode": mode})
        elif cmd == "set_heating_request":
            mode = params.get("mode")
            if mode not in ("auto", "enable", "disable"):
                await self._err(req_id, "invalid_params", "mode must be one of auto, enable, disable")
            else:
                srv.status["heating"]["mode"] = mode
                srv.status["heating"]["active"] = mode != "auto"
                await self._ok(
                    req_id,
                    {
                        "mode": mode,
                        "active": mode != "auto",
                        "suspended_reason": None,
                        "current_dm": -15,
                        "written_dm": -1,
                    },
                )
        elif cmd == "trigger_hot_water_boost":
            if srv.boost_error is not None:
                await self._err(req_id, *srv.boost_error)
            else:
                await self._ok(
                    req_id,
                    {"started": True, "remaining_seconds": 18000, "enabled_periodic_increase": False},
                )
        elif cmd == "refresh":
            await self._ok(req_id, {"queued": True})
        else:
            await self._err(req_id, "unknown_command", f"unknown command {cmd!r}")
        return True

    async def _auth(self, req_id: Any, params: dict[str, Any]) -> bool:
        srv = self.server
        client_nonce = params.get("client_nonce")
        proof = params.get("proof")
        ok = False
        if not srv.fail_auth and params.get("proto") == 1 and isinstance(client_nonce, str):
            try:
                expected = _hmac_hex(srv.key, self.nonce, client_nonce)
            except ValueError:
                expected = None
            ok = (
                expected is not None
                and isinstance(proof, str)
                and hmac.compare_digest(proof.lower().encode(), expected.encode())
            )
        if not ok:
            await self._err(req_id, "auth_failed", "bad proof or unsupported proto")
            return False
        self.authenticated = True
        self.client_name = params.get("client")
        server_proof = _hmac_hex(srv.key, client_nonce, self.nonce)
        if srv.wrong_server_proof:
            server_proof = "00" * 32
        await self.send({"id": req_id, "ok": True, "data": {"server_proof": server_proof}})
        return True


class FakeServer:
    """Fake gateway; use as ``async with FakeServer() as srv`` or via the pytest fixture."""

    def __init__(
        self,
        *,
        key: bytes = TEST_KEY,
        server_id: str = SERVER_ID,
        fail_auth: bool = False,
        wrong_server_proof: bool = False,
        proto: int = 1,
        delay_results: float = 0.0,
        hang_ping: bool = False,
        boost_error: tuple[str, str] | None = None,
    ) -> None:
        self.key = key
        self.server_id = server_id
        self.fail_auth = fail_auth
        self.wrong_server_proof = wrong_server_proof
        self.proto = proto
        self.delay_results = delay_results
        self.hang_ping = hang_ping
        self.boost_error = boost_error
        self.snapshot: dict[str, dict[str, Any]] = default_snapshot()
        self.status: dict[str, Any] = default_status()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.port: int = 0
        self._server: asyncio.Server | None = None
        self._sessions: set[Session] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> FakeServer:
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        await self.drop_connections()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None

    async def __aenter__(self) -> FakeServer:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = Session(self, reader, writer)
        self._sessions.add(session)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        try:
            await session.run()
        except asyncio.CancelledError:
            self._sessions.discard(session)
            with contextlib.suppress(Exception):
                writer.close()
            raise

    # ------------------------------------------------------------ test hooks

    @property
    def connections(self) -> list[Session]:
        """Live sessions (authenticated or not)."""
        return [s for s in self._sessions if not s.writer.is_closing()]

    @property
    def authenticated_connections(self) -> list[Session]:
        return [s for s in self.connections if s.authenticated]

    def calls_for(self, cmd: str) -> list[dict[str, Any]]:
        return [p for c, p in self.calls if c == cmd]

    async def push_sensor_update(self, values: dict[str, Any]) -> None:
        now = time.time()
        for name, value in values.items():
            if name in self.snapshot:
                self.snapshot[name]["value"] = value
                self.snapshot[name]["ts"] = now
        for session in list(self.authenticated_connections):
            await session.send_event("sensor_update", {"values": dict(values)})

    async def push_status(self, status: dict[str, Any] | None = None) -> None:
        if status is not None:
            self.status = copy.deepcopy(status)
        for session in list(self.authenticated_connections):
            await session.send_event("status", copy.deepcopy(self.status))

    async def push_event(self, event: str, data: dict[str, Any]) -> None:
        for session in list(self.authenticated_connections):
            await session.send_event(event, data)

    async def drop_connections(self) -> None:
        for session in list(self._sessions):
            await session.close()

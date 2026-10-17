"""Pure asyncio client for the nibe-service-usb TCP protocol (proto 1).

This module has no Home Assistant dependency so it can be tested on its own.
Wire format: JSON Lines over a persistent TCP connection, see the server's
``docs/protocol.md``. Authentication is a mutual HMAC-SHA256 challenge /
response over the raw nonces (``docs/security.md``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import itertools
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

PROTO = 1
DEFAULT_PORT = 9477
KEY_BYTES = 32
NONCE_BYTES = 32
MAX_LINE = 1024 * 1024  # the server sends at most 64 KiB per line; be generous
CONNECT_TIMEOUT = 10.0
REQUEST_TIMEOUT = 30.0
BOOST_TIMEOUT = 60.0  # trigger_hot_water_boost takes 5-20 s on the pump

HOT_WATER_MODES = ("eco", "normal", "lux")
HEATING_MODES = ("auto", "enable", "disable")

EventCallback = Callable[[str, dict[str, Any]], None]
DisconnectCallback = Callable[[Exception | None], None]


# ------------------------------------------------------------------ errors


class NibeError(Exception):
    """Base class for all client errors."""


class ConnectionFailed(NibeError):
    """TCP connection could not be established or the hello was not received."""


class ConnectionLost(NibeError):
    """The connection dropped (or timed out) while a request was in flight."""


class AuthenticationError(NibeError):
    """The server rejected our proof, or its own proof did not verify."""


class ProtocolError(NibeError):
    """The server speaks a protocol version or shape we do not understand."""


class CommandError(NibeError):
    """The server answered a request with ``ok: false``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ key helpers


def parse_key(text: str) -> bytes:
    """Decode a key given as 64 hex characters or as dash-grouped base32.

    Mirrors ``keystore.parse_key`` on the server so both notations printed by
    ``nibe-service-usb show-key`` are accepted.
    """
    cleaned = text.strip()
    try:
        if len(cleaned) == KEY_BYTES * 2:
            key = bytes.fromhex(cleaned)
        else:
            b32 = cleaned.replace("-", "").replace(" ", "").upper()
            b32 += "=" * (-len(b32) % 8)
            key = base64.b32decode(b32)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("not a valid key") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    return key


def _decode_nonce(nonce_b64: str) -> bytes:
    if not isinstance(nonce_b64, str):
        raise ValueError("nonce must be a string")
    try:
        return base64.b64decode(nonce_b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("invalid base64 nonce") from err


def compute_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str) -> str:
    """Client proof: HMAC-SHA256 over ``server_nonce || client_nonce`` (raw bytes), hex."""
    data = _decode_nonce(server_nonce_b64) + _decode_nonce(client_nonce_b64)
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def compute_server_proof(key: bytes, server_nonce_b64: str, client_nonce_b64: str) -> str:
    """Server proof: HMAC-SHA256 over ``client_nonce || server_nonce`` (raw bytes), hex."""
    data = _decode_nonce(client_nonce_b64) + _decode_nonce(server_nonce_b64)
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def make_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(NONCE_BYTES)).decode("ascii")


# ------------------------------------------------------------------ client


@dataclass(frozen=True)
class ServerInfo:
    """What the server told us in ``hello``."""

    server_id: str
    version: str
    proto: int
    name: str


class NibeServiceUsbClient:
    """One persistent, authenticated connection to a gateway.

    Usage: ``await connect()``, then ``await ping()`` etc. Events arrive via
    ``on_event(name, data)``; ``on_disconnect(exc)`` fires once when an
    established connection ends for any reason. The object is single-use:
    create a new client for every (re)connect.
    """

    def __init__(
        self,
        host: str,
        port: int,
        key: bytes,
        *,
        client_name: str = "nibe-service-usb-client/0",
        on_event: EventCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self._key = key
        self._client_name = client_name
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._closed = asyncio.Event()
        self._established = False
        self._close_reason: Exception | None = None
        self.server: ServerInfo | None = None

    # ---------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._established and not self._closed.is_set()

    async def connect(self) -> ServerInfo:
        """Open the connection, read ``hello`` and authenticate (mutually)."""
        if self._writer is not None:
            raise RuntimeError("client is single-use; create a new one to reconnect")
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE), self._connect_timeout
            )
        except (TimeoutError, OSError) as err:
            raise ConnectionFailed(f"cannot connect to {self.host}:{self.port}: {err}") from err

        try:
            hello = await asyncio.wait_for(self._read_message(), self._connect_timeout)
        except (TimeoutError, OSError, ValueError) as err:
            await self._abort()
            raise ConnectionFailed(f"no hello from {self.host}:{self.port}: {err}") from err
        if hello is None or hello.get("event") != "hello" or not isinstance(hello.get("data"), dict):
            await self._abort()
            raise ProtocolError(f"unexpected first message: {hello!r}")
        data = hello["data"]
        if data.get("proto") != PROTO:
            await self._abort()
            raise ProtocolError(f"unsupported protocol version {data.get('proto')!r} (want {PROTO})")
        server_nonce = data.get("nonce")
        server_id = data.get("server_id")
        if not isinstance(server_nonce, str) or not isinstance(server_id, str):
            await self._abort()
            raise ProtocolError("hello without nonce/server_id")
        self.server = ServerInfo(
            server_id=server_id,
            version=str(data.get("version", "")),
            proto=PROTO,
            name=str(data.get("server", "")),
        )

        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop(), name="nibe-usb-reader")
        client_nonce = make_nonce()
        try:
            result = await self._request(
                "auth",
                {
                    "proto": PROTO,
                    "client": self._client_name,
                    "client_nonce": client_nonce,
                    "proof": compute_proof(self._key, server_nonce, client_nonce),
                },
                timeout=self._connect_timeout,
            )
        except CommandError as err:
            await self._abort()
            if err.code == "auth_failed":
                raise AuthenticationError("server rejected the key") from err
            raise
        except ConnectionLost as err:
            await self._abort()
            raise ConnectionFailed(f"connection lost during handshake: {err}") from err
        except asyncio.CancelledError:
            # Never leave the reader task and socket behind when the caller gives up.
            await self._abort()
            raise
        expected = compute_server_proof(self._key, server_nonce, client_nonce)
        proof = result.get("server_proof")
        if not isinstance(proof, str) or not hmac.compare_digest(proof.lower(), expected):
            await self._abort()
            raise AuthenticationError("server proof mismatch; not the gateway this key belongs to")
        self._established = True
        return self.server

    async def close(self) -> None:
        """Close the connection; idempotent."""
        await self._abort()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _abort(self, reason: Exception | None = None) -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        if self._writer is not None:
            self._writer.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionLost(str(reason) if reason else "connection closed"))
        self._pending.clear()
        task = self._reader_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._writer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        if self._established and self._on_disconnect is not None:
            self._established = False
            try:
                self._on_disconnect(reason)
            except Exception:  # noqa: BLE001 - never let a callback kill the client
                _LOGGER.exception("on_disconnect callback failed")
        self._established = False

    # ---------------------------------------------------------- I/O

    async def _read_message(self) -> dict[str, Any] | None:
        assert self._reader is not None
        line = await self._reader.readline()
        if not line:
            return None
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("message is not an object")
        return msg

    async def _read_loop(self) -> None:
        reason: Exception | None = None
        try:
            while True:
                try:
                    msg = await self._read_message()
                except ValueError as err:
                    _LOGGER.warning("Ignoring malformed line from %s:%s: %s", self.host, self.port, err)
                    continue
                if msg is None:
                    break
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as err:
            reason = err
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Reader failed")
            reason = err
        await self._abort(reason)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "ok" in msg:
            fut = self._pending.pop(msg.get("id"), None)  # type: ignore[arg-type]
            if fut is None:
                if msg.get("id") is None:
                    _LOGGER.warning("Server reported a malformed request: %s", msg.get("error"))
                else:
                    _LOGGER.debug("Result for unknown request id %s", msg.get("id"))
                return
            if not fut.done():
                fut.set_result(msg)
            return
        event = msg.get("event")
        if isinstance(event, str):
            data = msg.get("data")
            if not isinstance(data, dict):
                data = {}
            if self._on_event is not None:
                try:
                    self._on_event(event, data)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Event callback failed for %s", event)
            return
        _LOGGER.debug("Ignoring unknown message: %s", msg)

    async def _request(
        self, cmd: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if self._writer is None or self._closed.is_set():
            raise ConnectionLost("not connected")
        req_id = next(self._ids)
        msg: dict[str, Any] = {"id": req_id, "cmd": cmd}
        if params:
            msg["params"] = params
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            self._writer.write(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n")
            await self._writer.drain()
            reply = await asyncio.wait_for(fut, timeout or self._request_timeout)
        except TimeoutError as err:
            self._pending.pop(req_id, None)
            # A silent server is a dead connection; close so the owner reconnects.
            await self._abort(ConnectionLost(f"{cmd} timed out"))
            raise ConnectionLost(f"{cmd} timed out after {timeout or self._request_timeout:g} s") from err
        except OSError as err:
            self._pending.pop(req_id, None)
            await self._abort(err)
            raise ConnectionLost(str(err)) from err
        if reply.get("ok"):
            data = reply.get("data")
            return data if isinstance(data, dict) else {}
        error = reply.get("error") or {}
        if not isinstance(error, dict):
            error = {}
        raise CommandError(str(error.get("code", "unknown")), str(error.get("message", "")))

    async def request(
        self, cmd: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request and return its ``data``; raises ``CommandError`` on ``ok: false``."""
        if not self._established:
            raise ConnectionLost("not authenticated")
        return await self._request(cmd, params, timeout=timeout)

    # ---------------------------------------------------------- commands

    async def ping(self) -> dict[str, Any]:
        return await self.request("ping")

    async def subscribe(self, sensors: list[str] | None = None) -> dict[str, Any]:
        """Subscribe and return ``{"poll_interval": s, "sensors": {name: {value, ts, ...meta}}}``."""
        return await self.request("subscribe", {"sensors": sensors or ["*"]})

    async def unsubscribe(self) -> dict[str, Any]:
        return await self.request("unsubscribe")

    async def list_sensors(self) -> dict[str, Any]:
        return await self.request("list_sensors")

    async def get_status(self) -> dict[str, Any]:
        return await self.request("get_status")

    async def set_hot_water_mode(self, mode: str) -> dict[str, Any]:
        if mode not in HOT_WATER_MODES:
            raise ValueError(f"mode must be one of {HOT_WATER_MODES}")
        return await self.request("set_hot_water_mode", {"mode": mode})

    async def set_heating_request(self, mode: str) -> dict[str, Any]:
        if mode not in HEATING_MODES:
            raise ValueError(f"mode must be one of {HEATING_MODES}")
        return await self.request("set_heating_request", {"mode": mode})

    async def trigger_hot_water_boost(self) -> dict[str, Any]:
        return await self.request("trigger_hot_water_boost", timeout=BOOST_TIMEOUT)

    async def refresh(self) -> dict[str, Any]:
        return await self.request("refresh")

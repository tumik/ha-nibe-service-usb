"""Tests for the pure asyncio client against the fake gateway."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import socket

import pytest

from custom_components.nibe_service_usb.client import (
    AuthenticationError,
    CommandError,
    ConnectionFailed,
    ConnectionLost,
    NibeServiceUsbClient,
    ProtocolError,
    ServerInfo,
    compute_proof,
    compute_server_proof,
    parse_key,
)

from .fake_server import SERVER_ID, TEST_KEY, FakeServer


def make_client(server: FakeServer, key: bytes = TEST_KEY, **kwargs) -> NibeServiceUsbClient:
    return NibeServiceUsbClient("127.0.0.1", server.port, key, **kwargs)


@pytest.fixture
async def client(fake_server: FakeServer):
    c = make_client(fake_server)
    await c.connect()
    yield c
    await c.close()


def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(predicate, timeout: float = 2.0) -> None:  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


# ------------------------------------------------------------------ connect


async def test_connect_returns_server_info(fake_server: FakeServer) -> None:
    c = make_client(fake_server, client_name="pytest/1")
    assert not c.connected
    try:
        info = await c.connect()
        assert isinstance(info, ServerInfo)
        assert info.server_id == SERVER_ID
        assert info.version == "0.1.0"
        assert info.proto == 1
        assert info.name == "nibe-service-usb"
        assert c.server == info
        assert c.connected
        assert fake_server.calls[0][0] == "auth"
        assert fake_server.calls[0][1]["client"] == "pytest/1"
        assert fake_server.calls[0][1]["proto"] == 1
    finally:
        await c.close()
    assert not c.connected


async def test_wrong_key_raises_authentication_error(fake_server: FakeServer) -> None:
    c = make_client(fake_server, key=bytes(32))
    with pytest.raises(AuthenticationError):
        await c.connect()
    assert not c.connected


async def test_server_rejecting_auth(fake_server: FakeServer) -> None:
    fake_server.fail_auth = True
    c = make_client(fake_server)
    with pytest.raises(AuthenticationError):
        await c.connect()
    assert not c.connected


async def test_wrong_server_proof_raises_authentication_error(fake_server: FakeServer) -> None:
    fake_server.wrong_server_proof = True
    c = make_client(fake_server)
    with pytest.raises(AuthenticationError, match="proof"):
        await c.connect()
    assert not c.connected


async def test_unsupported_proto_raises_protocol_error(fake_server: FakeServer) -> None:
    fake_server.proto = 2
    c = make_client(fake_server)
    with pytest.raises(ProtocolError):
        await c.connect()
    assert not c.connected
    # The client must not even try to authenticate against an unknown protocol.
    assert fake_server.calls == []


async def test_connection_refused_raises_connection_failed() -> None:
    c = NibeServiceUsbClient("127.0.0.1", unused_port(), TEST_KEY)
    with pytest.raises(ConnectionFailed):
        await c.connect()
    assert not c.connected


# ------------------------------------------------------------------ commands


async def test_ping(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    result = await client.ping()
    assert result["pump_online"] is True
    assert result["ha_alive"] is True
    assert "uptime" in result
    assert ("ping", {}) in fake_server.calls


async def test_subscribe(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    result = await client.subscribe()
    assert result["poll_interval"] == 600
    sensors = result["sensors"]
    assert sensors["bt1_outdoor"]["value"] == 4.7
    assert sensors["bt1_outdoor"]["unit"] == "°C"
    assert sensors["hw_comfort"]["options"] == ["eco", "normal", "lux"]
    assert sensors["compressor_running"]["value"] is True
    for meta in sensors.values():
        for key in (
            "name",
            "kind",
            "unit",
            "precision",
            "tier",
            "device_class",
            "state_class",
            "description",
            "ts",
        ):
            assert key in meta
    assert ("subscribe", {"sensors": ["*"]}) in fake_server.calls


async def test_subscribe_subset(client: NibeServiceUsbClient) -> None:
    result = await client.subscribe(["uptime"])
    assert list(result["sensors"]) == ["uptime"]


async def test_list_sensors_and_unsubscribe(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    listed = await client.list_sensors()
    assert "value" not in listed["sensors"]["bt1_outdoor"]
    assert await client.unsubscribe() == {}
    assert [c for c, _ in fake_server.calls][-2:] == ["list_sensors", "unsubscribe"]


async def test_get_status(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    status = await client.get_status()
    assert status["identity"]["product"] == "F1226-6 R"
    assert status["hot_water"]["mode"] == "normal"
    assert ("get_status", {}) in fake_server.calls


async def test_set_hot_water_mode(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    assert await client.set_hot_water_mode("lux") == {"mode": "lux"}
    assert fake_server.snapshot["hw_comfort"]["value"] == "lux"
    assert (await client.get_status())["hot_water"]["mode"] == "lux"
    assert ("set_hot_water_mode", {"mode": "lux"}) in fake_server.calls


async def test_set_heating_request(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    result = await client.set_heating_request("disable")
    assert result["mode"] == "disable"
    assert result["active"] is True
    assert (await client.get_status())["heating"]["mode"] == "disable"
    assert (await client.set_heating_request("auto"))["active"] is False


async def test_trigger_boost_and_refresh(client: NibeServiceUsbClient) -> None:
    boost = await client.trigger_hot_water_boost()
    assert boost["started"] is True
    assert boost["remaining_seconds"] == 18000
    assert await client.refresh() == {"queued": True}


async def test_boost_error(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    fake_server.boost_error = ("rate_limited", "retry in 500 s")
    with pytest.raises(CommandError) as exc_info:
        await client.trigger_hot_water_boost()
    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.message == "retry in 500 s"


async def test_command_error_carries_code_and_message(client: NibeServiceUsbClient) -> None:
    with pytest.raises(CommandError) as exc_info:
        await client.request("set_hot_water_mode", {"mode": "bad"})
    assert exc_info.value.code == "invalid_params"
    assert exc_info.value.message
    assert "invalid_params" in str(exc_info.value)
    assert client.connected  # an error result does not kill the connection


async def test_unknown_command(client: NibeServiceUsbClient) -> None:
    with pytest.raises(CommandError) as exc_info:
        await client.request("frobnicate")
    assert exc_info.value.code == "unknown_command"


@pytest.mark.parametrize(
    ("method", "arg"),
    [("set_hot_water_mode", "turbo"), ("set_heating_request", "maybe")],
)
async def test_invalid_mode_rejected_client_side(
    client: NibeServiceUsbClient, fake_server: FakeServer, method: str, arg: str
) -> None:
    with pytest.raises(ValueError, match="mode"):
        await getattr(client, method)(arg)
    assert method not in [c for c, _ in fake_server.calls]


async def test_request_before_connect_fails(fake_server: FakeServer) -> None:
    c = make_client(fake_server)
    with pytest.raises(ConnectionLost):
        await c.ping()


async def test_pipelined_requests(client: NibeServiceUsbClient, fake_server: FakeServer) -> None:
    fake_server.delay_results = 0.05
    results = await asyncio.gather(client.ping(), client.get_status(), client.refresh())
    assert results[0]["pump_online"] is True
    assert "identity" in results[1]
    assert results[2] == {"queued": True}


# ------------------------------------------------------------------ events


async def test_events_delivered_to_callback(fake_server: FakeServer) -> None:
    events: list[tuple[str, dict]] = []
    c = make_client(fake_server, on_event=lambda name, data: events.append((name, data)))
    await c.connect()
    try:
        await c.subscribe()
        await wait_for(lambda: fake_server.authenticated_connections)
        await fake_server.push_sensor_update({"bt1_outdoor": 3.9, "degree_minutes": -50.0})
        await wait_for(lambda: events)
        assert events[0] == ("sensor_update", {"values": {"bt1_outdoor": 3.9, "degree_minutes": -50.0}})
        assert fake_server.snapshot["bt1_outdoor"]["value"] == 3.9

        status = {"heating": {"mode": "auto"}}
        await fake_server.push_status(status)
        await wait_for(lambda: len(events) == 2)
        assert events[1] == ("status", status)
    finally:
        await c.close()


async def test_event_callback_exception_does_not_kill_client(fake_server: FakeServer) -> None:
    def boom(name: str, data: dict) -> None:
        raise RuntimeError("callback failure")

    c = make_client(fake_server, on_event=boom)
    await c.connect()
    try:
        await fake_server.push_sensor_update({"bt1_outdoor": 1.0})
        await asyncio.sleep(0.05)
        assert c.connected
        await c.ping()
    finally:
        await c.close()


# ------------------------------------------------------------------ disconnect


async def test_drop_connections_calls_on_disconnect_once(fake_server: FakeServer) -> None:
    reasons: list[Exception | None] = []
    c = make_client(fake_server, on_disconnect=reasons.append)
    await c.connect()
    await fake_server.drop_connections()
    await asyncio.wait_for(c.wait_closed(), 2.0)
    await wait_for(lambda: reasons)
    assert len(reasons) == 1
    assert not c.connected
    await c.close()  # does not fire the callback again
    await asyncio.sleep(0.05)
    assert len(reasons) == 1
    with pytest.raises(ConnectionLost):
        await c.ping()


async def test_pending_requests_fail_with_connection_lost_on_drop(fake_server: FakeServer) -> None:
    reasons: list[Exception | None] = []
    fake_server.hang_ping = True
    c = make_client(fake_server, on_disconnect=reasons.append)
    await c.connect()
    pending = asyncio.ensure_future(c.ping())
    await wait_for(lambda: ("ping", {}) in fake_server.calls)
    await fake_server.drop_connections()
    with pytest.raises(ConnectionLost):
        await asyncio.wait_for(pending, 2.0)
    assert not c.connected
    await wait_for(lambda: reasons)
    assert len(reasons) == 1


async def test_close_does_not_call_on_disconnect_with_error(fake_server: FakeServer) -> None:
    reasons: list[Exception | None] = []
    c = make_client(fake_server, on_disconnect=reasons.append)
    await c.connect()
    await c.close()
    assert not c.connected
    assert reasons == [None]


async def test_request_timeout_closes_connection(fake_server: FakeServer) -> None:
    fake_server.hang_ping = True
    reasons: list[Exception | None] = []
    c = make_client(fake_server, request_timeout=0.2, on_disconnect=reasons.append)
    await c.connect()
    with pytest.raises(ConnectionLost, match="timed out"):
        await c.ping()
    assert not c.connected
    assert len(reasons) == 1
    assert isinstance(reasons[0], ConnectionLost)
    await wait_for(lambda: not fake_server.connections)


async def test_close_is_idempotent_and_client_is_single_use(fake_server: FakeServer) -> None:
    c = make_client(fake_server)
    await c.connect()
    await c.close()
    await c.close()
    assert not c.connected
    with pytest.raises(RuntimeError):
        await c.connect()


async def test_fake_server_stop_drops_clients(fake_server: FakeServer) -> None:
    c = make_client(fake_server)
    await c.connect()
    await fake_server.stop()
    await asyncio.wait_for(c.wait_closed(), 2.0)
    assert not c.connected


# ------------------------------------------------------------------ key helpers


def format_key_grouped(key: bytes) -> str:
    text = base64.b32encode(key).decode("ascii").rstrip("=")
    return "-".join(text[i : i + 8] for i in range(0, len(text), 8))


def test_parse_key_hex() -> None:
    assert parse_key(TEST_KEY.hex()) == TEST_KEY
    assert parse_key(f"  {TEST_KEY.hex().upper()}\n") == TEST_KEY


def test_parse_key_grouped_base32() -> None:
    grouped = format_key_grouped(TEST_KEY)
    assert "-" in grouped
    assert "=" not in grouped
    assert parse_key(grouped) == TEST_KEY
    assert parse_key(grouped.lower()) == TEST_KEY
    assert parse_key(grouped.replace("-", " ")) == TEST_KEY
    assert parse_key(grouped.replace("-", "")) == TEST_KEY


@pytest.mark.parametrize(
    "garbage",
    ["", "not a key", "zz" * 32, "abcd", "1" * 64 + "00", "!!!!!!!!-!!!!!!!!", "A" * 10],
)
def test_parse_key_rejects_garbage(garbage: str) -> None:
    with pytest.raises(ValueError):
        parse_key(garbage)


def test_proofs_match_independent_hmac() -> None:
    server_raw = bytes(range(100, 132))
    client_raw = bytes(range(200, 232))
    s_b64 = base64.b64encode(server_raw).decode()
    c_b64 = base64.b64encode(client_raw).decode()
    assert (
        compute_proof(TEST_KEY, s_b64, c_b64)
        == hmac.new(TEST_KEY, server_raw + client_raw, hashlib.sha256).hexdigest()
    )
    assert (
        compute_server_proof(TEST_KEY, s_b64, c_b64)
        == hmac.new(TEST_KEY, client_raw + server_raw, hashlib.sha256).hexdigest()
    )
    assert compute_proof(TEST_KEY, s_b64, c_b64) != compute_server_proof(TEST_KEY, s_b64, c_b64)


def test_proof_rejects_bad_nonce() -> None:
    with pytest.raises(ValueError):
        compute_proof(TEST_KEY, "not base64!!", "AAAA")

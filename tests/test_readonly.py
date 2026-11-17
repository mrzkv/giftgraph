import asyncio
import sqlite3
from types import SimpleNamespace as NS

import pytest

from giftgraph.readonly import (
    ReadOnlyViolation,
    read_only_client_class,
    validate_existing_session,
)


def raw_types():
    names = {
        "contacts": ["ResolveUsername"],
        "users": ["GetUsers"],
        "channels": ["GetChannels", "GetParticipants"],
        "messages": ["GetChats", "GetFullChat"],
        "payments": ["GetSavedStarGifts"],
        "help": ["GetConfig"],
    }
    groups = {}
    for group, methods in names.items():
        groups[group] = NS(
            **{
                name: type(name, (), {"QUALNAME": f"functions.{group}.{name}"})
                for name in methods
            }
        )
    return NS(functions=NS(**groups))


class Transport:
    def __init__(self):
        self.forwarded = []

    async def invoke(self, query, *args, **kwargs):
        self.forwarded.append(query)
        return "read result"


@pytest.mark.parametrize(
    "name",
    [
        "SendMessage",
        "SendMedia",
        "SendReaction",
        "SetTyping",
        "ReadHistory",
        "ReadMentions",
        "SaveDraft",
        "JoinChannel",
        "LeaveChannel",
        "UpdateStatus",
        "UpdateProfile",
        "TransferStarGift",
        "SendCode",
        "InvokeWithoutUpdates",
    ],
)
async def test_forbidden_rpc_never_reaches_transport(name):
    raw = raw_types()
    client = read_only_client_class(Transport, raw)()
    request = type(name, (), {"QUALNAME": "functions.messages." + name})()
    with pytest.raises(ReadOnlyViolation):
        await client.invoke(request)
    assert client.forwarded == []


async def test_exact_classes_checked_not_spoofable_names():
    raw = raw_types()
    client = read_only_client_class(Transport, raw)()
    spoof = type("GetUsers", (), {"QUALNAME": "functions.users.GetUsers"})()
    with pytest.raises(ReadOnlyViolation):
        await client.invoke(spoof)
    assert client.forwarded == []


async def test_safe_reads_are_counted_without_recording_parameters():
    raw = raw_types()
    client = read_only_client_class(Transport, raw)()
    request = raw.functions.payments.GetSavedStarGifts()
    assert await client.invoke(request) == "read result"
    assert client.forwarded == [request]
    assert dict(client.read_request_counts) == {
        "functions.payments.GetSavedStarGifts": 1
    }


async def test_all_allowed_requests_share_minimum_interval_and_wait_only_remainder(monkeypatch):
    import giftgraph.readonly as readonly

    clock = {"now": 100.0}
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    fake_asyncio = NS(
        Lock=asyncio.Lock,
        get_running_loop=lambda: NS(time=lambda: clock["now"]),
        sleep=sleep,
    )
    monkeypatch.setattr(readonly, "asyncio", fake_asyncio)
    raw = raw_types()
    client = read_only_client_class(Transport, raw)()
    client.read_request_delay = 1.0

    await client.invoke(raw.functions.contacts.ResolveUsername())
    assert waits == []
    clock["now"] += 0.25
    await client.invoke(raw.functions.channels.GetParticipants())
    assert waits == [0.75]
    clock["now"] += 2
    await client.invoke(raw.functions.payments.GetSavedStarGifts())
    assert waits == [0.75]
    assert len(client.forwarded) == 3


async def test_forbidden_request_does_not_wait_or_advance_read_clock(monkeypatch):
    import giftgraph.readonly as readonly

    clock = {"now": 100.0}
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(readonly, "asyncio", NS(
        Lock=asyncio.Lock,
        get_running_loop=lambda: NS(time=lambda: clock["now"]),
        sleep=sleep,
    ))
    raw = raw_types()
    client = read_only_client_class(Transport, raw)()
    client.read_request_delay = 1.0
    request = raw.functions.users.GetUsers()
    await client.invoke(request)
    with pytest.raises(ReadOnlyViolation):
        await client.invoke(type("SendMessage", (), {})())
    assert waits == []
    assert client._read_last_request == 100.0
    assert client.forwarded == [request]
    await client.invoke(request)
    assert waits == [1.0]


def create_session(path, *, auth_key=b"x" * 256, user_id=123, is_bot=0):
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE sessions(
            user_id INTEGER, is_bot INTEGER, test_mode INTEGER, auth_key BLOB,
            api_id INTEGER, dc_id INTEGER, server_address TEXT, port INTEGER)""")
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (user_id, is_bot, 0, auth_key, 1, 2, "127.0.0.1", 443),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"auth_key": None},
        {"auth_key": b"bad"},
        {"user_id": None},
        {"is_bot": 1},
    ],
)
def test_incomplete_sessions_rejected_without_network_or_reauthorization(
    tmp_path, kwargs
):
    path = tmp_path / "test.session"
    create_session(path, **kwargs)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="сессия"):
        validate_existing_session(path)
    assert path.read_bytes() == before


def test_valid_session_validation_does_not_modify_file(tmp_path):
    path = tmp_path / "test.session"
    create_session(path)
    before = path.read_bytes()
    validate_existing_session(path)
    assert path.read_bytes() == before

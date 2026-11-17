from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from giftgraph.models import Peer, PeerUnavailable
from giftgraph.telegram import TelegramSource, normalize_gift_page


def raw_user(user_id):
    return NS(user_id=user_id)


def raw_regular(**kwargs):
    return NS(gift=NS(id=10), date=100, **kwargs)


def raw_unique(**kwargs):
    return NS(id=20, gift_id=10, slug="Gift-1", title="Gift #1", **kwargs)


def response(*items, **kwargs):
    return NS(gifts=list(items), **kwargs)


def test_regular_edges_point_to_profile_and_keep_same_second_occurrences():
    raw = response(raw_regular(from_id=raw_user(2)), raw_regular(from_id=raw_user(2)))
    page = normalize_gift_page(raw, Peer(1))
    assert len(page.events) == 2
    assert len({event.key for event in page.events}) == 2
    assert {(event.sender_key, event.recipient_key) for event in page.events} == {
        ("user:2", "user:1")
    }
    assert normalize_gift_page(raw, Peer(1)).events == page.events
    assert (
        normalize_gift_page(raw, Peer(1), "page-2").events[0].key != page.events[0].key
    )
    assert page.item_count == 2


def test_regular_message_id_is_stable_across_page_cursors():
    raw = response(raw_regular(from_id=raw_user(2), msg_id=7))
    assert normalize_gift_page(raw, Peer(1)).events[0].key == (
        normalize_gift_page(raw, Peer(1), "another-cursor").events[0].key
    )


def test_regular_keeps_catalog_price_and_title_not_conversion_value():
    saved = NS(
        gift=NS(id=10, title="Red Rose", stars=75, convert_stars=65),
        date=100,
        from_id=raw_user(2),
        convert_stars=60,
        upgrade_stars=100,
    )
    gift = normalize_gift_page(response(saved), Peer(1)).gifts[0]
    assert gift.title == "Red Rose"
    assert gift.stars == 75
    assert gift.number is None


def test_regular_title_falls_back_to_sticker_emoji_then_id():
    sticker = NS(attributes=[NS(alt="🌹")])
    saved = NS(gift=NS(id=10, sticker=sticker), date=100)
    assert normalize_gift_page(response(saved), Peer(1)).gifts[0].title == "🌹"
    assert normalize_gift_page(response(raw_regular()), Peer(1)).gifts[0].title == "Gift 10"
    assert normalize_gift_page(response(raw_regular()), Peer(1)).gifts[0].stars is None


@pytest.mark.parametrize("title", ["Plush Bear", "Plush Bear #42"])
def test_unique_title_contains_number_and_never_substitutes_resale_price(title):
    gift = NS(
        id=20,
        gift_id=10,
        slug="PlushBear-42",
        title=title,
        num=42,
        attributes=[],
        resell_amount=[NS(amount=9999)],
        value_amount=8888,
        value_currency="XTR",
        offer_min_stars=7777,
    )
    normalized = normalize_gift_page(response(NS(gift=gift, date=100)), Peer(1)).gifts[0]
    assert normalized.title == "Plush Bear #42"
    assert normalized.number == 42
    assert normalized.stars is None


def test_gift_descriptions_are_opt_in_and_keep_separate_duplicate_gift_notes():
    raw = response(
        raw_regular(from_id=raw_user(2), msg_id=7, message=NS(text="Первый подарок")),
        raw_regular(from_id=raw_user(2), msg_id=8, message=NS(text="Второй подарок")),
    )
    assert all(event.description is None for event in normalize_gift_page(raw, Peer(1)).events)
    included = normalize_gift_page(raw, Peer(1), include_descriptions=True)
    assert [event.description for event in included.events] == [
        "Первый подарок", "Второй подарок"
    ]
    assert len({event.key for event in included.events}) == 2


def test_unique_description_uses_original_details_not_current_profile_message():
    gift = raw_unique(attributes=[NS(
        sender_id=raw_user(1), recipient_id=raw_user(2), date=50,
        message=NS(text="Original note"),
    )])
    raw = response(NS(gift=gift, date=100, message=NS(text="Unrelated note")))
    assert normalize_gift_page(raw, Peer(3)).events[0].description is None
    assert normalize_gift_page(
        raw, Peer(3), include_descriptions=True
    ).events[0].description == "Original note"


def test_installed_raw_gift_schema_roundtrips_catalog_and_unique_fields():
    from pyrogram import raw

    saved_regular = raw.types.SavedStarGift(
        date=100,
        gift=raw.types.StarGift(
            id=10, sticker=raw.types.DocumentEmpty(id=1), stars=75,
            convert_stars=65, title="Rose",
        ),
        from_id=raw.types.PeerUser(user_id=2),
        message=raw.types.TextWithEntities(text="Happy birthday", entities=[]),
    )
    original = raw.types.StarGiftAttributeOriginalDetails(
        recipient_id=raw.types.PeerUser(user_id=1),
        sender_id=raw.types.PeerUser(user_id=2), date=50,
        message=raw.types.TextWithEntities(text="Original", entities=[]),
    )
    saved_unique = raw.types.SavedStarGift(
        date=100,
        gift=raw.types.StarGiftUnique(
            id=20, gift_id=10, title="Rose", slug="Rose-42", num=42,
            attributes=[original], availability_issued=100, availability_total=100,
        ),
    )
    page = normalize_gift_page(
        response(saved_regular, saved_unique), Peer(1), include_descriptions=True
    )
    assert [(gift.title, gift.stars, gift.number) for gift in page.gifts] == [
        ("Rose", 75, None), ("Rose #42", None, 42),
    ]
    assert [event.description for event in page.events] == ["Happy birthday", "Original"]


def test_anonymous_and_hidden_senders_never_invent_nodes_or_leak_private_sender():
    raw = response(
        raw_regular(),
        raw_regular(from_id=raw_user(2), name_hidden=True, message=NS(text="secret")),
    )
    page = normalize_gift_page(raw, Peer(1))
    assert [event.sender_key for event in page.events] == [None, None]
    assert {peer.key for peer in page.peers} == {"user:1"}
    assert "secret" not in repr(page)


def test_unique_original_recipient_is_not_current_owner():
    gift = raw_unique(
        owner_id=raw_user(3),
        attributes=[NS(sender_id=raw_user(1), recipient_id=raw_user(2), date=50)],
    )
    page = normalize_gift_page(
        response(NS(gift=gift, date=100, from_id=raw_user(4))), Peer(3)
    )
    assert len(page.events) == 1
    event = page.events[0]
    assert (event.sender_key, event.recipient_key, event.kind, event.date) == (
        "user:1",
        "user:2",
        "original_gift",
        50,
    )
    assert page.ownerships[0].owner_key == "user:3"
    assert page.gifts[0].catalog_id == "10"
    assert "user:4" not in {peer.key for peer in page.peers}


def test_unique_without_details_produces_ownership_only():
    raw = response(NS(gift=raw_unique(attributes=[]), date=100, from_id=raw_user(4)))
    page = normalize_gift_page(raw, Peer(3))
    assert not page.events
    assert page.ownerships[0].owner_key == "user:3"


def test_unique_anonymous_original_gift_keeps_null_sender():
    gift = raw_unique(attributes=[NS(recipient_id=raw_user(2), date=50)])
    page = normalize_gift_page(response(NS(gift=gift, date=100)), Peer(3))
    assert page.events[0].sender_key is None
    assert page.events[0].recipient_key == "user:2"


def test_hosted_and_blockchain_gift_never_assign_profile_as_owner():
    hosted = raw_unique(host_id=raw_user(3), owner_address="wallet", attributes=[])
    page = normalize_gift_page(response(NS(gift=hosted, date=100)), Peer(3))
    assert page.ownerships[0].owner_key is None
    assert page.ownerships[0].host_key == "user:3"
    assert "wallet" not in repr(page)


def test_unsaved_skipped_but_counts_toward_raw_page_limit():
    page = normalize_gift_page(response(raw_regular(unsaved=True)), Peer(1))
    assert page.item_count == 1
    assert not page.gifts
    assert not page.events


def test_sender_channels_and_users_have_distinct_keys_and_metadata():
    channel = type(
        "Channel", (), {"id": 2, "title": "Channel", "username": "channel"}
    )()
    raw = response(
        raw_regular(from_id=NS(channel_id=2)),
        raw_regular(from_id=raw_user(2)),
        users=[NS(id=2, first_name="Ada", last_name="L", username="ada")],
        chats=[channel],
    )
    page = normalize_gift_page(raw, Peer(1))
    peers = {peer.key: peer for peer in page.peers}
    assert peers["user:2"].name == "Ada L"
    assert peers["channel:2"].name == "Channel"
    assert {event.sender_key for event in page.events} == {"user:2", "channel:2"}


class FakeRPCError(Exception):
    pass


class FakeFloodWait(FakeRPCError):
    def __init__(self, value):
        self.value = value


class ChannelPrivate(FakeRPCError):
    pass


def source(tmp_path):
    result = TelegramSource(tmp_path / "session.session")
    result._errors = NS(FloodWait=FakeFloodWait, RPCError=FakeRPCError)
    return result


async def test_flood_wait_retries_identical_request_without_dropping_offset(
    tmp_path, monkeypatch
):
    provider = source(tmp_path)
    sleep = AsyncMock()
    monkeypatch.setattr("giftgraph.telegram.asyncio.sleep", sleep)
    request = NS(offset="cursor-2")
    provider._client = NS(invoke=AsyncMock(side_effect=[FakeFloodWait(8), "result"]))
    assert await provider._invoke(request) == "result"
    sleep.assert_awaited_once_with(9)
    assert provider._client.invoke.await_count == 2
    assert all(
        call.args == (request,) for call in provider._client.invoke.await_args_list
    )


async def test_access_error_is_peer_unavailable_but_other_rpc_errors_propagate(
    tmp_path,
):
    provider = source(tmp_path)
    with pytest.raises(PeerUnavailable, match="ChannelPrivate"):
        await provider._call(AsyncMock(side_effect=ChannelPrivate()))
    with pytest.raises(FakeRPCError):
        await provider._call(AsyncMock(side_effect=FakeRPCError()))


async def test_gift_request_filters_hosted_and_unsaved_and_preserves_cursor(tmp_path):
    provider = source(tmp_path)
    provider._raw = NS(
        functions=NS(payments=NS(GetSavedStarGifts=lambda **kw: NS(**kw)))
    )
    provider._client = NS(resolve_peer=AsyncMock(return_value="resolved"))
    provider._invoke = AsyncMock(return_value=response(next_offset="cursor-3"))
    page = await provider.fetch_gifts(Peer(3), "cursor-2", 12)
    request = provider._invoke.await_args.args[0]
    assert vars(request) == {
        "peer": "resolved",
        "offset": "cursor-2",
        "limit": 12,
        "exclude_unsaved": True,
        "exclude_hosted": True,
    }
    assert page.next_offset == "cursor-3"


async def test_gift_source_passes_description_opt_in_to_normalizer(tmp_path):
    provider = TelegramSource(tmp_path / "session.session", include_descriptions=True)
    provider._errors = NS(FloodWait=FakeFloodWait, RPCError=FakeRPCError)
    provider._raw = NS(functions=NS(payments=NS(GetSavedStarGifts=lambda **kw: NS(**kw))))
    provider._client = NS(resolve_peer=AsyncMock(return_value="resolved"))
    provider._invoke = AsyncMock(return_value=response(raw_regular(message=NS(text="Saved"))))
    assert (await provider.fetch_gifts(Peer(3), "", 12)).events[0].description == "Saved"


async def test_source_sets_rpc_delay_before_connecting_existing_session(tmp_path, monkeypatch):
    import pyrogram

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.is_connected = False
            self.storage = NS(is_bot=AsyncMock(return_value=False))

        async def connect(self):
            self.delay_at_connect = self.read_request_delay
            self.is_connected = True
            return True

        async def disconnect(self):
            self.is_connected = False

    session = tmp_path / "existing.session"
    session.write_bytes(b"mock session")
    monkeypatch.setattr("giftgraph.telegram.validate_existing_session", lambda _path: None)
    monkeypatch.setattr(pyrogram, "Client", FakeClient)
    provider = TelegramSource(session, request_delay=1.25)
    async with provider:
        assert provider._client.delay_at_connect == 1.25
    assert not provider._client.is_connected


async def test_missing_session_fails_before_attempting_login(tmp_path):
    with pytest.raises(RuntimeError, match="generate_session.py"):
        async with TelegramSource(tmp_path / "missing"):
            pytest.fail("Must not connect without a preexisting session")


async def test_group_members_are_paginated_and_response_extra_users_not_seeded(
    tmp_path,
):
    provider = source(tmp_path)
    provider.resolve_peer = AsyncMock(return_value=Peer(99, "channel"))
    provider._input_peer = AsyncMock(return_value=NS(channel_id=99, access_hash=123))
    provider._raw = NS(
        functions=NS(channels=NS(GetParticipants=lambda **kw: NS(**kw))),
        types=NS(
            InputChannel=lambda **kw: NS(**kw),
            ChannelParticipantsSearch=lambda **kw: NS(**kw),
        ),
    )
    provider._invoke = AsyncMock(
        side_effect=[
            NS(participants=[NS(user_id=1)], users=[NS(id=1), NS(id=999)], count=2),
            NS(participants=[NS(user_id=2)], users=[NS(id=2)], count=2),
        ]
    )
    members = [member async for member in provider.iter_members("test", 5)]
    assert [member.id for member in members] == [1, 2]
    assert [call.args[0].offset for call in provider._invoke.await_args_list] == [0, 1]


async def test_numeric_channel_resolution_uses_canonical_positive_id(tmp_path):
    provider = source(tmp_path)
    provider._client = NS(
        get_chat=AsyncMock(
            return_value=NS(
                id=-1000000000123,
                title="Channel",
                username="channel",
            )
        )
    )
    assert await provider.resolve_peer("channel:123") == Peer(
        123, "channel", "Channel", "channel"
    )
    provider._client.get_chat.assert_awaited_once_with(-1000000000123, force_full=False)

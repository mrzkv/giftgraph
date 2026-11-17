"""Read-only Kurigram provider and normalization of public gift evidence.

Imports of Kurigram are lazy so an offline demo/export needs no Telegram session.
Gift notes are collected only on explicit request; authorization data is never exported.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from .models import Gift, GiftEvent, GiftPage, Ownership, Peer, PeerUnavailable
from .readonly import read_only_client_class, validate_existing_session

log = logging.getLogger(__name__)
T = TypeVar("T")


def _peer_ref(value: Any) -> Peer | None:
    if value is None:
        return None
    for attribute, kind in (
        ("user_id", "user"),
        ("channel_id", "channel"),
        ("chat_id", "chat"),
    ):
        peer_id = getattr(value, attribute, None)
        if peer_id is not None:
            return Peer(id=int(peer_id), kind=kind)
    return None


def _entity(value: Any, kind: str) -> Peer:
    if kind == "user":
        name = " ".join(
            part
            for part in (
                getattr(value, "first_name", None),
                getattr(value, "last_name", None),
            )
            if part
        )
    else:
        name = getattr(value, "title", None) or ""
    return Peer(
        id=int(value.id),
        kind=kind,
        name=name,
        username=getattr(value, "username", None),
    )


def _chat_kind(value: Any) -> str:
    # Raw Channel/ChannelForbidden objects are not basic Chat objects.
    return "channel" if "Channel" in type(value).__name__ else "chat"


def _fingerprint(*values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _gift_title(gift: Any, catalog_id: str) -> str:
    title = getattr(gift, "title", None)
    if title:
        return title
    sticker = getattr(gift, "sticker", None)
    emoji = getattr(sticker, "emoji", None)
    if emoji:
        return emoji
    for attribute in getattr(sticker, "attributes", ()) or ():
        alt = getattr(attribute, "alt", None)
        if alt:
            return alt
    return f"Gift {catalog_id}"


def _gift_description(value: Any, include_descriptions: bool) -> str | None:
    if not include_descriptions:
        return None
    text = getattr(getattr(value, "message", None), "text", None)
    return text if isinstance(text, str) and text else None


def normalize_gift_page(
    response: Any,
    profile: Peer,
    offset: str = "",
    *,
    include_descriptions: bool = False,
) -> GiftPage:
    """Turn one raw MTProto page into evidence without inventing transfer events.

    Regular gifts have catalog IDs, not globally unique instance IDs. Without a
    message/saved ID, the page cursor and occurrence index distinguish identical
    visible gifts. Such event keys are only stable within the crawl snapshot.
    """
    peers: dict[str, Peer] = {profile.key: profile}
    for user in getattr(response, "users", ()) or ():
        peer = _entity(user, "user")
        peers[peer.key] = peer
    for chat in getattr(response, "chats", ()) or ():
        peer = _entity(chat, _chat_kind(chat))
        peers[peer.key] = peer

    def reference(value: Any) -> str | None:
        peer = _peer_ref(value)
        if peer is None:
            return None
        peers.setdefault(peer.key, peer)
        return peer.key

    gifts: dict[str, Gift] = {}
    events: list[GiftEvent] = []
    ownerships: list[Ownership] = []
    items = list(getattr(response, "gifts", ()) or ())
    for index, saved in enumerate(items):
        # This should already be enforced server-side, but don't accidentally
        # include an unsaved gift when inspecting our own account.
        if getattr(saved, "unsaved", False):
            continue
        gift = saved.gift
        unique = getattr(gift, "slug", None) is not None
        key = f"{'unique' if unique else 'regular'}:{gift.id}"
        catalog_id = str(getattr(gift, "gift_id", gift.id)) if unique else str(gift.id)
        number = getattr(gift, "num", None) if unique else None
        title = _gift_title(gift, catalog_id)
        if number is not None and not title.endswith(f"#{number}"):
            title = f"{title} #{number}"
        # StarGift.stars is the catalog price observed now. A unique gift only
        # exposes resale/valuation figures, not what its original sender paid.
        stars = None if unique else getattr(gift, "stars", None)
        gifts[key] = Gift(
            key=key,
            kind="unique" if unique else "regular",
            catalog_id=catalog_id,
            title=title,
            slug=getattr(gift, "slug", None),
            stars=stars,
            number=number,
        )

        if unique:
            owner_key = reference(getattr(gift, "owner_id", None))
            host_key = reference(getattr(gift, "host_id", None))
            # Hosted gifts don't imply that the profile owns the gift. A missing
            # owner with a blockchain address must likewise stay unknown.
            if (
                owner_key is None
                and not host_key
                and not getattr(gift, "owner_address", None)
            ):
                owner_key = profile.key
            ownerships.append(Ownership(key, owner_key, profile.key, host_key))
            for attribute in getattr(gift, "attributes", ()) or ():
                # OriginalDetails is the only gift attribute with recipient_id.
                original_recipient = getattr(attribute, "recipient_id", None)
                if original_recipient is None:
                    continue
                recipient_key = reference(original_recipient)
                if recipient_key is None:
                    continue
                sender_key = reference(getattr(attribute, "sender_id", None))
                date = int(attribute.date)
                events.append(
                    GiftEvent(
                        key=f"original:{key}",
                        gift_key=key,
                        sender_key=sender_key,
                        recipient_key=recipient_key,
                        date=date,
                        source_peer_key=profile.key,
                        kind="original_gift",
                        source="original_details",
                        description=_gift_description(attribute, include_descriptions),
                    )
                )
                break
            continue

        sender_key = None
        if not getattr(saved, "name_hidden", False):
            sender_key = reference(getattr(saved, "from_id", None))
        date = int(saved.date)
        saved_id = getattr(saved, "saved_id", None)
        msg_id = getattr(saved, "msg_id", None)
        if saved_id is not None:
            occurrence = f"saved:{saved_id}"
        elif msg_id is not None:
            occurrence = f"message:{msg_id}"
        else:
            occurrence = "snapshot:" + _fingerprint(
                offset, index, key, sender_key, date, getattr(saved, "gift_num", None)
            )
        events.append(
            GiftEvent(
                key=f"{profile.key}:{occurrence}",
                gift_key=key,
                sender_key=sender_key,
                recipient_key=profile.key,
                date=date,
                source_peer_key=profile.key,
                description=_gift_description(saved, include_descriptions),
            )
        )

    return GiftPage(
        gifts=tuple(gifts.values()),
        events=tuple(events),
        ownerships=tuple(ownerships),
        peers=tuple(peers.values()),
        next_offset=getattr(response, "next_offset", None) or None,
        item_count=len(items),
    )


class TelegramSource:
    """Read an existing user session. Never starts an interactive login."""

    def __init__(
        self,
        session_path: Path,
        api_id: int | None = None,
        api_hash: str | None = None,
        *,
        include_descriptions: bool = False,
        request_delay: float = 0.0,
    ) -> None:
        path = Path(session_path).expanduser().resolve()
        self.session_path = (
            path if path.suffix == ".session" else Path(str(path) + ".session")
        )
        self.api_id = api_id
        self.api_hash = api_hash
        self.include_descriptions = include_descriptions
        self.request_delay = request_delay
        self._client: Any = None
        self._raw: Any = None
        self._errors: Any = None

    async def __aenter__(self) -> TelegramSource:
        if not self.session_path.is_file():
            raise RuntimeError(
                f"Нет сессии {self.session_path}. Сначала запустите generate_session.py."
            )
        validate_existing_session(self.session_path)
        try:
            from pyrogram import Client, errors, raw
        except ImportError as exc:
            raise RuntimeError(
                "Установите зависимости проекта: pip install -e ."
            ) from exc

        request = getattr(raw.functions.payments, "GetSavedStarGifts", None)
        if (
            request is None
            or "exclude_hosted" not in inspect.signature(request).parameters
        ):
            raise RuntimeError(
                "Установленная версия Kurigram не поддерживает актуальный gift API "
                "(exclude_hosted). Обновите Kurigram."
            )
        self._raw, self._errors = raw, errors
        self._client = read_only_client_class(Client, raw)(
            self.session_path.stem,
            workdir=str(self.session_path.parent),
            api_id=self.api_id,
            api_hash=self.api_hash,
            no_updates=True,
            sleep_threshold=0,
        )
        self._client.read_request_delay = self.request_delay
        try:
            authorized = await self._client.connect()
            if not authorized:
                raise RuntimeError(
                    "Сессия не авторизована. Запустите generate_session.py."
                )
            if await self._client.storage.is_bot():
                raise RuntimeError(
                    "Для сборщика нужна пользовательская сессия, а не сессия бота."
                )
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        counts = getattr(self._client, "read_request_counts", None)
        if counts:
            log.info("Read-only RPC: %s", dict(counts))

    async def _call(
        self, function: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        unavailable = {
            "PeerIdInvalid",
            "UserIdInvalid",
            "InputUserDeactivated",
            "UserDeactivated",
            "ChannelPrivate",
            "ChannelInvalid",
            "ChatIdInvalid",
            "ChatAdminRequired",
            "UsernameInvalid",
            "UsernameNotOccupied",
            "UserNotParticipant",
        }
        while True:
            try:
                return await function(*args, **kwargs)
            except self._errors.FloodWait as exc:
                seconds = max(0, int(exc.value)) + 1
                log.warning(
                    "Telegram FLOOD_WAIT: ждём %s с, затем повторяем запрос", seconds
                )
                await asyncio.sleep(seconds)
            except self._errors.RPCError as exc:
                if type(exc).__name__ in unavailable:
                    raise PeerUnavailable(type(exc).__name__) from exc
                raise

    async def _invoke(self, request: Any) -> Any:
        # Kurigram.invoke persists users/chats and their access hashes in its
        # private session cache; numeric peers remain resolvable after restart.
        return await self._call(self._client.invoke, request, sleep_threshold=0)

    @staticmethod
    def _telegram_id(peer: Peer) -> int:
        if peer.kind == "channel":
            return -1_000_000_000_000 - peer.id
        if peer.kind == "chat":
            return -peer.id
        return peer.id

    async def _input_peer(self, peer: Peer) -> Any:
        try:
            return await self._call(self._client.resolve_peer, self._telegram_id(peer))
        except PeerUnavailable:
            if not peer.username:
                raise
            candidate = await self._call(self._client.resolve_peer, peer.username)
            resolved = _peer_ref(candidate)
            if resolved is None or resolved.key != peer.key:
                raise PeerUnavailable(
                    "Username no longer belongs to this peer"
                ) from None
            return candidate

    async def resolve_peer(self, reference: str) -> Peer:
        if reference.startswith(("user:", "channel:", "chat:")):
            reference = str(self._telegram_id(Peer.from_key(reference)))
        if reference.startswith(("http://", "https://")):
            url = urlparse(reference)
            if url.hostname not in {"t.me", "telegram.me", "www.t.me"}:
                raise ValueError(
                    "Ожидается Telegram username, ID или публичная t.me-ссылка."
                )
            reference = url.path.strip("/")
            if "/" in reference or reference.startswith("+"):
                raise ValueError(
                    "Укажите username чата, к которому аккаунт уже имеет доступ."
                )
        query: str | int = (
            int(reference) if reference.lstrip("-").isdigit() else reference
        )
        value = await self._call(self._client.get_chat, query, force_full=False)
        peer_id = int(value.id)
        if peer_id <= -1_000_000_000_000:
            kind, peer_id = "channel", -peer_id - 1_000_000_000_000
        elif peer_id < 0:
            kind, peer_id = "chat", -peer_id
        else:
            kind = "user"
        entity = _entity(value, kind)
        return Peer(
            id=peer_id,
            kind=kind,
            name=entity.name,
            username=getattr(value, "username", None),
        )

    async def iter_members(self, chat: str, limit: int) -> AsyncIterator[Peer]:
        if limit < 1:
            raise ValueError("Member limit must be positive")
        profile = await self.resolve_peer(chat)
        if profile.kind == "user":
            raise ValueError("Для --chat укажите группу или канал, не пользователя.")
        peer = await self._input_peer(profile)
        seen: set[int] = set()
        if profile.kind == "chat":
            response = await self._invoke(
                self._raw.functions.messages.GetFullChat(chat_id=profile.id)
            )
            members = getattr(response.full_chat.participants, "participants", None)
            if members is None:
                raise PeerUnavailable("Chat participant list is unavailable")
            users = {int(user.id): user for user in response.users}
            for member in members:
                user = users.get(getattr(member, "user_id", None))
                if user is not None and user.id not in seen:
                    seen.add(user.id)
                    yield _entity(user, "user")
                    if len(seen) >= limit:
                        return
            return

        offset = 0
        while len(seen) < limit:
            response = await self._invoke(
                self._raw.functions.channels.GetParticipants(
                    channel=self._raw.types.InputChannel(
                        channel_id=peer.channel_id,
                        access_hash=peer.access_hash,
                    ),
                    filter=self._raw.types.ChannelParticipantsSearch(q=""),
                    offset=offset,
                    limit=min(200, limit - len(seen)),
                    hash=0,
                )
            )
            participants = response.participants
            if not participants:
                break
            users = {int(user.id): user for user in response.users}
            new_members = 0
            for member in participants:
                user_id = getattr(member, "user_id", None)
                user = users.get(user_id)
                if user is not None and user.id not in seen:
                    seen.add(user.id)
                    new_members += 1
                    yield _entity(user, "user")
                    if len(seen) >= limit:
                        return
            offset += len(participants)
            if not new_members or offset >= response.count:
                break

    async def fetch_gifts(self, peer: Peer, offset: str, limit: int) -> GiftPage:
        if not 1 <= limit <= 100:
            raise ValueError("Gift page size must be between 1 and 100")
        if peer.kind == "chat":
            raise PeerUnavailable("Basic groups do not have a gift profile")
        resolved = await self._input_peer(peer)
        response = await self._invoke(
            self._raw.functions.payments.GetSavedStarGifts(
                peer=resolved,
                offset=offset,
                limit=limit,
                exclude_unsaved=True,
                exclude_hosted=True,
            )
        )
        return normalize_gift_page(
            response, peer, offset, include_descriptions=self.include_descriptions
        )

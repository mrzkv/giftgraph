"""Small, entirely fictional dataset for running the crawler without Telegram."""

from dataclasses import dataclass

from .models import Gift, GiftEvent, GiftPage, Ownership, Peer

ANYA = Peer(1, name="Аня", username="demo_anya")
BORIS = Peer(2, name="Борис", username="demo_boris")
VERA = Peer(3, name="Вера", username="demo_vera")
DIMA = Peer(4, name="Дима", username="demo_dima")
EGOR = Peer(5, name="Егор", username="demo_egor")
DEMO_SEEDS = [ANYA]

HEART = Gift("regular:101", "regular", "101", title="Сердце ❤️")
BEAR = Gift("regular:102", "regular", "102", title="Мишка 🧸")
FLOWER = Gift("regular:103", "regular", "103", title="Цветок 🌷")
UNIQUE = Gift("unique:7001", "unique", "104", title="Звезда #7", slug="DemoStar-7")


@dataclass(frozen=True)
class _Item:
    gift: Gift
    event: GiftEvent | None = None
    ownership: Ownership | None = None


def _event(key: str, gift: Gift, sender: Peer | None, recipient: Peer, date: int) -> GiftEvent:
    return GiftEvent(
        key=key,
        gift_key=gift.key,
        sender_key=sender.key if sender else None,
        recipient_key=recipient.key,
        date=date,
        source_peer_key=recipient.key,
    )


class DemoSource:
    """Matches the real provider's cursor pagination and incoming-gift semantics.

    At depth 1, Boris is discovered from Anya. At depth 2, Vera is discovered
    from Boris. The original Vera -> Dima gift currently owned by Anya must not
    be mistaken for Vera -> Anya or expand the BFS from Anya to Dima.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._peers = {peer.key: peer for peer in (ANYA, BORIS, VERA, DIMA, EGOR)}
        self._profiles: dict[str, tuple[_Item, ...]] = {
            ANYA.key: (
                _Item(HEART, _event("demo:a:1", HEART, BORIS, ANYA, 1794787200)),
                # Distinct gifts can have the same sender, catalog ID and second.
                _Item(HEART, _event("demo:a:2", HEART, BORIS, ANYA, 1794787200)),
                _Item(BEAR, _event("demo:a:3", BEAR, None, ANYA, 1794787300)),
                _Item(
                    UNIQUE,
                    GiftEvent(
                        key="demo:original:7001",
                        gift_key=UNIQUE.key,
                        sender_key=VERA.key,
                        recipient_key=DIMA.key,
                        date=1794700000,
                        source_peer_key=ANYA.key,
                        kind="original_gift",
                        source="original_details",
                    ),
                    Ownership(UNIQUE.key, ANYA.key, ANYA.key),
                ),
            ),
            BORIS.key: (
                _Item(FLOWER, _event("demo:b:1", FLOWER, VERA, BORIS, 1794780000)),
                _Item(BEAR, _event("demo:b:2", BEAR, ANYA, BORIS, 1794780100)),
            ),
            VERA.key: (_Item(HEART, _event("demo:c:1", HEART, EGOR, VERA, 1794770000)),),
        }

    async def fetch_gifts(self, peer: Peer, offset: str, limit: int) -> GiftPage:
        if limit < 1:
            raise ValueError("limit must be positive")
        start = int(offset or "0")
        if start < 0:
            raise ValueError("offset must be non-negative")
        self.calls.append((peer.key, offset, limit))
        items = self._profiles.get(peer.key, ())
        page = items[start : start + limit]
        next_start = start + len(page)
        related = {peer.key}
        for item in page:
            if item.event:
                related.add(item.event.recipient_key)
                if item.event.sender_key:
                    related.add(item.event.sender_key)
            if item.ownership and item.ownership.owner_key:
                related.add(item.ownership.owner_key)
        return GiftPage(
            gifts=tuple(item.gift for item in page),
            events=tuple(item.event for item in page if item.event is not None),
            ownerships=tuple(item.ownership for item in page if item.ownership is not None),
            peers=tuple(self._peers.get(key, Peer.from_key(key)) for key in sorted(related)),
            next_offset=str(next_start) if next_start < len(items) else None,
            item_count=len(page),
        )

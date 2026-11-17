"""Provider-independent data model. Telegram IDs are namespaced by peer type."""

import math
from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Peer:
    id: int
    kind: Literal["user", "channel", "chat"] = "user"
    name: str = ""
    username: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def from_key(cls, key: str) -> "Peer":
        kind, value = key.split(":", 1)
        if kind not in {"user", "channel", "chat"}:
            raise ValueError(f"Unknown peer kind: {kind}")
        return cls(id=int(value), kind=kind)


@dataclass(frozen=True)
class Gift:
    key: str  # regular:<catalog id> or unique:<unique id>
    kind: Literal["regular", "unique"]
    catalog_id: str
    title: str = ""
    slug: str | None = None
    stars: int | None = None
    number: int | None = None


@dataclass(frozen=True)
class GiftEvent:
    key: str  # Idempotent within one run; not necessarily a global Telegram event ID.
    gift_key: str
    sender_key: str | None
    recipient_key: str
    date: int
    source_peer_key: str
    kind: Literal["gift", "original_gift"] = "gift"
    source: str = "profile"
    description: str | None = None


@dataclass(frozen=True)
class Ownership:
    gift_key: str
    owner_key: str | None
    source_peer_key: str
    host_key: str | None = None


@dataclass(frozen=True)
class GiftPage:
    gifts: tuple[Gift, ...] = ()
    events: tuple[GiftEvent, ...] = ()
    ownerships: tuple[Ownership, ...] = ()
    peers: tuple[Peer, ...] = ()
    next_offset: str | None = None
    item_count: int = 0  # Number of raw gifts, including gifts without public senders.


@dataclass(frozen=True)
class CrawlConfig:
    max_depth: int = 3
    max_nodes: int = 3000
    max_gifts_per_profile: int = 1000
    max_pages: int = 2000
    page_size: int = 100
    request_delay: float = 1.0

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        for key in ("max_nodes", "max_gifts_per_profile", "max_pages", "page_size"):
            if getattr(self, key) < 1:
                raise ValueError(f"{key} must be positive")
        if self.page_size > 100:
            raise ValueError("page_size must be at most 100")
        if not math.isfinite(self.request_delay) or self.request_delay < 0:
            raise ValueError("request_delay must be finite and non-negative")


class PeerUnavailable(Exception):
    """This peer cannot be read with the current account's access rights."""


class GiftSource(Protocol):
    async def fetch_gifts(self, peer: Peer, offset: str, limit: int) -> GiftPage: ...


@dataclass
class CrawlStats:
    run_id: int
    status: str
    nodes: int = 0
    pages: int = 0
    events: int = 0
    states: dict[str, int] = field(default_factory=dict)

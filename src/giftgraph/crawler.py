"""Breadth-first traversal of confirmed incoming gift relationships."""

import asyncio
import logging

from .models import CrawlStats, GiftSource, Peer, PeerUnavailable
from .store import Store

log = logging.getLogger(__name__)


async def crawl(store: Store, source: GiftSource, run_id: int) -> CrawlStats:
    config = store.config(run_id)
    store.set_status(run_id, "running")
    pages_this_invocation = 0
    try:
        while node := store.next_node(run_id):
            if pages_this_invocation >= config.max_pages:
                store.set_status(run_id, "paused", "Page budget reached; use resume")
                return store.stats(run_id)
            peer = Peer(int(node["id"]), node["kind"], node["name"], node["username"])
            limit = min(config.page_size, config.max_gifts_per_profile - node["gifts_seen"])
            log.info(
                "Profile %s, depth %s, gifts read %s", peer.key, node["depth"], node["gifts_seen"]
            )
            try:
                page = await source.fetch_gifts(peer, node["offset"], limit)
            except PeerUnavailable as exc:
                store.mark_unavailable(run_id, peer.key, str(exc))
                log.warning("Cannot read %s: %s", peer.key, exc)
                continue
            if page.item_count > limit:
                raise ValueError("Provider returned more gifts than the requested limit")
            store.save_page(run_id, node, page)
            pages_this_invocation += 1
            if config.request_delay:
                await asyncio.sleep(config.request_delay)
        store.finish(run_id)
    except (KeyboardInterrupt, asyncio.CancelledError):
        store.set_status(run_id, "paused", "Interrupted; use resume")
        raise
    except Exception as exc:
        store.set_status(run_id, "failed", type(exc).__name__)
        raise
    return store.stats(run_id)

from dataclasses import replace

import pytest

from giftgraph.crawler import crawl
from giftgraph.demo import ANYA, BORIS, DEMO_SEEDS, DemoSource
from giftgraph.models import CrawlConfig, GiftEvent, GiftPage, Peer, PeerUnavailable
from giftgraph.store import Store


def config(**kwargs):
    kwargs.setdefault("max_depth", 2)
    return CrawlConfig(request_delay=0, **kwargs)


@pytest.mark.parametrize(
    ("depth", "node_count", "edge_count", "queried"),
    [
        (0, 1, 0, set()),
        (1, 2, 2, {"user:1"}),
        (2, 3, 4, {"user:1", "user:2"}),
        (3, 4, 5, {"user:1", "user:2", "user:3"}),
    ],
)
async def test_strict_depth_direction_cycles_and_multiple_gifts(
    tmp_path,
    depth,
    node_count,
    edge_count,
    queried,
):
    source = DemoSource()
    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run(DEMO_SEEDS, config(max_depth=depth, page_size=1))
        result = await crawl(store, source, run_id)
        graph = store.graph_data(run_id)
    assert result.status == "complete"
    assert len(graph["nodes"]) == node_count
    assert len(graph["edges"]) == edge_count
    assert max(node["depth"] for node in graph["nodes"]) <= depth
    assert {call[0] for call in source.calls} == queried
    assert all(edge["source"] != edge["target"] for edge in graph["edges"])
    # Original recipient Dima is unrelated to the visible incoming-gift traversal.
    assert "user:4" not in {node["id"] for node in graph["nodes"]}
    if depth:
        assert len(graph["anonymous_events"]) == 1
        assert len(graph["ownerships"]) == 1
        assert not any(e["gift_key"] == "unique:7001" for e in graph["edges"])


async def test_page_budget_and_resume_from_checkpoint_after_database_reopen(tmp_path):
    path = tmp_path / "test.db"
    with Store(path) as store:
        run_id = store.create_run(DEMO_SEEDS, config(page_size=1, max_pages=1), source="demo")
        result = await crawl(store, DemoSource(), run_id)
        assert result.status == "paused"
        assert result.pages == 1
        assert store.next_node(run_id)["offset"] == "1"
    source = DemoSource()
    with Store(path) as store:
        while store.next_node(run_id):
            await crawl(store, source, run_id)
        graph = store.graph_data(run_id)
        assert store.stats(run_id).status == "complete"
        assert store.stats(run_id).pages == 6
        assert store.stats(run_id).events == 6
    assert source.calls[0] == ("user:1", "1", 1)
    assert len(graph["edges"]) == 4


async def test_network_failure_preserves_cursor_and_resumes_without_duplicate_events(tmp_path):
    class BrokenSource(DemoSource):
        async def fetch_gifts(self, peer, offset, limit):
            if offset == "1":
                raise ConnectionError("Temporary network failure")
            return await super().fetch_gifts(peer, offset, limit)

    path = tmp_path / "test.db"
    with Store(path) as store:
        run_id = store.create_run(DEMO_SEEDS, config(page_size=1))
        with pytest.raises(ConnectionError):
            await crawl(store, BrokenSource(), run_id)
        assert store.stats(run_id).status == "failed"
        assert store.next_node(run_id)["offset"] == "1"
    with Store(path) as store:
        await crawl(store, DemoSource(), run_id)
        assert store.stats(run_id).events == 6
        assert len(store.graph_data(run_id)["edges"]) == 4


async def test_inaccessible_profile_is_recorded_without_losing_other_work(tmp_path):
    class PrivateSource(DemoSource):
        async def fetch_gifts(self, peer, offset, limit):
            if peer == BORIS:
                raise PeerUnavailable("Private profile")
            return await super().fetch_gifts(peer, offset, limit)

    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run(DEMO_SEEDS, config())
        result = await crawl(store, PrivateSource(), run_id)
        assert result.status == "partial"
        assert result.states["unavailable"] == 1
        assert len(store.graph_data(run_id)["edges"]) == 2


async def test_node_and_profile_gift_budgets_are_hard_limits(tmp_path):
    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run(DEMO_SEEDS, config(max_nodes=1))
        result = await crawl(store, DemoSource(), run_id)
        assert result.nodes == 1
        assert result.status == "partial"
        assert store.graph_data(run_id)["metadata"]["node_limit_hit"] is True
        run_id = store.create_run(DEMO_SEEDS, config(max_gifts_per_profile=2))
        source = DemoSource()
        result = await crawl(store, source, run_id)
        assert result.status == "partial"
        assert result.states["truncated"] == 1
        assert source.calls[0][2] == 2
        assert not store.graph_data(run_id)["anonymous_events"]


async def test_all_seeds_start_at_zero_even_if_they_are_also_donors(tmp_path):
    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run([ANYA, BORIS, ANYA], config(max_depth=1))
        await crawl(store, DemoSource(), run_id)
        nodes = {node["id"]: node for node in store.graph_data(run_id)["nodes"]}
        assert nodes[ANYA.key]["depth"] == nodes[BORIS.key]["depth"] == 0
        assert nodes["user:3"]["depth"] == 1


async def test_events_are_snapshots_not_accumulated_across_runs(tmp_path):
    with Store(tmp_path / "test.db") as store:
        for _ in range(2):
            run_id = store.create_run(DEMO_SEEDS, config())
            await crawl(store, DemoSource(), run_id)
            assert len(store.graph_data(run_id)["edges"]) == 4


async def test_page_persistence_is_atomic_on_invalid_evidence(tmp_path):
    page = await DemoSource().fetch_gifts(ANYA, "", 1)
    bad_event = GiftEvent("bad", "regular:101", "invalid:1", ANYA.key, 1, ANYA.key)
    page = replace(page, events=(*page.events, bad_event))
    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run(DEMO_SEEDS, config())
        with pytest.raises(ValueError, match="Unknown peer kind"):
            store.save_page(run_id, store.next_node(run_id), page)
        assert store.stats(run_id).events == 0
        assert store.stats(run_id).nodes == 1
        assert store.stats(run_id).pages == 0
        assert store.next_node(run_id)["offset"] == ""


async def test_replayed_page_is_idempotent(tmp_path):
    page = await DemoSource().fetch_gifts(ANYA, "", 1)
    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run(DEMO_SEEDS, config())
        node = store.next_node(run_id)
        store.save_page(run_id, node, page)
        store.save_page(run_id, node, page)
        assert store.stats(run_id).events == 1
        assert store.stats(run_id).pages == 1


async def test_pagination_cycle_fails_instead_of_infinite_loop(tmp_path):
    class CyclicSource:
        async def fetch_gifts(self, peer, offset, limit):
            return GiftPage(next_offset="a", item_count=0)

    with Store(tmp_path / "test.db") as store:
        run_id = store.create_run([Peer(1)], config())
        with pytest.raises(ValueError, match="pagination cycle"):
            await crawl(store, CyclicSource(), run_id)
        assert store.stats(run_id).status == "failed"
        assert store.stats(run_id).pages == 1


def test_invalid_configuration_and_too_many_seeds_rejected(tmp_path):
    with pytest.raises(ValueError):
        config(max_depth=-1)
    with pytest.raises(ValueError):
        config(page_size=101)
    with Store(tmp_path / "test.db") as store:
        with pytest.raises(ValueError, match="Seed count"):
            store.create_run([ANYA, BORIS], config(max_nodes=1))

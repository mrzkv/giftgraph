import json
import sqlite3
from xml.etree import ElementTree as ET

import pytest

from giftgraph.export import GRAPHML_NS, export_gift_descriptions, export_graph
from giftgraph.models import CrawlConfig, Gift, GiftEvent, GiftPage, Peer
from giftgraph.store import Store


def save_gifts(store, gifts, events):
    recipient = Peer(9007199254740993, username="recipient")
    sender = Peer(9007199254740995, username="sender")
    run_id = store.create_run([recipient], CrawlConfig(max_depth=1, request_delay=0))
    store.save_page(
        run_id,
        store.next_node(run_id),
        GiftPage(
            gifts=tuple(gifts),
            events=tuple(events),
            peers=(recipient, sender),
            item_count=len(events),
        ),
    )
    return run_id


def event(key, gift_key, description=None):
    return GiftEvent(
        key=key,
        gift_key=gift_key,
        sender_key="user:9007199254740995",
        recipient_key="user:9007199254740993",
        date=1794787200,
        source_peer_key="user:9007199254740993",
        description=description,
    )


def test_graph_has_requested_fields_and_preserves_unknown_prices(tmp_path):
    regular = Gift(
        "regular:111222333444555666", "regular", "111222333444555666", "Мишка 🧸", stars=50
    )
    unique = Gift(
        "unique:9007199254740999",
        "unique",
        "catalog",
        "Plush Pepe #123",
        "PlushPepe-123",
        number=123,
    )
    with Store(tmp_path / "graph.db") as store:
        run_id = save_gifts(
            store, [regular, unique], [event("a", regular.key), event("b", unique.key)]
        )
        graph = store.graph_data(run_id)
    assert [(n["user_id"], n["username"], n["depth"]) for n in graph["nodes"]] == [
        ("9007199254740993", "recipient", 0),
        ("9007199254740995", "sender", 1),
    ]
    first, second = graph["edges"]
    assert first["sender_id"] == "9007199254740995"
    assert first["recipient_id"] == "9007199254740993"
    assert first["gift_id"] == "111222333444555666"
    assert first["gift_key"] == regular.key
    assert first["date"] == 1794787200
    assert first["stars"] == 50
    assert first["gift_title"] == "Мишка 🧸"
    assert second["gift_number"] == 123
    assert second["gift_title"] == "Plush Pepe #123"
    assert second["stars"] is None
    assert "historical" in graph["metadata"]["price_semantics"]
    output = tmp_path / "graph.json"
    export_graph(graph, output)
    assert json.loads(output.read_text())["edges"][1]["stars"] is None
    output = tmp_path / "graph.graphml"
    export_graph(graph, output)
    ns = {"g": GRAPHML_NS}
    root = ET.parse(output).getroot()
    assert root.find("g:key[@id='node_user_id']", ns).attrib["attr.type"] == "string"
    assert root.find("g:key[@id='edge_stars']", ns).attrib["attr.type"] == "long"
    edges = root.findall("g:graph/g:edge", ns)
    assert edges[0].find("g:data[@key='edge_sender_id']", ns).text == "9007199254740995"
    assert edges[0].find("g:data[@key='edge_stars']", ns).text == "50"
    assert edges[1].find("g:data[@key='edge_stars']", ns) is None


def test_description_sidecar_keeps_different_notes_for_repeated_gifts(tmp_path):
    gift = Gift("regular:1", "regular", "1", "Сердце")
    with Store(tmp_path / "graph.db") as store:
        run_id = save_gifts(
            store,
            [gift],
            [
                event("a", gift.key, "С днём рождения ❤️"),
                event("b", gift.key, "Спасибо!"),
                event("c", gift.key),
            ],
        )
        graph = store.graph_data(run_id)
        descriptions = store.gift_descriptions(run_id)
    assert descriptions == {"a": "С днём рождения ❤️", "b": "Спасибо!"}
    assert "С днём рождения" not in json.dumps(graph, ensure_ascii=False)
    output = tmp_path / "notes" / "graph.gift_descriptions.json"
    export_gift_descriptions(descriptions, output)
    assert json.loads(output.read_text()) == descriptions
    assert "С днём рождения ❤️" in output.read_text()


def test_legacy_database_migrates_and_reopens_without_losing_gifts(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE gifts(key TEXT PRIMARY KEY,kind TEXT NOT NULL,catalog_id TEXT NOT NULL,
                title TEXT NOT NULL,slug TEXT);
            CREATE TABLE events(run_id INTEGER NOT NULL,key TEXT NOT NULL,gift_key TEXT NOT NULL,
                sender_key TEXT,recipient_key TEXT NOT NULL,date INTEGER NOT NULL,
                source_peer_key TEXT NOT NULL,kind TEXT NOT NULL,source TEXT NOT NULL,
                PRIMARY KEY(run_id,key));
            INSERT INTO gifts VALUES ('regular:1','regular','1','Сердце',NULL);
        """)
    with Store(path) as store:
        run_id = store.create_run([Peer(1), Peer(2)], CrawlConfig(max_depth=0))
        store.db.execute(
            """INSERT INTO events(run_id,key,gift_key,sender_key,recipient_key,date,
                source_peer_key,kind,source) VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, "old", "regular:1", "user:2", "user:1", 123, "user:1", "gift", "profile"),
        )
        store.db.commit()
    with Store(path) as store:
        edge = store.graph_data(run_id)["edges"][0]
        assert (edge["gift_id"], edge["gift_title"], edge["stars"], edge["gift_number"]) == (
            "1",
            "Сердце",
            None,
            None,
        )
        assert store.gift_descriptions(run_id) == {}


def test_channel_gift_sender_is_observed_without_entering_user_graph(tmp_path):
    gift = Gift("regular:1", "regular", "1")
    with Store(tmp_path / "graph.db") as store:
        run_id = store.create_run([Peer(1)], CrawlConfig(request_delay=0))
        store.save_page(
            run_id,
            store.next_node(run_id),
            GiftPage(
                gifts=(gift,),
                events=(GiftEvent("channel", gift.key, "channel:2", "user:1", 123, "user:1"),),
                item_count=1,
            ),
        )
        graph = store.graph_data(run_id)
        assert len(graph["nodes"]) == 1
        assert graph["edges"] == []
        assert graph["metadata"]["events_outside_graph"] == 1
        assert store.stats(run_id).events == 1
        with pytest.raises(ValueError, match="must be users"):
            store.create_run([Peer(2, kind="channel")], CrawlConfig())


def test_requested_default_limits():
    config = CrawlConfig()
    assert config.max_depth == 3
    assert config.max_nodes == 3000
    assert config.max_gifts_per_profile == 1000
    assert config.max_pages == 2000
    assert config.request_delay == 1

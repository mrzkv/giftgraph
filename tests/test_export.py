import json
from xml.etree import ElementTree as ET

import pytest

from giftgraph.demo import ANYA, BORIS, DEMO_SEEDS, DemoSource
from giftgraph.export import GRAPHML_NS, export_graph


@pytest.fixture
def graph():
    return {
        "metadata": {"run_id": 1, "status": "complete", "notice": "Данные <демо> & тест"},
        "nodes": [
            {
                "id": "user:9007199254740993",
                "label": '<script>alert("привет")</script> & ❤️',
                "kind": "user",
                "username": None,
                "depth": 0,
                "scan_state": "done",
            },
            {
                "id": "user:2",
                "label": "Борис",
                "kind": "user",
                "username": "boris",
                "depth": 1,
                "scan_state": "boundary",
            },
        ],
        "edges": [
            {
                "id": "event:a",
                "source": "user:2",
                "target": "user:9007199254740993",
                "gift_id": "regular:111222333444555666",
                "gift_title": 'Сердце <3 & "❤"',
                "gift_slug": None,
                "date": 1794787200,
                "kind": "gift",
            },
            {
                "id": "event:b",
                "source": "user:2",
                "target": "user:9007199254740993",
                "gift_id": "regular:111222333444555666",
                "gift_title": "Сердце",
                "gift_slug": None,
                "date": 1794787200,
                "kind": "gift",
            },
        ],
        "ownerships": [{"gift_key": "unique:9", "owner_key": "user:2"}],
        "anonymous_events": [{"id": "anonymous:1", "recipient_key": "user:2"}],
    }


def test_json_round_trip_preserves_unicode_big_ids_and_parallel_edges(tmp_path, graph):
    output = tmp_path / "nested" / "graph.json"
    export_graph(graph, output)
    assert json.loads(output.read_text()) == graph
    assert "Борис" in output.read_text()
    assert len(json.loads(output.read_text())["edges"]) == 2


def test_graphml_is_directed_multigraph_and_escapes_values(tmp_path, graph):
    output = tmp_path / "graph.graphml"
    export_graph(graph, output)
    ns = {"g": GRAPHML_NS}
    root = ET.parse(output).getroot()
    network = root.find("g:graph", ns)
    assert network is not None
    assert network.attrib["edgedefault"] == "directed"
    assert len(network.findall("g:node", ns)) == 2
    edges = network.findall("g:edge", ns)
    assert len(edges) == 2
    assert len({edge.attrib["id"] for edge in edges}) == 2
    assert all(edge.attrib["source"] == "user:2" for edge in edges)
    assert all(edge.attrib["target"] == "user:9007199254740993" for edge in edges)
    label = network.find("g:node/g:data[@key='node_label']", ns)
    assert label is not None
    assert label.text == graph["nodes"][0]["label"]
    assert root.find(".//script") is None
    owner_data = network.find("g:data[@key='graph_ownerships']", ns)
    anonymous_data = network.find("g:data[@key='graph_anonymous_events']", ns)
    assert owner_data is not None and anonymous_data is not None
    assert json.loads(owner_data.text) == graph["ownerships"]
    assert json.loads(anonymous_data.text) == graph["anonymous_events"]


def test_forbidden_xml_control_character_does_not_corrupt_export(tmp_path, graph):
    graph["nodes"][0]["label"] = "name\x00tail"
    output = tmp_path / "graph.graphml"
    export_graph(graph, output)
    ET.parse(output)
    assert "name\ufffdtail" in output.read_text()


def test_export_rejects_missing_endpoint_without_overwriting_existing_file(tmp_path, graph):
    output = tmp_path / "graph.json"
    output.write_text("existing graph")
    graph["edges"][0]["target"] = "user:missing"
    with pytest.raises(ValueError, match="missing from the graph"):
        export_graph(graph, output)
    assert output.read_text() == "existing graph"


def test_unsupported_format_does_not_create_output_directory(tmp_path, graph):
    output = tmp_path / "nested" / "graph.html"
    with pytest.raises(ValueError, match=".json or .graphml"):
        export_graph(graph, output)
    assert not output.parent.exists()


async def test_demo_paginates_preserves_occurrences_and_separates_ownership():
    source = DemoSource()
    pages = []
    offset = ""
    while True:
        page = await source.fetch_gifts(DEMO_SEEDS[0], offset, 1)
        pages.append(page)
        if not page.next_offset:
            break
        offset = page.next_offset
    assert len(pages) == 4
    assert all(page.item_count == 1 for page in pages)
    events = [event for page in pages for event in page.events]
    incoming = [event for event in events if event.recipient_key == ANYA.key]
    assert len(incoming) == 3
    assert [event.sender_key for event in incoming] == [BORIS.key, BORIS.key, None]
    assert len({event.key for event in incoming}) == 3
    original = next(event for event in events if event.kind == "original_gift")
    assert original.recipient_key != ANYA.key
    assert pages[-1].ownerships[0].owner_key == ANYA.key


async def test_demo_same_cursor_is_retry_safe():
    source = DemoSource()
    first = await source.fetch_gifts(ANYA, "1", 2)
    retried = await source.fetch_gifts(ANYA, "1", 2)
    assert first == retried

"""Portable exports without network access or visualization dependencies."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
ET.register_namespace("", GRAPHML_NS)


def _tag(name: str) -> str:
    return f"{{{GRAPHML_NS}}}{name}"


def _xml_text(value: Any) -> str:
    """Keep Unicode while replacing characters forbidden by XML 1.0."""
    return "".join(
        char
        if (
            char in "\t\n\r"
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
        else "\ufffd"
        for char in str(value)
    )


def _prepare(data: dict[str, Any]) -> dict[str, Any]:
    """Keep Telegram identifiers lossless in JSON and validate graph endpoints."""
    result = dict(data)
    result["nodes"] = [dict(node, id=str(node["id"])) for node in data.get("nodes", [])]
    result["edges"] = [
        dict(
            edge,
            id=str(edge["id"]),
            source=str(edge["source"]),
            target=str(edge["target"]),
            gift_id=str(edge["gift_id"]),
        )
        for edge in data.get("edges", [])
    ]
    for node in result["nodes"]:
        if node.get("user_id") is not None:
            node["user_id"] = str(node["user_id"])
    for edge in result["edges"]:
        for field in ("sender_id", "recipient_id"):
            if edge.get(field) is not None:
                edge[field] = str(edge[field])
    node_ids = {node["id"] for node in result["nodes"]}
    if len(node_ids) != len(result["nodes"]):
        raise ValueError("Graph contains duplicate node IDs")
    for edge in result["edges"]:
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            raise ValueError(f"Edge {edge['id']} refers to a node missing from the graph")
    return result


def _graphml(data: dict[str, Any]) -> str:
    root = ET.Element(_tag("graphml"))
    # Original event IDs are edge attributes. Generated XML edge IDs stay unique
    # even when a consumer supplies two edges with the same event identifier.
    node_fields = {
        "user_id": "string",
        "label": "string",
        "kind": "string",
        "username": "string",
        "depth": "int",
        "scan_state": "string",
    }
    edge_fields = {
        "id": "string",
        "sender_id": "string",
        "recipient_id": "string",
        "gift_id": "string",
        "gift_key": "string",
        "gift_title": "string",
        "gift_slug": "string",
        "gift_number": "long",
        "stars": "long",
        "date": "long",
        "kind": "string",
    }
    graph_fields = ("metadata", "ownerships", "anonymous_events")
    for scope, fields in (("node", node_fields), ("edge", edge_fields)):
        for field, field_type in fields.items():
            ET.SubElement(
                root,
                _tag("key"),
                {
                    "id": f"{scope}_{field}",
                    "for": scope,
                    "attr.name": field,
                    "attr.type": field_type,
                },
            )
    for field in graph_fields:
        ET.SubElement(
            root,
            _tag("key"),
            {
                "id": f"graph_{field}",
                "for": "graph",
                "attr.name": field,
                "attr.type": "string",
            },
        )
    graph = ET.SubElement(root, _tag("graph"), id="gift_graph", edgedefault="directed")
    for field in graph_fields:
        default: dict | list = {} if field == "metadata" else []
        ET.SubElement(graph, _tag("data"), key=f"graph_{field}").text = _xml_text(
            json.dumps(data.get(field, default), ensure_ascii=False, sort_keys=True)
        )
    for node in data["nodes"]:
        element = ET.SubElement(graph, _tag("node"), id=_xml_text(node["id"]))
        for field in node_fields:
            if node.get(field) is not None:
                ET.SubElement(element, _tag("data"), key=f"node_{field}").text = _xml_text(
                    node[field]
                )
    for index, edge in enumerate(data["edges"]):
        element = ET.SubElement(
            graph,
            _tag("edge"),
            id=f"e{index}",
            source=_xml_text(edge["source"]),
            target=_xml_text(edge["target"]),
        )
        for field in edge_fields:
            if edge.get(field) is not None:
                ET.SubElement(element, _tag("data"), key=f"edge_{field}").text = _xml_text(
                    edge[field]
                )
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def export_graph(data: dict[str, Any], path: Path) -> None:
    """Write a directed multigraph as JSON or GraphML, replacing the file atomically.

    GraphML stores anonymous gifts and ownership snapshots in graph attributes,
    so graph tools cannot accidentally interpret them as social connections.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in {".json", ".graphml"}:
        raise ValueError("Export path must end in .json or .graphml")
    prepared = _prepare(data)
    if suffix == ".json":
        content = json.dumps(prepared, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        content = _graphml(prepared)
    _atomic_text(path, content)


def export_gift_descriptions(data: dict[str, str], path: Path) -> None:
    """Write the separate opt-in event ID to gift-note mapping as UTF-8 JSON."""
    _atomic_text(Path(path), json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary_path = Path(output.name)
            output.write(content + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

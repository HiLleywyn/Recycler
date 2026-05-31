"""V3 Pillar 3 unit tests."""
from __future__ import annotations

from services.clan_wars import NODES, NODE_BY_ID, NODE_IDS


def test_twelve_nodes() -> None:
    assert len(NODES) == 12


def test_nodes_have_ids_labels_sources() -> None:
    for node in NODES:
        assert node["id"]
        assert node["label"]
        assert node["source"]


def test_node_ids_unique() -> None:
    assert len(set(NODE_IDS)) == len(NODE_IDS)


def test_node_by_id_lookup_consistent() -> None:
    for nid in NODE_IDS:
        assert NODE_BY_ID[nid]["id"] == nid


def test_apex_node_has_extra_weight() -> None:
    assert NODE_BY_ID["apex"].get("weight", 1.0) > 1.0


def test_war_map_renders() -> None:
    from services.war_render import render_war_map
    nodes = [
        {"node_id": nid, "a_score": 5 * (i + 1), "b_score": 3 * (i + 1)}
        for i, nid in enumerate(NODE_IDS)
    ]
    match = {"id": 1, "group_a_id": 1, "group_b_id": 2,
             "started_at": None, "ends_at": None}
    png = render_war_map(
        match, nodes,
        group_a_name="Hawks", group_b_name="Foxes",
        time_remaining_sec=3 * 86400,
    )
    assert png.startswith(b"\x89PNG")
    assert len(png) > 8000


def test_war_map_empty_state() -> None:
    from services.war_render import render_war_map
    match = {"id": 1, "group_a_id": 1, "group_b_id": 2,
             "started_at": None, "ends_at": None}
    png = render_war_map(match, [], group_a_name="A", group_b_name="B",
                        time_remaining_sec=0)
    assert png.startswith(b"\x89PNG")

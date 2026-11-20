from datetime import datetime

import polars as pl
import pytest

from graph_platform.graph import GraphStore, graph_dir, named_query

A = "alpha_group"


NOW = datetime(2100, 1, 1)
NS = "pytest_alpha"  # tenant label used inside Neo4j, so a developer's own graph is never touched


@pytest.fixture(scope="module")
def store(built):
    """Needs the local Neo4j (scripts/neo4j-local.sh start)."""
    import os
    import shutil

    import yaml
    from neo4j.exceptions import ServiceUnavailable

    from tests.conftest import ROOT

    secret = ROOT / ".tools/neo4j-password"
    if "GP_NEO4J_PASSWORD" not in os.environ and secret.exists():
        os.environ["GP_NEO4J_PASSWORD"] = secret.read_text()
    if "GP_NEO4J_PASSWORD" not in os.environ:
        pytest.skip("Neo4j is not set up: run scripts/neo4j-local.sh start")
    cfg = yaml.safe_load((ROOT / "conf/graph_db.yaml").read_text())
    cfg["import_dir"] = str(ROOT / cfg["import_dir"])
    (built.conf_dir / "graph_db.yaml").write_text(yaml.safe_dump(cfg))
    s = GraphStore(built)
    try:
        counts = {NS: s.load(NS, graph_dir(built, A)), "pytest_beta": s.load("pytest_beta", graph_dir(built, "beta_group"))}
    except ServiceUnavailable:
        pytest.skip("Neo4j is not running: run scripts/neo4j-local.sh start")
    yield s, counts[NS]
    for ns in counts:
        s._run("MATCH (n {tenant: $t}) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 20000 ROWS", t=ns)
        shutil.rmtree(s.import_dir / ns, ignore_errors=True)
    s.close()


def _edges(built, name):
    return pl.read_parquet(graph_dir(built, A) / "edges" / f"{name}.parquet")


def test_party_endpoints_are_global_ids(built):
    party = pl.read_parquet(graph_dir(built, A) / "nodes/Party.parquet")
    assert party["node_id"].str.starts_with("gp_").all() and party["node_id"].n_unique() == party.height
    assert "full_name" not in party.columns  # PII stays out of the shared graph
    tx = _edges(built, "TRANSACTS")
    assert tx["src_id"].str.starts_with("gp_").all() and tx["dst_id"].str.starts_with("gp_").all()
    assert tx["src_id"].is_in(party["node_id"].implode()).all()
    assert party.filter(pl.col("source_cnt") >= 3).height > 500


def test_store_matches_parquet(built, store):
    _, counts = store
    for kind in ("nodes", "edges"):
        for f in (graph_dir(built, A) / kind).glob("*.parquet"):
            rows = pl.scan_parquet(f).select(pl.len()).collect().item()
            assert counts[f.stem] >= 0.99 * rows and counts[f.stem] <= rows, f.stem
    assert counts["Party"] == pl.scan_parquet(graph_dir(built, A) / "nodes/Party.parquet").select(pl.len()).collect().item()


def test_named_queries(built, store):
    s, _ = store
    tx = _edges(built, "TRANSACTS")
    hub = tx.group_by("src_id").len().sort("len", descending=True)["src_id"][0]
    near = s.query(NS, named_query("party_neighbors"), {"party": hub, "as_of": NOW})
    assert near.height > 0 and "TRANSACTS" in set(near["edge_type"])

    rings = pl.read_parquet(built.data_dir / A / "ground_truth/rings.parquet")
    ids = pl.read_parquet(built.data_dir / A / "ground_truth/party_ids.parquet").select(
        "person", node_id=pl.format("{}:Party:{}", "source", "source_pk")
    )
    resolved = pl.read_parquet(built.data_dir / A / "shared/party_resolved.parquet")
    ring = rings.filter(pl.col("ring_id") == 0).join(ids, on="person").join(resolved, on="node_id")
    members = ring["global_party_id"].unique().to_list()
    shared = s.query(NS, named_query("party_sharers"), {"party": members[0], "as_of": NOW})
    assert set(shared["node_id"]) & set(members[1:])

    src, dst = tx["src_id"][0], tx["dst_id"][0]
    path = s.query(NS, named_query("money_path"), {"src": src, "dst": dst})
    assert path.filter(pl.col("hops") == 1)["path_cnt"][0] >= 1


def test_tenants_are_separated_inside_neo4j(built, store):
    s, _ = store
    hub = _edges(built, "TRANSACTS")["src_id"][0]
    assert s.query("pytest_beta", named_query("party_neighbors"), {"party": hub, "as_of": NOW}).height == 0
    shared_ids = s._run(
        "MATCH (a:Identifier {tenant: $a}), (b:Identifier {tenant: $b}) WHERE a.node_id = b.node_id RETURN count(*) AS c",
        a=NS, b="pytest_beta",
    )  # fmt: skip
    crossing = s._run("MATCH (a {tenant: $a})--(b {tenant: $b}) RETURN count(*) AS c", a=NS, b="pytest_beta")
    assert crossing[0]["c"] == 0, shared_ids
    amount = s._run("MATCH (:Party {tenant: $a})-[t:TRANSACTS]->() RETURN t.amount AS x, t.event_time AS ts LIMIT 1", a=NS)[0]
    assert isinstance(amount["x"], float) and amount["ts"].year >= 2023


def _ring(built) -> list[str]:
    rings = pl.read_parquet(built.data_dir / A / "ground_truth/rings.parquet")
    ids = pl.read_parquet(built.data_dir / A / "ground_truth/party_ids.parquet").select(
        "person", node_id=pl.format("{}:Party:{}", "source", "source_pk")
    )
    resolved = pl.read_parquet(built.data_dir / A / "shared/party_resolved.parquet")
    return rings.filter(pl.col("ring_id") == 0).join(ids, on="person").join(resolved, on="node_id")["global_party_id"].unique().to_list()


def _drawable(picture: dict) -> None:
    ids = [n["id"] for n in picture["nodes"]]
    assert len(ids) == len(set(ids))
    assert all(e["source"] in ids and e["target"] in ids for e in picture["edges"])
    assert {n["kind"] for n in picture["nodes"]} <= {"person", "merchant", "device", "identifier", "item"}
    shown = " ".join([*(n["label"] + n.get("note", "") for n in picture["nodes"]), *picture["story"]])
    assert "gp_" not in shown and ":Party:" not in shown and "Identifier:" not in shown  # no ids, no identifier values


def test_picture_of_a_group_shows_the_shared_device_and_the_money(built, store):
    from graph_platform.ui.network import group_network

    s, _ = store
    members = _ring(built)
    picture = group_network(s, NS, members)
    _drawable(picture)
    assert {n["id"] for n in picture["nodes"] if n["kind"] in ("person", "merchant")} == set(members)
    device = [n for n in picture["nodes"] if n["kind"] == "device"]
    assert device and sum(e["target"] == device[0]["id"] for e in picture["edges"]) >= len(members) // 2
    assert any(e["kind"] == "transfer" for e in picture["edges"]) and any("dùng chung" in line for line in picture["story"])
    assert group_network(s, "pytest_beta", members)["nodes"] == []


def test_picture_of_a_party_centres_on_it_and_flags_what_is_on_record(built, store):
    from datetime import date

    from graph_platform.ui.network import party_network

    s, _ = store
    late = s.query(
        NS,
        "MATCH (p:Party {tenant: $tenant})-[:TRANSACTS]-(n:Party)-[:APPLIES_FOR]->(:Event {subtype: 'loan'})<-[:RELATES_TO]-(f:Event) "
        "WHERE f.days_past_due >= 30 RETURN p.node_id AS party, count(DISTINCT n) AS c ORDER BY c DESC, party LIMIT 1",
    )["party"][0]
    picture = party_network(s, NS, late, watch=frozenset({"late"}))
    _drawable(picture)
    focus = [n for n in picture["nodes"] if n.get("focus")]
    assert [n["id"] for n in focus] == [late] and not focus[0]["open"]
    assert all(late in (e["source"], e["target"]) or e["kind"] in ("device", "identifier") for e in picture["edges"])
    flagged = [n for n in picture["nodes"] if n.get("flag") and not n.get("focus")]
    assert flagged and all("trễ hạn" in n["note"] for n in flagged) and any("trễ hạn" in line for line in picture["story"])
    before = party_network(s, NS, late, as_of=date(2023, 1, 2), watch=frozenset({"late"}))
    assert not [n for n in before["nodes"] if n.get("flag")]  # nothing was on record yet at that date


def test_display_names_are_stable_and_never_the_real_name(built):
    from graph_platform.ui.names import party_name, person_name, plain

    party = pl.read_parquet(graph_dir(built, A) / "nodes/Party.parquet")
    person = party.filter(pl.col("subtype") == "person")["node_id"][0]
    assert person_name(person) == person_name(person) and "gp_" not in person_name(person)
    shops = party.filter(pl.col("subtype") == "merchant")
    assert shops["trade_name"].null_count() == 0 and shops["trade_name"].str.contains("CGV").any()
    shop = shops.row(0, named=True)
    assert party_name(shop["node_id"], "merchant", shop["trade_name"]) == shop["trade_name"]
    assert plain("Trần Đức") == "tran duc"


def test_picture_of_a_suggestion_keeps_what_was_chosen_apart_from_what_is_suggested(built, store):
    from datetime import date

    from graph_platform.ui.network import recommendation_network

    s, _ = store
    bought = _edges(built, "PURCHASES").filter(pl.col("dst_id").str.contains(":Item:MV"))
    fan = bought.group_by("src_id").len().sort("len", "src_id", descending=True)["src_id"][0]
    seen = set(bought.filter(pl.col("src_id") == fan)["dst_id"])
    suggested = sorted(set(bought["dst_id"]) - seen)[:5]
    picture = recommendation_network(s, NS, fan, suggested)
    _drawable(picture)
    assert {n["id"] for n in picture["nodes"] if n.get("suggest")} == set(suggested)
    assert {e["target"] for e in picture["edges"] if e["source"] == fan and e["kind"] == "purchase"} <= seen
    assert not [e for e in recommendation_network(s, NS, fan, suggested, date(2023, 1, 2))["edges"] if e["kind"] == "purchase"]

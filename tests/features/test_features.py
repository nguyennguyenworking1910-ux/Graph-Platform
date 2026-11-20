import shutil
from datetime import date, datetime

import polars as pl
import pytest

from graph_platform import features
from graph_platform.contracts import GraphScope
from graph_platform.features.families import FAMILIES
from graph_platform.graph import graph_dir
from graph_platform.naming import FeatureName

AS_OF = date(2024, 6, 1)
SCOPE = GraphScope()


def _ts(s):
    return datetime.fromisoformat(s)


def _write(root, kind, name, rows):
    path = root / kind / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)


@pytest.fixture
def tiny(tmp_path):
    """A->B twice, B->C once, C->D after as_of. B's default is observed before as_of, C's only after."""
    p = ["gp_000000000000000a", "gp_000000000000000b", "gp_000000000000000c", "gp_000000000000000d"]
    _write(tmp_path, "nodes", "Party", {
        "node_id": p, "node_type": ["Party"] * 4, "subtype": ["person"] * 4,
        "valid_from": [_ts("2023-01-01")] * 4, "income": [10.0, 20.0, None, 40.0],
    })  # fmt: skip
    pl.DataFrame({
        "party_id": [p[0], p[1], p[3], p[2]],
        "valid_from": [_ts("2023-01-01"), _ts("2023-01-01"), _ts("2023-01-01"), _ts("2024-09-01")],
        "income": [10.0, 20.0, 40.0, 30.0],
    }).write_parquet(tmp_path / "party_history.parquet")  # fmt: skip
    _write(tmp_path, "nodes", "Event", {
        "node_id": ["loan_b", "loan_c", "perf_b", "perf_c"], "node_type": ["Event"] * 4,
        "subtype": ["loan", "loan", "loan_performance", "loan_performance"],
        "event_time": [_ts("2024-01-05"), _ts("2024-01-05"), _ts("2024-05-01"), _ts("2024-07-01")],
        "days_past_due": [None, None, 90, 120],
    })  # fmt: skip
    _write(tmp_path, "edges", "TRANSACTS", {
        "src_id": [p[0], p[0], p[1], p[2]], "dst_id": [p[1], p[1], p[2], p[3]],
        "event_time": [_ts("2024-01-10"), _ts("2024-05-20"), _ts("2024-02-01"), _ts("2024-08-01")],
        "amount": [100.0, 50.0, 70.0, 999.0],
    })  # fmt: skip
    _write(tmp_path, "edges", "APPLIES_FOR", {
        "src_id": [p[1], p[2]], "dst_id": ["loan_b", "loan_c"], "event_time": [_ts("2024-01-05")] * 2,
    })  # fmt: skip
    _write(tmp_path, "edges", "RELATES_TO", {
        "src_id": ["perf_b", "perf_c"], "dst_id": ["loan_b", "loan_c"],
        "event_time": [_ts("2024-05-01"), _ts("2024-07-01")],
    })  # fmt: skip
    _write(tmp_path, "edges", "USES", {
        "src_id": [p[0], p[3]], "dst_id": ["Device:mobile:x", "Device:mobile:x"],
        "event_time": [_ts("2024-01-01"), _ts("2024-09-01")],
    })  # fmt: skip
    return tmp_path, p


def _row(frames, family, party):
    return frames[family].filter(pl.col("party_id") == party).row(0, named=True)


def test_values_on_a_hand_built_graph(tiny, settings):
    root, (a, b, c, d) = tiny
    f = features.compute(root, AS_OF, settings.ontology, SCOPE)

    deg = _row(f, "degree", a)
    assert deg["party__degree__txn_cnt__transacts_out__all"] == 2
    assert deg["party__degree__amt_sum__transacts_out__all"] == 150
    assert deg["party__degree__txn_cnt__transacts_out__30d"] == 1
    assert deg["party__degree__counterparty_nuniq__transacts_out__all"] == 1
    assert _row(f, "degree", b)["party__degree__txn_cnt__transacts_in__all"] == 2
    assert _row(f, "degree", d)["party__degree__txn_cnt__transacts_in__all"] == 0  # C->D is in the future

    ka, kb, kd = (_row(f, "khop", x) for x in (a, b, d))
    assert ka["party__khop__neighbor_cnt__peer_h1__all"] == 1 and kb["party__khop__neighbor_cnt__peer_h1__all"] == 2
    assert ka["party__khop__dpd90_party_rate__peer_h1__all"] == 1.0  # B defaulted, observed 2024-05-01
    assert kb["party__khop__loan_party_cnt__peer_h1__all"] == 1  # C has a loan
    assert kb["party__khop__dpd90_party_cnt__peer_h1__all"] == 0  # C's 90+ DPD is only seen in July
    assert kb["party__khop__income_avg__peer_h1__all"] == 10.0  # A known, C unknown
    assert kd["party__khop__neighbor_cnt__peer_h1__all"] == 0
    assert _row(f, "shared", a)["party__shared__party_nuniq__device__all"] == 0  # D joins the device later

    later = features.compute(root, date(2024, 10, 1), settings.ontology, SCOPE)
    assert _row(later, "khop", b)["party__khop__dpd90_party_cnt__peer_h1__all"] == 1
    assert _row(later, "shared", a)["party__shared__party_nuniq__device__all"] == 1
    assert _row(later, "khop", d)["party__khop__neighbor_cnt__peer_h1__all"] == 2  # C by payment, A by device


def test_every_feature_name_follows_the_standard(tiny, settings):
    root, _ = tiny
    f = features.compute(root, AS_OF, settings.ontology, SCOPE)
    assert set(f) == set(FAMILIES)
    names = [c for df in f.values() for c in df.columns[1:]]
    assert len(names) == len(set(names)) > 50
    for family, df in f.items():
        assert df.columns[0] == "party_id" and df.height == 4
        for c in df.columns[1:]:
            n = FeatureName.parse(c)
            assert n.entity == "party" and n.family == family


def test_no_leakage_from_the_future(built, tmp_path):
    """Corrupting everything at or after as_of must not change a single feature value."""
    src = graph_dir(built, "alpha_group")
    before = features.compute(src, AS_OF, built.ontology, SCOPE)

    cut = datetime(AS_OF.year, AS_OF.month, AS_OF.day)
    shutil.copytree(src / "nodes", tmp_path / "nodes")
    shutil.copy(src / "party_history.parquet", tmp_path / "party_history.parquet")
    (tmp_path / "edges").mkdir()
    future_rows = 0
    for f in sorted((src / "edges").glob("*.parquet")):
        df = pl.read_parquet(f)
        future = pl.col("event_time") >= cut
        future_rows += df.filter(future).height
        changes = [pl.when(future).then(pl.col("dst_id").shuffle(seed=1)).otherwise(pl.col("dst_id")).alias("dst_id")]
        if "amount" in df.columns:
            changes.append(pl.when(future).then(pl.col("amount") * 1000).otherwise(pl.col("amount")).alias("amount"))
        df.with_columns(changes).write_parquet(tmp_path / "edges" / f.name)
    ev = pl.read_parquet(src / "nodes/Event.parquet")
    ev.with_columns(days_past_due=pl.when(pl.col("event_time") >= cut).then(999).otherwise(pl.col("days_past_due"))).write_parquet(
        tmp_path / "nodes/Event.parquet"
    )
    assert future_rows > 10_000

    after = features.compute(tmp_path, AS_OF, built.ontology, SCOPE)
    for family in FAMILIES:
        assert before[family].equals(after[family]), family

    # sanity: the same corruption applied before as_of does change features
    earlier = features.compute(tmp_path, date(2025, 6, 1), built.ontology, SCOPE)
    assert not earlier["khop"].equals(features.compute(src, date(2025, 6, 1), built.ontology, SCOPE)["khop"])


def test_build_writes_snapshots_and_registry(built):
    import json

    dates = features.month_starts(date(2024, 5, 1), date(2024, 6, 1))
    counts = features.build(built, "alpha_group", dates, ["degree", "khop"])
    assert set(counts) == {"degree/2024-05-01", "degree/2024-06-01", "khop/2024-05-01", "khop/2024-06-01"}
    df = features.load(built, "alpha_group", AS_OF, ["degree", "khop"])
    assert df["party_id"].n_unique() == df.height and df.width == 1 + 25 + 11
    registry = json.loads((features.features_dir(built, "alpha_group", SCOPE) / "registry.json").read_text())
    assert {r["name"] for r in registry["features"]} >= set(df.columns[1:])
    assert all(r["description"] for r in registry["features"])
    assert registry["graph_scope"]["lookback_days"] == 365


def test_tenant_without_credit_domain_still_builds(built):
    f = features.compute(graph_dir(built, "beta_group"), AS_OF, built.ontology, SCOPE, ["khop", "shared"])
    assert f["khop"].height > 0


def test_graph_scope_bounds_time_and_fanout(tiny, built, settings):
    root, (a, _b, _c, _d) = tiny
    short = features.compute(root, AS_OF, settings.ontology, GraphScope(lookback_days=30), ["degree", "khop"])
    assert _row(short, "degree", a)["party__degree__txn_cnt__transacts_out__all"] == 1  # January payment is out of scope
    assert _row(short, "khop", a)["party__khop__dpd90_party_rate__peer_h1__all"] == 1.0  # loan history is not windowed

    g = graph_dir(built, "alpha_group")
    col = "party__khop__neighbor_cnt__peer_h1__all"
    wide = features.compute(g, date(2025, 6, 1), built.ontology, SCOPE, ["khop"])["khop"][col]
    capped = features.compute(g, date(2025, 6, 1), built.ontology, GraphScope(max_degree=3), ["khop"])["khop"][col]
    assert wide.max() > 3 and capped.max() <= 3
    strong = features.compute(g, date(2025, 6, 1), built.ontology, GraphScope(min_weight=5), ["khop"])["khop"][col]
    assert strong.sum() < wide.sum()


def test_party_attributes_are_read_as_of_the_snapshot(tiny, settings):
    """C's income is only reported by a source record dated 2024-09-01."""
    root, (_a, b, c, _d) = tiny
    col = "party__profile__income_val__self__all"
    before = features.compute(root, AS_OF, settings.ontology, SCOPE, ["profile", "khop"])
    unknown = _row(before, "profile", c)[col]
    assert (unknown is None or unknown != unknown) and _row(before, "profile", b)[col] == 20.0  # NaN = not known yet
    after = features.compute(root, date(2024, 10, 1), settings.ontology, SCOPE, ["profile", "khop"])
    assert _row(after, "profile", c)[col] == 30.0
    assert _row(after, "khop", b)["party__khop__income_avg__peer_h1__all"] == 20.0  # mean of A=10 and C=30

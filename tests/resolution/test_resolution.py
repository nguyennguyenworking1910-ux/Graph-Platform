import polars as pl

from graph_platform.resolution import evaluate

A, B = "alpha_group", "beta_group"


def _resolved(s, tenant):
    return pl.read_parquet(s.data_dir / tenant / "shared/party_resolved.parquet")


def test_precision_and_recall_meet_the_bar(built):
    m = evaluate(built, A)
    assert m["precision"] >= 0.95 and m["recall"] >= 0.85, m
    assert m["max_cluster"] <= 12


def test_links_span_domains(built):
    df = _resolved(built, A)
    per_party = df.group_by("global_party_id").agg(pl.col("domain").n_unique().alias("domains"))
    assert (per_party["domains"] >= 3).sum() > 500
    assert df["node_id"].n_unique() == df.height


def test_person_and_merchant_never_merge(built):
    df = _resolved(built, A)
    assert df.group_by("global_party_id").agg(pl.col("subtype").n_unique().alias("k"))["k"].max() == 1


def test_merchant_linked_across_portal_and_wallet(built):
    df = _resolved(built, A).filter(pl.col("subtype") == "merchant")
    both = df.group_by("global_party_id").agg(pl.col("source_system").n_unique().alias("k"))
    assert (both["k"] >= 2).mean() > 0.9  # cinema sites are also seen by the ticketing source


def test_household_shared_phone_does_not_merge_people(built):
    truth = pl.read_parquet(built.data_dir / A / "ground_truth/party_ids.parquet").select(
        "person", node_id=pl.format("{}:Party:{}", "source", "source_pk")
    )
    df = _resolved(built, A).join(truth, on="node_id")
    mixed = df.group_by("global_party_id").agg(pl.col("person").n_unique().alias("people"))
    assert (mixed["people"] > 1).mean() < 0.01


def test_tenants_never_share_a_party(built):
    a, b = _resolved(built, A), _resolved(built, B)
    assert not set(a["global_party_id"]) & set(b["global_party_id"])
    assert not set(a["node_id"]) & set(b["node_id"])
    assert set(a["source_system"]) & set(b["source_system"]) == set()

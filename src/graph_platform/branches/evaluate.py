"""Scoring of branch outputs against the simulator's ground truth. Only meaningful for synthetic tenants."""

from __future__ import annotations

import polars as pl

from graph_platform.config import Settings
from graph_platform.storage import Principal, resolve


def ring_recovery(settings: Settings, tenant: str, table: pl.DataFrame, k: int = 20) -> dict[str, float]:
    """A detected cluster is a hit when most of its members belong to one planted ring."""
    truth_dir = resolve(settings, Principal(tenant, "platform", "evaluator"), "ground_truth", tenant=tenant)
    shared = resolve(settings, Principal(tenant, "platform", "resolution"), "shared", tenant=tenant)
    ids = pl.read_parquet(truth_dir / "party_ids.parquet").select("person", node_id=pl.format("{}:Party:{}", "source", "source_pk"))
    resolved = pl.read_parquet(shared / "party_resolved.parquet").select("node_id", party_id="global_party_id")
    ring = (
        pl.read_parquet(truth_dir / "rings.parquet")
        .join(ids, on="person")
        .join(resolved, on="node_id")
        .select("party_id", "ring_id")
        .unique()
    )

    joined = table.join(ring, on="party_id", how="left")
    same = joined.group_by("cluster_id", "ring_id").len("same").drop_nulls()
    per = (
        joined.group_by("cluster_id", "rank")
        .agg(members=pl.len(), top_ring=pl.col("ring_id").drop_nulls().mode().first(), in_ring=pl.col("ring_id").is_not_null().sum())
        .join(same, left_on=["cluster_id", "top_ring"], right_on=["cluster_id", "ring_id"], how="left")
        .with_columns(hit=pl.col("same").fill_null(0) / pl.col("members") >= 0.6)
        .sort("rank")
    )  # fmt: skip
    top = per.head(k)
    found = set(top.filter("hit")["top_ring"].to_list())
    all_found = set(per.filter("hit")["top_ring"].to_list())
    return {
        f"precision_at_{k}": float(top["hit"].mean()) if top.height else 0.0,
        f"rings_found_at_{k}": len(found),
        "rings_found": len(all_found),
        "rings_total": ring["ring_id"].n_unique(),
        # precision among as many top clusters as there are true rings: 1.0 means every ring outranks every false alarm
        "r_precision": float(per.head(ring["ring_id"].n_unique())["hit"].mean()) if per.height else 0.0,
        "ring_recall": len(all_found) / ring["ring_id"].n_unique(),
    }


def quasi_merchant_recovery(settings: Settings, tenant: str, table: pl.DataFrame) -> dict[str, float]:
    """How many planted quasi-cash merchants sit at the top of the ranking."""
    truth_dir = resolve(settings, Principal(tenant, "platform", "evaluator"), "ground_truth", tenant=tenant)
    shared = resolve(settings, Principal(tenant, "platform", "resolution"), "shared", tenant=tenant)
    resolved = pl.read_parquet(shared / "party_resolved.parquet").filter(pl.col("subtype") == "merchant")
    truth = pl.read_parquet(truth_dir / "quasi_merchants.parquet").join(
        resolved.with_columns(source_pk=pl.col("node_id").str.split(":").list.last()), on="source_pk"
    )
    true_ids = set(truth["global_party_id"])
    ranked = table.sort("rank")["party_id"].to_list()
    k = len(true_ids)
    return {
        "quasi_total": k,
        "r_precision": len(true_ids & set(ranked[:k])) / k,
        "recall_at_2r": len(true_ids & set(ranked[: 2 * k])) / k,
    }


def scalper_recovery(settings: Settings, tenant: str, table: pl.DataFrame) -> dict[str, float]:
    truth_dir = resolve(settings, Principal(tenant, "platform", "evaluator"), "ground_truth", tenant=tenant)
    shared = resolve(settings, Principal(tenant, "platform", "resolution"), "shared", tenant=tenant)
    ids = pl.read_parquet(truth_dir / "party_ids.parquet").select("person", node_id=pl.format("{}:Party:{}", "source", "source_pk"))
    resolved = pl.read_parquet(shared / "party_resolved.parquet").select("node_id", party_id="global_party_id")
    true_ids = set(pl.read_parquet(truth_dir / "scalpers.parquet").join(ids, on="person").join(resolved, on="node_id")["party_id"])
    ranked = table.sort("rank")["party_id"].to_list()
    k = len(true_ids)
    return {"scalper_total": k, "r_precision": len(true_ids & set(ranked[:k])) / k, "recall": len(true_ids & set(ranked)) / k}


def corridor_recovery(settings: Settings, tenant: str, table: pl.DataFrame) -> dict[str, float]:
    truth_dir = resolve(settings, Principal(tenant, "platform", "evaluator"), "ground_truth", tenant=tenant)
    planted = pl.read_parquet(truth_dir / "corridor.parquet")["corridor"][0]
    ranks = table.filter(pl.col("corridor") == planted)["rank"]
    return {"planted_rank": float(ranks[0]) if ranks.len() else -1.0}


EVALUATORS = {
    "community_rings": ring_recovery,
    "circular_flow": ring_recovery,
    "merchant_quasi_cash": quasi_merchant_recovery,
    "purchase_velocity": scalper_recovery,
    "corridor_anomaly": corridor_recovery,
}

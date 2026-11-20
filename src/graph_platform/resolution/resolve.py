"""Entity resolution of Party nodes across the domains of one tenant. Never crosses tenants."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import numpy as np
import polars as pl
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from graph_platform.config import Settings
from graph_platform.storage import Principal, canonical_files, resolve

STRONG = ("national_id", "merchant_code", "tax_id")


@dataclass(frozen=True)
class ErRules:
    max_fanout: int = 6  # identifiers shared by more parties than this carry no evidence
    name_similarity: float = 0.88  # required with a shared phone
    max_cluster: int = 12  # larger clusters fall back to strong evidence only


def _files(paths: list[str]) -> str:
    return "[" + ", ".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"


def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return connected_components(coo_matrix((np.ones(a.size, bool), (a, b)), shape=(n, n)), directed=False)[1]


def resolve_parties(settings: Settings, tenant: str, rules: ErRules = ErRules()) -> pl.DataFrame:
    """Writes shared/party_resolved.parquet: node_id -> global_party_id."""
    who = Principal(tenant, "platform", "resolution")
    con = duckdb.connect()
    con.execute(
        f"""CREATE TABLE party AS
        SELECT row_number() OVER (ORDER BY node_id) - 1 AS idx, node_id, subtype, domain, source_system,
               lower(regexp_replace(full_name, '\\s+', '', 'g')) AS name
        FROM read_parquet({_files(canonical_files(settings, who, tenant, "nodes", "Party"))}, union_by_name=true)"""
    )
    has_identifier = _files(canonical_files(settings, who, tenant, "edges", "HAS_IDENTIFIER"))
    con.execute(
        f"""CREATE TABLE hid AS
        SELECT DISTINCT p.idx, h.dst_id AS ident, split_part(h.dst_id, ':', 2) AS kind
        FROM read_parquet({has_identifier}, union_by_name=true) h
        JOIN party p ON p.node_id = h.src_id"""
    )
    con.execute("CREATE TABLE nid AS SELECT idx, min(ident) AS nid FROM hid WHERE kind = 'national_id' GROUP BY idx")
    pairs = con.sql(
        f"""WITH ok AS (SELECT ident FROM hid GROUP BY ident HAVING count(*) BETWEEN 2 AND {rules.max_fanout}),
        cand AS (
            SELECT a.idx AS a, b.idx AS b, a.kind
            FROM hid a JOIN hid b ON a.ident = b.ident AND a.idx < b.idx
            WHERE a.ident IN (SELECT ident FROM ok))
        SELECT c.a, c.b, bool_or(c.kind IN {STRONG}) AS strong
        FROM cand c
        JOIN party pa ON pa.idx = c.a JOIN party pb ON pb.idx = c.b
        LEFT JOIN nid na ON na.idx = c.a LEFT JOIN nid nb ON nb.idx = c.b
        WHERE pa.subtype = pb.subtype AND (
              c.kind IN {STRONG}
           OR (na.nid IS NULL OR nb.nid IS NULL OR na.nid = nb.nid) AND (
                 c.kind = 'email'
              OR c.kind = 'phone' AND jaro_winkler_similarity(pa.name, pb.name) >= {rules.name_similarity}))
        GROUP BY c.a, c.b"""
    ).pl()

    n = con.execute("SELECT count(*) FROM party").fetchall()[0][0]
    a, b, strong = pairs["a"].to_numpy(), pairs["b"].to_numpy(), pairs["strong"].to_numpy()
    comp = _components(n, a, b)
    big = np.bincount(comp)[comp] > rules.max_cluster
    nid = con.sql("SELECT idx, nid FROM nid").pl()
    mixed = pl.DataFrame({"comp": comp[nid["idx"].to_numpy()], "nid": nid["nid"]}).group_by("comp").agg(pl.col("nid").n_unique().alias("k"))
    big |= np.isin(comp, mixed.filter(pl.col("k") > 1)["comp"].to_numpy())  # weak links chained two different people
    keep = strong | ~(big[a] | big[b])
    comp = _components(n, a[keep], b[keep])

    con.register("comp", pl.DataFrame({"idx": np.arange(n), "comp": comp}))
    out = con.sql(
        """SELECT p.node_id,
                  'gp_' || substr(md5(min(p.node_id) OVER (PARTITION BY c.comp)), 1, 16) AS global_party_id,
                  p.subtype, p.domain, p.source_system,
                  count(*) OVER (PARTITION BY c.comp) AS cluster_size
           FROM party p JOIN comp c USING (idx) ORDER BY p.node_id"""
    ).pl()
    shared = resolve(settings, who, "shared", tenant=tenant, mode="w")
    shared.mkdir(parents=True, exist_ok=True)
    out.write_parquet(shared / "party_resolved.parquet")
    return out


def evaluate(settings: Settings, tenant: str) -> dict[str, float]:
    """Pairwise precision/recall of resolved persons against the simulator's ground truth."""
    truth_dir = resolve(settings, Principal(tenant, "platform", "evaluator"), "ground_truth", tenant=tenant)
    shared = resolve(settings, Principal(tenant, "platform", "resolution"), "shared", tenant=tenant)
    truth = pl.read_parquet(truth_dir / "party_ids.parquet").select("person", node_id=pl.format("{}:Party:{}", "source", "source_pk"))
    df = pl.read_parquet(shared / "party_resolved.parquet").join(truth, on="node_id")

    def pairs(*by: str) -> int:
        k = df.group_by(by).len()["len"]
        return int((k * (k - 1) // 2).sum())

    tp, predicted, actual = pairs("global_party_id", "person"), pairs("global_party_id"), pairs("person")
    return {
        "precision": tp / predicted if predicted else 1.0,
        "recall": tp / actual if actual else 1.0,
        "nodes": df.height,
        "max_cluster": int(df.group_by("global_party_id").len()["len"].max()),
    }

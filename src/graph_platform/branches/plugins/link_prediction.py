"""Edge-level family: predict the next Party -> Item edges (recommendation, cross-sell)."""

from __future__ import annotations

from datetime import timedelta

import duckdb
import numpy as np
import polars as pl
from scipy.sparse import csr_matrix, diags

from graph_platform.branches.runner import Context, Result
from graph_platform.graph import graph_dir

CHUNK = 5000


def _top_k(scores: csr_matrix | np.ndarray, seen: csr_matrix, k: int) -> np.ndarray:
    dense = np.array(scores.todense() if hasattr(scores, "todense") else scores, dtype=np.float64)
    dense[seen.nonzero()] = -np.inf
    return np.argsort(-dense, axis=1, kind="stable")[:, :k]


def run(ctx: Context) -> Result:
    c = ctx.contract
    par = {"edge": "PURCHASES", "target": "Item", "item_subtype": "movie", "test_days": 90, "k": 10, "peer_weight": 0.5} | c.params
    edge = ctx.settings.ontology_of(ctx.tenant).edge(par["edge"]).name
    target = ctx.settings.ontology_of(ctx.tenant).node(par["target"]).name
    if par["item_subtype"] not in ctx.settings.ontology_of(ctx.tenant).node(target).subtypes:
        raise ValueError(f"unknown item subtype: {par['item_subtype']}")
    days, k = int(par["test_days"]), int(par["k"])
    snap = ctx.snapshot(c.as_of)
    n = snap.party_ids.size
    train = snap.con.sql(
        f"""SELECT p.idx AS u, i.node_id AS item, count(*) AS cnt
            FROM e_{edge} e JOIN party p ON p.node_id = e.src_id
            JOIN n_{target} i ON i.node_id = e.dst_id AND i.subtype = '{par["item_subtype"]}' GROUP BY 1, 2"""
    ).pl()
    items = train["item"].unique().sort()
    item_idx = {v: i for i, v in enumerate(items.to_list())}
    r = csr_matrix(
        (np.ones(train.height), (train["u"].to_numpy(), train["item"].replace_strict(item_idx).to_numpy())), shape=(n, len(items))
    )

    # held-out truth: edges in [as_of, as_of + test_days) to items the party had not taken before
    g = graph_dir(ctx.settings, ctx.tenant, principal="branches")
    end = c.as_of + timedelta(days=days)
    future = (
        duckdb.connect()
        .sql(
            f"""SELECT DISTINCT src_id AS party_id, dst_id AS item FROM read_parquet('{g / "edges" / (edge + ".parquet")}')
            WHERE event_time >= DATE '{c.as_of}' AND event_time < DATE '{end}'"""
        )
        .pl()
    )
    ids = pl.DataFrame({"party_id": snap.party_ids}).with_row_index("u")
    future = future.join(ids, on="party_id").filter(pl.col("item").is_in(items.implode()))
    fu = np.array(future["u"].to_numpy(), dtype=np.int64)  # copies: polars buffers are read-only
    fi = np.array(future["item"].replace_strict(item_idx).to_numpy(), dtype=np.int64)
    new = np.asarray(r[fu, fi]).ravel() == 0
    truth = csr_matrix((np.ones(new.sum()), (fu[new], fi[new])), shape=r.shape)
    users = np.flatnonzero((np.asarray(truth.sum(axis=1)).ravel() > 0) & (np.asarray(r.sum(axis=1)).ravel() > 0))

    norm = np.sqrt(np.asarray(r.sum(axis=0)).ravel())
    sim = diags(1 / np.maximum(norm, 1)) @ (r.T @ r) @ diags(1 / np.maximum(norm, 1))
    peers = (snap.peers > 0).astype(np.float64)
    deg = np.asarray(peers.sum(axis=1)).ravel()
    peer_taste = diags(np.divide(1.0, deg, out=np.zeros(n), where=deg > 0)) @ peers @ r

    popularity = np.asarray(r.sum(axis=0)).ravel()
    scorers = {
        "popularity": lambda u: np.tile(popularity, (u.size, 1)),
        "item_cf": lambda u: r[u] @ sim,
        "item_cf_peer": lambda u: r[u] @ sim + par["peer_weight"] * (peer_taste[u] @ sim + peer_taste[u]),
    }
    metrics: dict = {"users_evaluated": int(users.size), "items": len(items), "train_edges": int(r.nnz)}
    best = np.zeros((users.size, k), dtype=np.int64)
    truth_cnt = np.asarray(truth[users].sum(axis=1)).ravel()
    for name, scorer in scorers.items():
        hit_any, recall = 0.0, 0.0
        for start in range(0, users.size, CHUNK):  # bounded memory: CHUNK x items at a time
            u = users[start : start + CHUNK]
            top = _top_k(scorer(u), r[u], k)
            hits = np.asarray(truth[np.repeat(u, k), top.ravel()]).reshape(u.size, k).sum(axis=1)
            hit_any += float((hits > 0).sum())
            recall += float((hits / truth_cnt[start : start + CHUNK]).sum())
            best[start : start + CHUNK] = top
        metrics[name] = {f"hit_rate_at_{k}": hit_any / users.size, f"recall_at_{k}": recall / users.size}
    table = pl.DataFrame(
        {
            "party_id": np.repeat(snap.party_ids[users], k),
            "rank": np.tile(np.arange(1, k + 1), users.size),
            "item_id": items.to_numpy()[best.ravel()],
        }
    )
    return Result(metrics, table)

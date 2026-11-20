"""Feature families. Each builder returns one row per party, columns named by the platform standard."""

from __future__ import annotations

import random
from collections.abc import Callable

import igraph as ig
import numpy as np
import polars as pl
from scipy.sparse import diags

from graph_platform.features.snapshot import Snapshot
from graph_platform.naming import feature_name as fn

WINDOWS = {"30d": 30, "90d": 90, "365d": 365, "all": None}
EMBEDDING_DIM = 16
OUTCOME_EVENTS = ("churn", "fraud_case", "claim", "chargeback")
DESCRIPTIONS: dict[str, str] = {}


def _f(family: str, metric: str, qualifier: str, window: str, description: str) -> str:
    name = fn("party", family, metric, qualifier, window)
    DESCRIPTIONS[name] = description
    return name


def _frame(snap: Snapshot, columns: dict[str, np.ndarray]) -> pl.DataFrame:
    return pl.DataFrame({"party_id": snap.party_ids, **columns})


def _dense(snap: Snapshot, sql: str, columns: list[str]) -> pl.DataFrame:
    """Left-join an aggregate onto every party, missing = 0."""
    agg = snap.con.sql(sql).pl()
    base = pl.DataFrame({"party_id": snap.party_ids})
    return base.join(agg, on="party_id", how="left").with_columns(pl.col(columns).fill_null(0).cast(pl.Float64))


def _dpd90(snap: Snapshot) -> tuple[np.ndarray, np.ndarray]:
    """Per party: has a loan, and has an observed 90+ DPD snapshot, both strictly before as_of."""
    rows = snap.con.execute(
        """SELECT p.idx, max((perf.days_past_due >= 90)::INT) AS bad
           FROM e_APPLIES_FOR a
           JOIN n_Event loan ON loan.node_id = a.dst_id AND loan.subtype = 'loan'
           JOIN party p ON p.node_id = a.src_id
           LEFT JOIN e_RELATES_TO r ON r.dst_id = loan.node_id
           LEFT JOIN n_Event perf ON perf.node_id = r.src_id AND perf.subtype = 'loan_performance'
           GROUP BY p.idx"""
    ).fetchnumpy()
    n = snap.party_ids.size
    has_loan, bad = np.zeros(n), np.zeros(n)
    has_loan[rows["idx"]] = 1
    bad[rows["idx"]] = np.nan_to_num(np.asarray(rows["bad"], dtype=float))
    return has_loan, bad


def profile(snap: Snapshot) -> pl.DataFrame:
    """The party's own attributes and credit history; the non-graph baseline."""
    r = snap.con.execute(
        f"""SELECT income, date_diff('year', birth_date, DATE '{snap.as_of}') AS age,
                   hash(occupation) % 9973 AS occupation, hash(gender) % 9973 AS gender, hash(city) % 9973 AS city
            FROM party ORDER BY idx"""
    ).fetchnumpy()
    loans = snap.con.execute(
        """SELECT p.idx, count(*) AS n FROM e_APPLIES_FOR a
           JOIN n_Event loan ON loan.node_id = a.dst_id AND loan.subtype = 'loan'
           JOIN party p ON p.node_id = a.src_id GROUP BY p.idx"""
    ).fetchnumpy()
    loan_cnt = np.zeros(snap.party_ids.size)
    loan_cnt[loans["idx"]] = loans["n"]

    worst = snap.con.execute(
        """SELECT p.idx, max(perf.days_past_due) AS d
           FROM e_APPLIES_FOR a JOIN e_RELATES_TO r ON r.dst_id = a.dst_id
           JOIN n_Event perf ON perf.node_id = r.src_id AND perf.subtype = 'loan_performance'
           JOIN party p ON p.node_id = a.src_id GROUP BY p.idx"""
    ).fetchnumpy()
    dpd_max = np.zeros(snap.party_ids.size)
    dpd_max[worst["idx"]] = np.nan_to_num(np.asarray(worst["d"], dtype=float))

    def num(key: str) -> np.ndarray:
        return np.ma.filled(np.ma.asarray(r[key]).astype(float), np.nan)

    # the tenant's own party attributes become features without code: numbers as they are, text as a code, dates as age in days
    own = {"string": ("code", "hash({c}) % 9973"), "date": ("days_val", "date_diff('day', {c}, DATE '{d}')")}
    own["timestamp"] = own["date"]
    extra = {}
    for name, kind in snap.party_extra:
        stat, sql = own.get(kind, ("val", "CAST({c} AS DOUBLE)"))
        got = snap.con.execute(f"SELECT {sql.format(c=name, d=snap.as_of)} AS x FROM party ORDER BY idx").fetchnumpy()["x"]
        extra[_f("profile", f"{name}_{stat}", "self", "all", f"tenant attribute {name}")] = np.ma.filled(
            np.ma.asarray(got).astype(float), np.nan
        )

    return _frame(
        snap,
        extra
        | {
            _f("profile", "income_val", "self", "all", "declared income"): num("income"),
            _f("profile", "age_val", "self", "all", "age in years at as_of"): num("age"),
            _f("profile", "occupation_code", "self", "all", "occupation, hashed code"): num("occupation"),
            _f("profile", "gender_code", "self", "all", "gender, hashed code"): num("gender"),
            _f("profile", "city_code", "self", "all", "city, hashed code"): num("city"),
            _f("profile", "loan_cnt", "self", "all", "own loans opened before as_of"): loan_cnt,
            _f("profile", "dpd90_flag", "self", "all", "own observed 90+ DPD before as_of"): _dpd90(snap)[1],
            _f("profile", "dpd_max", "self", "all", "worst own days past due observed before as_of"): dpd_max,
        },
    )


def degree(snap: Snapshot) -> pl.DataFrame:
    exprs, cols = [], []
    for w, days in WINDOWS.items():
        cond = f"event_time >= TIMESTAMP '{snap.as_of}' - INTERVAL {days} DAY" if days else "TRUE"
        for d in ("out", "in"):
            spec = (
                ("txn_cnt", f"count(*) FILTER ({cond} AND dir = '{d}')", f"transactions {d}"),
                ("amt_sum", f"coalesce(sum(amount) FILTER ({cond} AND dir = '{d}'), 0)", f"amount {d}"),
                (
                    "counterparty_nuniq",
                    f"count(DISTINCT other) FILTER ({cond} AND dir = '{d}')",
                    f"distinct counterparties {d}",
                ),
            )
            for metric, sql, text in spec:
                name = _f("degree", metric, f"transacts_{d}", w, f"{text}, window {w}")
                exprs.append(f'{sql} AS "{name}"')
                cols.append(name)
    pur = _f("degree", "txn_cnt", "purchases_out", "all", "purchases made")
    cols.append(pur)
    return _dense(
        snap,
        f"""WITH t AS (
            SELECT src_id AS party_id, dst_id AS other, 'out' AS dir, amount, event_time FROM e_TRANSACTS
            UNION ALL SELECT dst_id, src_id, 'in', amount, event_time FROM e_TRANSACTS),
        a AS (SELECT party_id, {", ".join(exprs)} FROM t GROUP BY party_id),
        p AS (SELECT src_id AS party_id, count(*) AS "{pur}" FROM e_PURCHASES GROUP BY 1)
        SELECT * FROM a FULL JOIN p USING (party_id)""",
        cols,
    )


def shared(snap: Snapshot) -> pl.DataFrame:
    dev_cnt = _f("shared", "device_nuniq", "device", "all", "devices used")
    dev_party = _f("shared", "party_nuniq", "device", "all", "other parties on the same devices")
    ident_party = _f("shared", "party_nuniq", "identifier", "all", "other parties with a common identifier")
    return _dense(
        snap,
        f"""WITH u AS (SELECT src_id AS party_id, dst_id AS thing FROM e_USES GROUP BY ALL),
        h AS (SELECT src_id AS party_id, dst_id AS thing FROM e_HAS_IDENTIFIER GROUP BY ALL),
        d AS (SELECT a.party_id, count(DISTINCT a.thing) AS "{dev_cnt}",
                     count(DISTINCT b.party_id) FILTER (b.party_id <> a.party_id) AS "{dev_party}"
              FROM u a LEFT JOIN u b ON a.thing = b.thing AND a.thing IN (SELECT device FROM dev_ok) GROUP BY 1),
        i AS (SELECT a.party_id, count(DISTINCT b.party_id) FILTER (b.party_id <> a.party_id) AS "{ident_party}"
              FROM h a LEFT JOIN h b ON a.thing = b.thing GROUP BY 1)
        SELECT * FROM d FULL JOIN i USING (party_id)""",
        [dev_cnt, dev_party, ident_party],
    )


def khop(snap: Snapshot) -> pl.DataFrame:
    a = (snap.peers > 0).astype(np.float64)
    has_loan, bad = _dpd90(snap)
    income = np.nan_to_num(snap.con.execute("SELECT income FROM party ORDER BY idx").fetchnumpy()["income"].astype(float))
    known = (income > 0).astype(float)
    deg = np.asarray(a.sum(axis=1)).ravel()

    def rate(num: np.ndarray, den: np.ndarray) -> np.ndarray:
        return np.divide(num, den, out=np.zeros_like(num), where=den > 0)

    outcomes = {}
    for kind in OUTCOME_EVENTS:  # peers already known to have churned, been reported, claimed, been charged back
        hit = snap.con.execute(
            f"""SELECT DISTINCT p.idx FROM e_APPLIES_FOR x JOIN n_Event e ON e.node_id = x.dst_id AND e.subtype = '{kind}'
                JOIN party p ON p.node_id = x.src_id"""
        ).fetchnumpy()["idx"]
        flag = np.zeros(a.shape[0])
        flag[hit] = 1
        outcomes[_f("khop", f"{kind}_party_cnt", "peer_h1", "all", f"peers with an observed {kind} event")] = a @ flag
    l1, b1 = a @ has_loan, a @ bad
    l2, b2 = a @ l1, a @ b1  # walk-weighted two-hop
    return _frame(
        snap,
        {
            _f("khop", "neighbor_cnt", "peer_h1", "all", "distinct peers"): deg,
            _f("khop", "loan_party_cnt", "peer_h1", "all", "peers with a loan"): l1,
            _f("khop", "dpd90_party_cnt", "peer_h1", "all", "peers with observed 90+ DPD"): b1,
            _f("khop", "dpd90_party_rate", "peer_h1", "all", "share of borrowing peers with observed 90+ DPD"): rate(b1, l1),
            _f("khop", "dpd90_party_rate", "peer_h2", "all", "same, over two-hop walks"): rate(b2, l2),
            _f("khop", "income_avg", "peer_h1", "all", "mean income of peers with known income"): rate(a @ income, a @ known),
            _f("khop", "interaction_sum", "peer_h1", "all", "interactions with peers"): np.asarray(snap.peers.sum(axis=1)).ravel(),
            **outcomes,
        },
    )


def centrality(snap: Snapshot) -> pl.DataFrame:
    a = snap.peers
    n = a.shape[0]
    out = np.asarray(a.sum(axis=1)).ravel()
    walk = diags(np.divide(1.0, out, out=np.zeros(n), where=out > 0)) @ a
    rank = np.full(n, 1.0 / n)
    for _ in range(30):
        rank = 0.15 / n + 0.85 * (walk.T @ rank + rank[out == 0].sum() / n)
    coo = a.tocoo()
    upper = coo.row < coo.col
    g = ig.Graph(n=n, edges=np.stack([coo.row[upper], coo.col[upper]], axis=1).tolist())
    return _frame(
        snap,
        {
            _f("centrality", "pagerank_score", "peer", "all", "PageRank on the peer graph, times n"): rank * n,
            _f("centrality", "kcore_score", "peer", "all", "k-core number on the peer graph"): np.array(g.coreness(), float),
        },
    )


def community_labels(snap: Snapshot) -> np.ndarray:
    coo = snap.peers.tocoo()
    upper = coo.row < coo.col
    g = ig.Graph(n=snap.peers.shape[0], edges=np.stack([coo.row[upper], coo.col[upper]], axis=1).tolist())
    random.seed(0)  # igraph draws from Python's RNG
    return np.array(g.community_multilevel(weights=coo.data[upper].tolist()).membership)


def community(snap: Snapshot) -> pl.DataFrame:
    """Statistics of the party's community; the raw label is unstable across snapshots and is not a feature."""
    label = community_labels(snap)
    a = (snap.peers > 0).astype(np.float64)
    has_loan, bad = _dpd90(snap)
    size = np.bincount(label).astype(float)
    coo = a.tocoo()
    inside = label[coo.row] == label[coo.col]
    internal = np.bincount(label[coo.row[inside]], minlength=size.size) / 2
    dev = snap.con.execute("SELECT a, b FROM peer WHERE shares_device").fetchnumpy()
    dev_in = label[dev["a"]] == label[dev["b"]]
    dev_edges = np.bincount(label[dev["a"][dev_in]], minlength=size.size).astype(float)
    possible = np.maximum(size * (size - 1) / 2, 1)
    loans = np.bincount(label, has_loan, size.size)
    bads = np.bincount(label, bad, size.size)
    bad_rate = np.divide(bads, loans, out=np.zeros_like(bads), where=loans > 0)
    return _frame(
        snap,
        {
            _f("community", "member_cnt", "peer", "all", "size of the party's community"): size[label],
            _f("community", "density_ratio", "peer", "all", "internal edges over possible edges"): (internal / possible)[label],
            _f("community", "shared_device_ratio", "peer", "all", "device-sharing pairs over possible pairs"): (dev_edges / possible)[
                label
            ],
            _f("community", "dpd90_party_rate", "peer", "all", "community borrowers with observed 90+ DPD"): bad_rate[label],
        },
    )


def embedding(snap: Snapshot) -> pl.DataFrame:
    """FastRP-style: random projection smoothed over 1..3 hops of the degree-normalized peer graph."""
    a = (snap.peers > 0).astype(np.float64)
    n = a.shape[0]
    deg = np.asarray(a.sum(axis=1)).ravel()
    walk = diags(np.divide(1.0, deg, out=np.zeros(n), where=deg > 0)) @ a
    # the projection row of a party depends only on its id, so it is stable across snapshots
    seed = np.array([int(p[3:11], 16) for p in snap.party_ids], dtype=np.uint64)[:, None]
    mult = np.uint64(2654435761) + np.uint64(40503) * np.arange(EMBEDDING_DIM, dtype=np.uint64)[None, :]
    r = (((seed * mult) >> np.uint64(13)) % np.uint64(3)).astype(np.float64) - 1.0
    h1 = walk @ r
    h2 = walk @ h1
    out = 0.5 * h1 + h2 + walk @ h2
    norm = np.linalg.norm(out, axis=1, keepdims=True)
    out = np.divide(out, norm, out=np.zeros_like(out), where=norm > 0)
    return _frame(
        snap,
        {_f("embedding", f"dim{k:03d}", "peer", "all", f"peer-graph embedding component {k}"): out[:, k] for k in range(EMBEDDING_DIM)},
    )


FAMILIES: dict[str, Callable[[Snapshot], pl.DataFrame]] = {
    "profile": profile,
    "degree": degree,
    "shared": shared,
    "khop": khop,
    "centrality": centrality,
    "community": community,
    "embedding": embedding,
}

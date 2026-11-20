"""Point-in-time training tables: population + label + split from the experiment config, features from the store."""

from __future__ import annotations

import duckdb
import polars as pl

from graph_platform import features
from graph_platform.config import Settings
from graph_platform.contracts import BranchContract, GraphScope
from graph_platform.graph import graph_dir
from graph_platform.naming import feature_name

EVENT_AMOUNT = feature_name("party", "profile", "event_amt_val", "application")
EVENT_TERM = feature_name("party", "profile", "event_term_val", "application")
PRIOR_BAD = feature_name("party", "profile", "dpd90_flag", "self")
KEYS = ["party_id", "event_id", "as_of", "event_time", "label", "split"]


def scope_of(settings: Settings, tenant: str, c: BranchContract) -> GraphScope:
    return c.graph_scope or features.load_scope(settings, tenant)


def population(settings: Settings, tenant: str, c: BranchContract) -> pl.DataFrame:
    """One row per population event with label and split.

    as_of is the first day of the event's month, so every feature precedes the event.
    Dropped: events whose performance window is not fully observed, and indeterminate outcomes.
    Split: train period -> train/test by party hash; oot period -> oot; anything else is out.
    """
    pop, lab, sp = c.population, c.label, c.split
    g = graph_dir(settings, tenant, principal="branches")
    con = duckdb.connect()
    for kind, name in (("nodes", "Event"), ("edges", "APPLIES_FOR"), ("edges", "RELATES_TO")):
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{g / kind / (name + '.parquet')}')")
    amount = "".join(f" AND e.amount {op} {v}" for op, v in ((">=", pop.min_amount), ("<=", pop.max_amount)) if v is not None)
    drop_band = (
        f"WHERE worst IS NULL OR worst < {lab.indeterminate_from} OR worst >= {lab.bad_threshold}"
        if lab.indeterminate_from is not None
        else ""
    )
    purge = f" AND event_time + INTERVAL {lab.performance_window_months} MONTH <= DATE '{sp.oot.start}'" if sp.purge_overlap else ""
    return con.sql(
        f"""WITH horizon AS (SELECT max(event_time) AS t FROM Event WHERE subtype = '{lab.outcome_event}'),
        pop AS (
            SELECT a.src_id AS party_id, e.node_id AS event_id, e.event_time,
                   CAST(date_trunc('month', e.event_time) AS DATE) AS as_of, e.amount, e.term_months
            FROM Event e JOIN APPLIES_FOR a ON a.dst_id = e.node_id
            WHERE e.subtype = '{pop.event}'{amount}
              AND e.event_time + INTERVAL {lab.performance_window_months} MONTH <= (SELECT t FROM horizon)),
        scored AS (
            SELECT p.*, max(o.{lab.attribute}) AS worst
            FROM pop p
            LEFT JOIN RELATES_TO r ON r.dst_id = p.event_id
            LEFT JOIN Event o ON o.node_id = r.src_id AND o.subtype = '{lab.outcome_event}'
                 AND o.event_time <= p.event_time + INTERVAL {lab.performance_window_months} MONTH
            GROUP BY ALL),
        labelled AS (SELECT * FROM scored {drop_band})
        SELECT party_id, event_id, as_of, event_time,
               (coalesce(worst, 0) >= {lab.bad_threshold})::INT AS label,
               CASE WHEN event_time::DATE BETWEEN DATE '{sp.oot.start}' AND DATE '{sp.oot.end}' THEN 'oot'
                    WHEN hash(party_id, {sp.seed}) % 1000 < {int(sp.test_fraction * 1000)} THEN 'test'
                    ELSE 'train' END AS split,
               amount AS "{EVENT_AMOUNT}", term_months AS "{EVENT_TERM}"
        FROM labelled
        WHERE event_time::DATE BETWEEN DATE '{sp.train.start}' AND DATE '{sp.train.end}'{purge}
           OR event_time::DATE BETWEEN DATE '{sp.oot.start}' AND DATE '{sp.oot.end}'
        ORDER BY event_time, event_id"""
    ).pl()


def training_table(settings: Settings, tenant: str, c: BranchContract) -> pl.DataFrame:
    """Population joined to every feature family any model of the experiment uses, as of each row's own as_of."""
    scope = scope_of(settings, tenant, c)
    families = list(dict.fromkeys(["profile", *(f for m in c.models for f in m.families)]))
    pop = population(settings, tenant, c)
    dates = pop["as_of"].unique().sort().to_list()
    todo = features.missing(settings, tenant, dates, families, scope)
    if todo:
        features.build(settings, tenant, todo, families, scope)
    parts = [
        pop.filter(pl.col("as_of") == d).join(features.load(settings, tenant, d, families, scope), on="party_id", how="left") for d in dates
    ]
    df = pl.concat(parts, how="diagonal_relaxed")
    return df.filter(pl.col(PRIOR_BAD).fill_null(0) == 0) if c.population.exclude_prior_bad else df


def feature_columns(df: pl.DataFrame, families: list[str]) -> list[str]:
    return [c for c in df.columns if c not in KEYS and c.split("__")[1] in families]

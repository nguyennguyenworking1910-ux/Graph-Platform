"""Build and read point-in-time feature snapshots. One directory per graph scope."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl

from graph_platform.config import Settings
from graph_platform.contracts import GraphScope, load_yaml
from graph_platform.features.families import DESCRIPTIONS, FAMILIES
from graph_platform.features.snapshot import open_snapshot
from graph_platform.graph import graph_dir
from graph_platform.naming import FeatureName
from graph_platform.ontology import Ontology
from graph_platform.storage import Principal, resolve


def load_scope(settings: Settings, tenant: str) -> GraphScope:
    """The tenant's standard scope."""
    return load_yaml(GraphScope, settings.tenant(tenant).graph_scope)


def features_dir(settings: Settings, tenant: str, scope: GraphScope, role: str = "features", mode: str = "r") -> Path:
    base = resolve(settings, Principal(tenant, "platform", role), "features", tenant=tenant, mode=mode)  # type: ignore[arg-type]
    version = (graph_dir(settings, tenant, principal=role) / "VERSION").read_text(encoding="utf-8")
    return base / f"graph_{version}" / scope.scope_id


def compute(graph: Path, as_of: date, ontology: Ontology, scope: GraphScope, families: list[str] | None = None) -> dict[str, pl.DataFrame]:
    snap = open_snapshot(graph, as_of, ontology, scope)
    out = {}
    for name in families or list(FAMILIES):
        df = FAMILIES[name](snap)
        for col in df.columns[1:]:
            assert FeatureName.parse(col).family == name, col
        out[name] = df
    return out


def build(
    settings: Settings, tenant: str, as_of: list[date], families: list[str] | None = None, scope: GraphScope | None = None
) -> dict[str, int]:
    """Writes features/<scope_id>/<family>/as_of=<date>/part.parquet and the scope's registry."""
    scope = scope or load_scope(settings, tenant)
    out = features_dir(settings, tenant, scope, mode="w")
    graph = graph_dir(settings, tenant, principal="features")
    counts = {}
    for d in as_of:
        for family, df in compute(graph, d, settings.ontology_of(tenant), scope, families).items():
            path = out / family / f"as_of={d.isoformat()}" / "part.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            df.write_parquet(path)
            counts[f"{family}/{d.isoformat()}"] = df.width - 1
    path = out / "registry.json"
    known = {r["name"]: r["description"] for r in json.loads(path.read_text())["features"]} if path.exists() else {}
    registry = [{"name": n, **FeatureName.parse(n).__dict__, "description": text} for n, text in sorted((known | DESCRIPTIONS).items())]
    path.write_text(json.dumps({"graph_scope": scope.model_dump(), "features": registry}, indent=2), encoding="utf-8")
    return counts


def load(
    settings: Settings, tenant: str, as_of: date, families: list[str], scope: GraphScope | None = None, role: str = "branches"
) -> pl.DataFrame:
    """One row per party with every feature of the requested families at as_of."""
    base = features_dir(settings, tenant, scope or load_scope(settings, tenant), role=role)
    frames = [pl.read_parquet(base / f / f"as_of={as_of.isoformat()}" / "part.parquet") for f in families]
    out = frames[0]
    for df in frames[1:]:
        out = out.join(df, on="party_id", how="full", coalesce=True)
    return out


def missing(settings: Settings, tenant: str, dates: list[date], families: list[str], scope: GraphScope) -> list[date]:
    base = features_dir(settings, tenant, scope, role="branches")
    return sorted({d for d in dates for f in families if not (base / f / f"as_of={d.isoformat()}" / "part.parquet").exists()})


def month_starts(start: date, end: date, step: int = 1) -> list[date]:
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        out.append(date(y, m, 1))
        y, m = (y + (m + step - 1) // 12, (m + step - 1) % 12 + 1)
    return out

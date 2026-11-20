"""Generate landing files and ground truth for one tenant."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from graph_platform.config import Settings
from graph_platform.contracts import DomainContract, load_yaml
from graph_platform.simulator.behaviors import ANOMALOUS_CORRIDOR, build_facts
from graph_platform.simulator.sources import EMITTERS, camel_dialect
from graph_platform.simulator.world import SimConfig, build_world
from graph_platform.storage import Principal, resolve

# tenants emitting the camelCase schema dialect
CAMEL_TENANTS = {"beta_group"}
EXTRA_SOURCE = ("bnpl", "bnpl_pay")


def _write(df: pl.DataFrame, path: Path, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    {"csv": df.write_csv, "parquet": df.write_parquet, "jsonl": df.write_ndjson}[fmt](path)


def simulate(settings: Settings, cfg: SimConfig, *, extra_source: bool = False) -> dict[str, int]:
    """Returns row counts per `<source>.<table>`."""
    paths = settings.tenant(cfg.tenant)
    sim = Principal(cfg.tenant, "platform", "simulator")
    sources = {
        c.domain: c.sources[0].name
        for c in (load_yaml(DomainContract, p) for p in sorted(paths.domains.glob("*.yaml")))
        if c.domain in EMITTERS
    }
    if extra_source:
        sources = dict([EXTRA_SOURCE])

    world = build_world(cfg)
    facts = build_facts(world)
    counts: dict[str, int] = {}
    ids = []
    for domain, source in sources.items():
        emit, fmt = EMITTERS[domain]
        tables, truth = emit(world, facts, cfg.rng(f"source/{domain}"))
        if cfg.tenant in CAMEL_TENANTS:
            tables = camel_dialect(tables)
        out = resolve(settings, sim, "landing", tenant=cfg.tenant, domain=domain, mode="w") / source
        for name, df in tables.items():
            _write(df, out / f"{name}.{fmt}", fmt)
            counts[f"{source}.{name}"] = df.height
        ids.append(truth.with_columns(domain=pl.lit(domain), source=pl.lit(source)))

    gt = resolve(settings, sim, "ground_truth", tenant=cfg.tenant, mode="w")
    suffix = "_extra" if extra_source else ""
    _write(pl.concat(ids), gt / f"party_ids{suffix}.parquet", "parquet")
    if not extra_source:
        _write(world.persons, gt / "persons.parquet", "parquet")
        _write(world.ring_members, gt / "rings.parquet", "parquet")
        _write(world.merchants, gt / "merchants.parquet", "parquet")
        quasi = facts.tables["quasi_merchants"].select(source_pk=pl.format("M{}", pl.col("merchant").cast(pl.String).str.zfill(6)))
        _write(quasi, gt / "quasi_merchants.parquet", "parquet")
        _write(facts.tables["scalpers"], gt / "scalpers.parquet", "parquet")
        _write(pl.DataFrame({"corridor": [ANOMALOUS_CORRIDOR]}), gt / "corridor.parquet", "parquet")
    return counts

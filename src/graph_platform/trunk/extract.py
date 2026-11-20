"""Pull the tables of a connected source database into landing, one load per run.

The connection URL lives in an environment variable named by the onboarding contract, never in config. A table with a
watermark column is pulled incrementally (rows above the last value seen); a table without one is a full extract.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import sqlalchemy as sa

from graph_platform.config import Settings
from graph_platform.contracts import DomainContract, load_yaml
from graph_platform.storage import Principal, resolve

BATCH = 200_000
_RESTORE = {"datetime": datetime.fromisoformat, "date": date.fromisoformat, "int": int, "float": float, "str": str}


def _keep(value: object) -> dict:
    kind = type(value).__name__
    if kind not in _RESTORE:
        raise TypeError(f"watermark of type {kind} is not supported; use a timestamp, date, integer or text column")
    return {"kind": kind, "value": value.isoformat() if isinstance(value, date) else value}


def pull(settings: Settings, tenant: str, domain: str, source: str, *, now: datetime | None = None) -> dict[str, int]:
    """Rows landed per table by this run."""
    contract = load_yaml(DomainContract, settings.tenant(tenant).domains / f"{domain}.yaml")
    connection = next(s for s in contract.sources if s.name == source).connection
    if connection is None:
        raise ValueError(f"source '{source}' has no connection; its files are delivered to landing")
    land = resolve(settings, Principal(tenant, "platform", "extract"), "landing", tenant=tenant, domain=domain, mode="w") / source
    state_path = land / "_state" / "extract.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    load = (now or datetime.now(UTC)).strftime("load_ts=%Y%m%dT%H%M%S")
    rows: dict[str, int] = {}
    engine = sa.create_engine(os.environ[connection.url_env])
    try:
        with engine.connect() as db:
            for t in connection.tables:
                *schema, name = t.relation.split(".")
                query = sa.select(sa.text("*")).select_from(sa.table(name, schema=".".join(schema) or None))
                if t.table in state:
                    last = state[t.table]
                    query = query.where(sa.column(t.watermark) > _RESTORE[last["kind"]](last["value"]))
                elif t.since is not None:
                    query = query.where(sa.column(t.watermark) > t.since)
                staging = land / "_state" / "staging" / t.table
                shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True)
                rows[t.table], high = 0, None
                for i, batch in enumerate(pl.read_database(query, db, iter_batches=True, batch_size=BATCH, infer_schema_length=None)):
                    batch.write_parquet(staging / f"part-{i:05d}.parquet")
                    rows[t.table] += batch.height
                    if t.watermark and batch.height:
                        top = batch[t.watermark].max()
                        high = top if high is None or top > high else high
                if rows[t.table]:
                    (land / t.table).mkdir(parents=True, exist_ok=True)
                    staging.replace(land / t.table / load)  # a load is visible only when complete
                if high is not None:
                    state[t.table] = _keep(high)
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    finally:
        engine.dispose()
    return rows


def pull_all(settings: Settings, tenant: str, domains: list[str] | None = None) -> dict[str, dict[str, int]]:
    """Pull every connected source of a tenant."""
    out = {}
    for path in sorted(settings.tenant(tenant).domains.glob("*.yaml")):
        contract = load_yaml(DomainContract, Path(path))
        if domains and contract.domain not in domains:
            continue
        for s in contract.sources:
            if s.connection:
                out[s.name] = pull(settings, tenant, contract.domain, s.name)
    return out

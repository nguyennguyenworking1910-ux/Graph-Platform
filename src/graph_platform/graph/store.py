"""Neo4j access. Every node carries its tenant; `uid` = `<tenant>|<node_id>` is the unique key.

Community edition has one database, so tenants share it and are separated by the `tenant` property on every node and
by `uid`. Hard isolation (one database per tenant) needs Neo4j Enterprise or one instance per tenant.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import polars as pl
import yaml
from neo4j import GraphDatabase
from pydantic import BaseModel, ConfigDict

from graph_platform.config import Settings

QUERIES = Path(__file__).parent / "queries"
EDGE_META = ("edge_type", "src_id", "dst_id", "src_type", "dst_type")
BATCH = 20_000
# DuckDB type -> (CSV export expression, Cypher conversion of the CSV string)
CASTS = {
    "VARCHAR": ("{c}", "row.{c}"),
    "BIGINT": ("{c}", "toInteger(row.{c})"),
    "INTEGER": ("{c}", "toInteger(row.{c})"),
    "DOUBLE": ("{c}", "toFloat(row.{c})"),
    "BOOLEAN": ("{c}", "row.{c} = 'true'"),
    "DATE": ("strftime({c}, '%Y-%m-%d')", "date(row.{c})"),
    "TIMESTAMP": ("strftime({c}, '%Y-%m-%dT%H:%M:%S')", "localdatetime(row.{c})"),
    "VARCHAR[]": ("array_to_string({c}, ';')", "split(row.{c}, ';')"),
}


class GraphDbConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str
    user: str
    database: str
    import_dir: Path


def named_query(name: str) -> str:
    return (QUERIES / f"{name}.cypher").read_text(encoding="utf-8")


class GraphStore:
    def __init__(self, settings: Settings) -> None:
        cfg = GraphDbConfig.model_validate(yaml.safe_load((settings.conf_dir / "graph_db.yaml").read_text(encoding="utf-8")))
        self.database = cfg.database
        # a deployment points at its own Neo4j through the environment; graph_db.yaml describes the local one
        import_dir = Path(os.environ.get("GP_NEO4J_IMPORT_DIR", cfg.import_dir))
        self.import_dir = import_dir if import_dir.is_absolute() else settings.root / import_dir
        self.driver = GraphDatabase.driver(os.environ.get("GP_NEO4J_URI", cfg.uri), auth=(cfg.user, os.environ["GP_NEO4J_PASSWORD"]))

    def close(self) -> None:
        self.driver.close()

    def _run(self, cypher: str, **params: object) -> list[dict]:
        """Auto-commit execution, required by `CALL { } IN TRANSACTIONS`."""
        with self.driver.session(database=self.database) as session:
            return session.run(cypher, **params).data()

    def load(self, tenant: str, graph_dir: Path) -> dict[str, int]:
        """Replace the tenant's graph with the Parquet projection."""
        duck = duckdb.connect()
        stage = self.import_dir / tenant
        stage.mkdir(parents=True, exist_ok=True)
        self._run(f"MATCH (n {{tenant: $t}}) CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF {BATCH} ROWS", t=tenant)

        def export(path: Path, select: str, skip: tuple[str, ...], where: str = "") -> tuple[str, str]:
            cols = [(r[0], r[1]) for r in duck.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall() if r[0] not in skip]
            fields = "".join(f", {CASTS[t][0].format(c=c)} AS {c}" for c, t in cols)
            name = f"{path.parent.name}_{path.stem}{abs(hash(where)) % 10**8 if where else ''}.csv"
            duck.execute(f"COPY (SELECT {select}{fields} FROM read_parquet('{path}') {where}) TO '{stage / name}' (FORMAT csv, HEADER)")
            sets = ", ".join(f"x.{c} = {CASTS[t][1].format(c=c)}" for c, t in cols)
            return f"file:///{tenant}/{name}", sets

        for f in sorted((graph_dir / "nodes").glob("*.parquet")):
            label = f.stem
            self._run(f"CREATE CONSTRAINT {label}_uid IF NOT EXISTS FOR (n:{label}) REQUIRE n.uid IS UNIQUE")
            url, sets = export(f, f"'{tenant}|' || node_id AS uid", ("node_type",))
            self._run(
                f"LOAD CSV WITH HEADERS FROM $url AS row CALL (row) {{ CREATE (x:{label} {{uid: row.uid, tenant: $t}}) SET {sets} }} "
                f"IN TRANSACTIONS OF {BATCH} ROWS",
                url=url, t=tenant,
            )  # fmt: skip
        for f in sorted((graph_dir / "edges").glob("*.parquet")):
            for src, dst in duck.execute(f"SELECT DISTINCT src_type, dst_type FROM read_parquet('{f}')").fetchall():
                url, sets = export(
                    f,
                    f"'{tenant}|' || src_id AS src, '{tenant}|' || dst_id AS dst",
                    EDGE_META,
                    f"WHERE src_type = '{src}' AND dst_type = '{dst}'",
                )
                self._run(
                    f"LOAD CSV WITH HEADERS FROM $url AS row CALL (row) {{ MATCH (a:{src} {{uid: row.src}}), (b:{dst} {{uid: row.dst}}) "
                    f"CREATE (a)-[x:{f.stem}]->(b) SET {sets or 'x.tenant = $t'} }} IN TRANSACTIONS OF {BATCH} ROWS",
                    url=url, t=tenant,
                )  # fmt: skip
        for csv in stage.glob("*.csv"):
            csv.unlink()
        return self.counts(tenant)

    def counts(self, tenant: str) -> dict[str, int]:
        nodes = self._run("MATCH (n {tenant: $t}) RETURN labels(n)[0] AS k, count(*) AS c", t=tenant)
        edges = self._run("MATCH (a {tenant: $t})-[r]->() RETURN type(r) AS k, count(*) AS c", t=tenant)
        return {r["k"]: r["c"] for r in [*nodes, *edges]}

    def query(self, tenant: str, cypher: str, params: dict | None = None) -> pl.DataFrame:
        """Run a read query scoped to one tenant: the query receives $tenant and must filter by it."""
        records, _, keys = self.driver.execute_query(cypher, {**(params or {}), "tenant": tenant}, database_=self.database, routing_="r")
        return pl.DataFrame([r.data() for r in records], schema=keys, infer_schema_length=None)

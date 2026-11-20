"""Discover source tables in landing and fingerprint their schemas."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import duckdb


def ident(name: str) -> str:
    """Quote a source-controlled table or column name; names in landed files are untrusted."""
    return '"' + name.replace('"', '""') + '"'


READERS = {
    ".csv": "read_csv({p}, header=true, all_varchar=true, union_by_name=true, hive_partitioning=false)",
    ".parquet": "read_parquet({p}, union_by_name=true, hive_partitioning=false)",
    ".jsonl": "read_json_auto({p}, format='newline_delimited', union_by_name=true, hive_partitioning=false)",
}


@dataclass(frozen=True)
class SourceTable:
    name: str
    paths: tuple[Path, ...]  # every landed file of the table that is in scope
    columns: dict[str, str]  # column -> DuckDB type
    rows: int

    @property
    def stamp(self) -> str:
        """Changes whenever a file is added or rewritten, even with the same row count and schema."""
        stats = [f.stat() for f in self.paths]
        return f"{len(stats)}:{sum(st.st_size for st in stats)}:{max(st.st_mtime_ns for st in stats)}"

    @property
    def reader(self) -> str:
        files = ", ".join("'" + str(f).replace("'", "''") + "'" for f in self.paths)
        return READERS[self.paths[0].suffix].format(p=f"[{files}]")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(repr(sorted(self.columns.items())).encode()).hexdigest()[:16]


def scan(source_dir: Path, snapshot_tables: frozenset[str] = frozenset()) -> dict[str, SourceTable]:
    """A table is `<table>.<ext>` or every file under `<table>/` (one sub-directory per load, e.g. load_date=2026-01-31).

    Append tables read every load. Snapshot tables are full extracts: only the latest load is read.
    """
    files: dict[str, list[Path]] = {}
    for entry in sorted(source_dir.iterdir()):
        if entry.is_file() and entry.suffix in READERS:
            files.setdefault(entry.stem, []).append(entry)
        elif entry.is_dir() and not entry.name.startswith("_"):
            found = sorted(f for f in entry.rglob("*") if f.is_file() and f.suffix in READERS)
            if entry.name in snapshot_tables and found:
                latest = max(f.parent for f in found)
                found = [f for f in found if f.parent == latest]
            if found:
                files.setdefault(entry.name, []).extend(found)
    con = duckdb.connect()
    tables = {}
    for name, paths in files.items():
        t = SourceTable(name, tuple(paths), {}, 0)
        cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {t.reader}").fetchall()}
        rows = con.execute(f"SELECT count(*) FROM {t.reader}").fetchall()[0][0]
        tables[name] = SourceTable(name, tuple(paths), cols, rows)
    return tables


def connect(tables: dict[str, SourceTable]) -> duckdb.DuckDBPyConnection:
    """In-memory connection with one view per source table."""
    con = duckdb.connect()
    for t in tables.values():
        con.execute(f"CREATE VIEW {ident(t.name)} AS SELECT * FROM {t.reader}")
    return con


def drift(tables: dict[str, SourceTable], known: dict[str, dict[str, str]]) -> list[dict]:
    """Schema differences against the columns recorded by the last accepted run."""
    out = []
    for name in sorted(set(tables) | set(known)):
        now = tables[name].columns if name in tables else {}
        was = known.get(name, {})
        for col in sorted(set(now) | set(was)):
            if col not in was:
                out.append({"table": name, "column": col, "change": "added"})
            elif col not in now:
                out.append({"table": name, "column": col, "change": "removed"})
            elif now[col] != was[col]:
                out.append({"table": name, "column": col, "change": "retyped", "from": was[col], "to": now[col]})
    return out

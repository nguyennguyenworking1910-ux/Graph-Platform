"""Onboarding contract: one per business domain of a tenant."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from graph_platform.naming import Snake


class TablePull(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: Snake  # name of the table in landing
    relation: str  # where it lives in the database: "schema.table", or "dataset.table" on BigQuery
    watermark: str | None = None  # column that only grows (created/updated time, sequence); without it every pull is a full extract
    since: datetime | date | int | None = None  # first pull starts above this watermark value: limits history and scan cost

    @model_validator(mode="after")
    def _since_needs_watermark(self) -> TablePull:
        if self.since is not None and self.watermark is None:
            raise ValueError(f"{self.table}: since needs a watermark column")
        return self


class Connection(BaseModel):
    """A source database the platform pulls from: any SQLAlchemy URL (postgresql+psycopg://, bigquery://, oracle+oracledb://)."""

    model_config = ConfigDict(extra="forbid")

    url_env: str  # name of the environment variable that holds the URL; credentials never live in config
    tables: list[TablePull]

    @model_validator(mode="after")
    def _distinct(self) -> Connection:
        names = [t.table for t in self.tables]
        if len(names) != len(set(names)):
            raise ValueError("connection.tables: duplicate landing table names")
        return self


class SourceDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Snake
    description: str = ""
    snapshot_tables: list[str] = []  # full extracts: only the latest load counts; every other table is append-only
    connection: Connection | None = None  # absent: the business delivers files to landing

    @property
    def full_extracts(self) -> frozenset[str]:
        pulled = [t.table for t in self.connection.tables if t.watermark is None] if self.connection else []
        return frozenset([*self.snapshot_tables, *pulled])


class DomainContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Snake
    product_group: Literal["financial", "payment", "entertainment"]
    owner: str
    sla_hours: int = 24
    readers: list[Snake] = []  # other domains of the same tenant allowed to read this one
    sources: list[SourceDecl]

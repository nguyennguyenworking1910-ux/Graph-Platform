"""Paths for platform config and per-tenant data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from graph_platform.ontology import Ontology, extend, load_ontology

ZONES = ("landing", "meta", "canonical", "shared", "features", "branches", "control", "ground_truth")


@dataclass(frozen=True)
class TenantPaths:
    name: str
    conf: Path
    data: Path

    @property
    def domains(self) -> Path:
        return self.conf / "domains"

    @property
    def dictionaries(self) -> Path:
        return self.conf / "dictionaries"

    @property
    def mappings(self) -> Path:
        return self.conf / "mappings"

    @property
    def branches(self) -> Path:
        return self.conf / "branches"

    def context(self, source: str) -> str:
        """What the business wrote about a source, for the agents: conf/tenants/<tenant>/context/<source>.md."""
        note = self.conf / "context" / f"{source}.md"
        return note.read_text(encoding="utf-8") if note.exists() else ""

    @property
    def agents(self) -> Path:
        return self.conf / "agents.yaml"

    @property
    def graph_scope(self) -> Path:
        return self.conf / "graph_scope.yaml"

    def zone(self, zone: str) -> Path:
        assert zone in ZONES, zone
        return self.data / zone


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path

    @classmethod
    def load(cls) -> Settings:
        root = Path(os.environ.get("GP_ROOT", Path.cwd())).resolve()
        return cls(root=root, data_dir=Path(os.environ.get("GP_DATA_DIR", root / "data")).resolve())

    @property
    def conf_dir(self) -> Path:
        return self.root / "conf"

    @property
    def users_path(self) -> Path:
        return self.conf_dir / "users.yaml"

    @cached_property
    def ontology(self) -> Ontology:
        return load_ontology(self.conf_dir / "ontology" / "core.yaml")

    def ontology_of(self, tenant: str) -> Ontology:
        """The core ontology plus what the tenant declares for its own special data (conf/tenants/<tenant>/ontology.yaml)."""
        extension = self.tenant(tenant).conf / "ontology.yaml"
        return extend(self.ontology, extension) if extension.exists() else self.ontology

    def tenants(self) -> list[str]:
        return sorted(p.name for p in (self.conf_dir / "tenants").iterdir() if p.is_dir())

    def tenant(self, name: str) -> TenantPaths:
        conf = self.conf_dir / "tenants" / name
        if not conf.is_dir():
            raise KeyError(f"unknown tenant: {name}")
        return TenantPaths(name=name, conf=conf, data=self.data_dir / name)

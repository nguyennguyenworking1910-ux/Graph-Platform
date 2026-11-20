"""Path resolution with access checks.

Library-level enforcement only: locally this guards against wiring mistakes,
on AWS the same rules are enforced by IAM on S3 prefixes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from graph_platform.config import Settings
from graph_platform.contracts import DomainContract, load_yaml

Mode = Literal["r", "w"]

# platform role -> zones it may read / write
_READ = {
    "landing": {"trunk"},
    "meta": {"trunk", "agents", "control"},
    "canonical": {"trunk", "resolution", "graph", "features", "branches"},
    "shared": {"resolution", "graph", "features", "branches"},
    "features": {"features", "branches"},
    "branches": {"branches"},
    "control": {"control"},
    "ground_truth": {"evaluator"},
}
_WRITE = {
    "landing": {"simulator", "extract"},
    "meta": {"trunk"},
    "canonical": {"trunk"},
    "shared": {"resolution", "graph"},
    "features": {"features"},
    "branches": {"branches"},
    "control": {"control"},
    "ground_truth": {"simulator"},
}
_DOMAIN_ZONES = {"landing", "meta", "canonical"}


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    tenant: str
    kind: Literal["platform", "domain"]
    name: str

    @classmethod
    def parse(cls, text: str) -> Principal:
        tenant, _, rest = text.partition("/")
        kind, _, name = rest.partition(":")
        return cls(tenant, kind, name)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return f"{self.tenant}/{self.kind}:{self.name}"


def resolve(
    settings: Settings,
    principal: Principal,
    zone: str,
    *,
    tenant: str,
    domain: str | None = None,
    mode: Mode = "r",
) -> Path:
    """Return the path for (tenant, zone, domain) or raise AccessDenied."""
    paths = settings.tenant(tenant)
    if (zone in _DOMAIN_ZONES) != (domain is not None):
        raise ValueError(f"zone '{zone}' {'requires' if zone in _DOMAIN_ZONES else 'takes no'} domain")
    if not _allowed(paths.domains, principal, zone, tenant, domain, mode):
        raise AccessDenied(f"{principal} may not {mode} {tenant}/{zone}/{domain or ''}")
    base = paths.zone(zone)
    return base / domain if domain else base


def _allowed(domains_dir: Path, p: Principal, zone: str, tenant: str, domain: str | None, mode: Mode) -> bool:
    if p.tenant != tenant:
        return False
    if p.kind == "platform":
        return p.name in (_READ if mode == "r" else _WRITE)[zone]
    if zone not in _DOMAIN_ZONES:
        return False
    if p.name == domain:
        return mode == "r" or zone == "landing"
    if mode == "w":
        return False
    contract = load_yaml(DomainContract, domains_dir / f"{domain}.yaml")
    return p.name in contract.readers


def canonical_files(settings: Settings, principal: Principal, tenant: str, kind: str, type_name: str) -> list[str]:
    """Canonical parquet files of one node/edge type across every domain the principal may read."""
    base = settings.tenant(tenant).zone("canonical")
    out: list[str] = []
    for d in sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        root = resolve(settings, principal, "canonical", tenant=tenant, domain=d)
        out += sorted(str(f) for f in (root / kind / type_name).glob("*.parquet"))
    return out

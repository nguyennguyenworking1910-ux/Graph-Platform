"""Change proposals: the only way an agent changes a declarative artifact."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from graph_platform.config import Settings
from graph_platform.contracts import ChangeProposal, MappingSpec, load_yaml
from graph_platform.control.audit import control_dir


def _dir(settings: Settings, tenant: str) -> Path:
    path = control_dir(settings, tenant) / "proposals"
    path.mkdir(exist_ok=True)
    return path


def is_destructive(settings: Settings, tenant: str, kind: str, action: str, target: str, payload: dict | None) -> bool:
    """Deleting an artifact, changing ER rules, or a mapping that stops producing a table/type it produced before."""
    if action == "delete" or kind == "er_rules":
        return True
    current = settings.tenant(tenant).conf / target
    if kind != "mapping_spec" or not current.exists():
        return False

    def produced(spec: MappingSpec) -> set[tuple[str, str, str]]:
        return {(t.table, "node", n.type) for t in spec.tables for n in t.nodes} | {
            (t.table, "edge", e.type) for t in spec.tables for e in t.edges
        }

    return bool(produced(load_yaml(MappingSpec, current)) - produced(MappingSpec.model_validate(payload)))


def create(
    settings: Settings, tenant: str, kind: str, action: str, target: str, payload: dict | None, author: str, rationale: str
) -> ChangeProposal:
    now = datetime.now(UTC)
    digest = hashlib.sha1(json.dumps([target, payload, now.isoformat()], default=str).encode()).hexdigest()[:8]
    p = ChangeProposal(
        id=f"{now.strftime('%Y%m%dT%H%M%S')}_{digest}", tenant=tenant, kind=kind, action=action, target=target, payload=payload,
        author=author, rationale=rationale, destructive=is_destructive(settings, tenant, kind, action, target, payload), created_at=now,
    )  # fmt: skip
    save(settings, p)
    return p


def save(settings: Settings, p: ChangeProposal) -> None:
    (_dir(settings, p.tenant) / f"{p.id}.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")


def load(settings: Settings, tenant: str, proposal_id: str) -> ChangeProposal:
    return ChangeProposal.model_validate_json((_dir(settings, tenant) / f"{proposal_id}.json").read_text(encoding="utf-8"))


def load_all(settings: Settings, tenant: str) -> list[ChangeProposal]:
    return [ChangeProposal.model_validate_json(f.read_text(encoding="utf-8")) for f in sorted(_dir(settings, tenant).glob("*.json"))]


def write_artifact(settings: Settings, p: ChangeProposal) -> None:
    """Make the proposal's payload the tenant's current artifact."""
    path = settings.tenant(p.tenant).conf / p.target
    if p.action == "delete":
        path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(p.payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

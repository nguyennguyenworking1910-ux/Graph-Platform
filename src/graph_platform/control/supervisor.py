"""Supervisor: finds work in landing, lets agents propose, runs the gates, applies what passes, audits everything.

One pass (`run_once`) is what a cloud task executes on every landing event and on schedule.
"""

from __future__ import annotations

import json

from graph_platform.agents import roles
from graph_platform.agents.llm import LLM, BudgetExceeded, Refused
from graph_platform.config import Settings
from graph_platform.contracts import AgentsConfig, DataDictionary, DomainContract, MappingSpec, load_yaml
from graph_platform.control import proposals
from graph_platform.control.audit import Audit
from graph_platform.storage import Principal, resolve
from graph_platform.trunk import dictionary as dictionaries
from graph_platform.trunk import extract, pipeline
from graph_platform.trunk.profiler import agent_view

ACTOR = "supervisor"


def _state(settings: Settings, tenant: str, domain: str, source: str) -> tuple[dict | None, dict]:
    """(last applied manifest, current landing tables as {name: {rows, fingerprint}})."""
    who = Principal(tenant, "platform", "control")
    manifest = resolve(settings, who, "meta", tenant=tenant, domain=domain) / source / "manifest.json"
    tables = pipeline.scan_source(settings, tenant, domain, source)
    now = {t.name: {"rows": t.rows, "fingerprint": t.fingerprint, "stamp": t.stamp} for t in tables.values()}
    return (json.loads(manifest.read_text()) if manifest.exists() else None), now


def _waiting_for_human(settings: Settings, tenant: str, target: str) -> bool:
    return any(p.target == target and p.status == "needs_human" for p in proposals.load_all(settings, tenant))


def _onboard_source(settings: Settings, tenant: str, domain: str, source: str, llm: LLM, cfg: AgentsConfig, audit: Audit) -> str:
    paths = settings.tenant(tenant)
    spec_target, dict_target = f"mappings/{source}.yaml", f"dictionaries/{source}.yaml"
    manifest, now = _state(settings, tenant, domain, source)
    if not now:
        return "no_data"
    if manifest and {k: {f: v[f] for f in ("rows", "fingerprint", "stamp")} for k, v in manifest["tables"].items()} == now:
        return "unchanged"
    if _waiting_for_human(settings, tenant, spec_target):
        return "waiting_for_human"

    drift = pipeline.schema_drift(settings, tenant, domain, source)
    prof = pipeline.profile(settings, tenant, domain, source)
    view = agent_view(prof)
    audit.write(ACTOR, "profiled", source=source, tables=len(prof["tables"]), drift=drift)

    if drift or not (paths.conf / dict_target).exists():
        described = roles.describe_source(llm, view, dictionaries.draft(prof), paths.context(source))
        p = proposals.create(
            settings,
            tenant,
            "dictionary",
            "create",
            dict_target,
            described.model_dump(mode="json", exclude_defaults=True),
            "profiler",
            "describe source",
        )
        p.status = "applied"
        proposals.write_artifact(settings, p)
        proposals.save(settings, p)
        audit.write("profiler", "dictionary_applied", source=source, proposal=p.id)
    dictionary = load_yaml(DataDictionary, paths.conf / dict_target)

    spec_path = paths.conf / spec_target
    if spec_path.exists() and not drift:
        result = pipeline.run_mapping(settings, tenant, load_yaml(MappingSpec, spec_path))
        audit.write(ACTOR, "mapping_run", source=source, applied=result.applied, failed=[g.gate for g in result.failed])
        if result.applied:
            return "applied"
        spec, author = (
            roles.repair_mapping(llm, settings.ontology_of(tenant), view, load_yaml(MappingSpec, spec_path), result.failed, drift),
            "maintainer",
        )
    elif spec_path.exists():
        spec, author = (
            roles.repair_mapping(llm, settings.ontology_of(tenant), view, load_yaml(MappingSpec, spec_path), [], drift),
            "maintainer",
        )
    else:
        example = next((load_yaml(MappingSpec, f) for f in sorted(paths.mappings.glob("*.yaml"))), None)
        spec, author = roles.propose_mapping(llm, settings.ontology_of(tenant), view, dictionary, example, paths.context(source)), "mapper"

    for round_no in range(cfg.max_repair_rounds + 1):
        if (spec.source, spec.domain) != (source, domain):
            raise ValueError(f"{author} answered for {spec.domain}/{spec.source}, expected {domain}/{source}")
        payload = spec.model_dump(mode="json", exclude_defaults=True)
        p = proposals.create(
            settings,
            tenant,
            "mapping_spec",
            "update" if spec_path.exists() else "create",
            spec_target,
            payload,
            author,
            f"round {round_no}",
        )
        audit.write(author, "proposed", source=source, proposal=p.id, destructive=p.destructive)
        if p.destructive:
            p.status = "needs_human"
            proposals.save(settings, p)
            audit.write(ACTOR, "needs_human", source=source, proposal=p.id, reason="destructive change")
            return "needs_human"
        result = pipeline.run_mapping(settings, tenant, spec)
        p.gates = result.gates
        if result.applied:
            p.status = "applied"
            proposals.write_artifact(settings, p)
            proposals.save(settings, p)
            audit.write(ACTOR, "applied", source=source, proposal=p.id, counts=result.counts)
            return "applied"
        last = round_no == cfg.max_repair_rounds
        p.status = "needs_human" if last else "rejected"
        proposals.save(settings, p)
        audit.write(ACTOR, "needs_human" if last else "gates_failed", source=source, proposal=p.id, failed=[g.gate for g in result.failed])
        if last:
            return "needs_human"
        spec, author = roles.repair_mapping(llm, settings.ontology_of(tenant), view, spec, result.failed, drift), "maintainer"
    return "needs_human"


def run_once(settings: Settings, tenant: str, llm: LLM) -> dict[str, str]:
    """One supervision pass over every declared source. Returns {source: outcome}."""
    from graph_platform.graph import build_graph
    from graph_platform.resolution import resolve_parties

    audit = Audit(settings, tenant)
    cfg = load_yaml(AgentsConfig, settings.tenant(tenant).agents)
    out: dict[str, str] = {}
    for path in sorted(settings.tenant(tenant).domains.glob("*.yaml")):
        contract = load_yaml(DomainContract, path)
        for src in contract.sources:
            try:
                if src.connection:
                    audit.write(ACTOR, "extracted", source=src.name, rows=extract.pull(settings, tenant, contract.domain, src.name))
                out[src.name] = _onboard_source(settings, tenant, contract.domain, src.name, llm, cfg, audit)
            except BudgetExceeded as exc:
                out[src.name] = "budget_exceeded"
                audit.write(ACTOR, "skipped", source=src.name, reason=str(exc))
            except (Refused, ValueError) as exc:
                out[src.name] = "needs_human"
                audit.write(ACTOR, "needs_human", source=src.name, reason=str(exc)[:500])
            except Exception as exc:  # one broken source must not stop the pass or hide what already changed
                out[src.name] = "failed"
                audit.write(ACTOR, "failed", source=src.name, error=f"{type(exc).__name__}: {exc}"[:500])
    if "applied" in out.values():
        resolved = resolve_parties(settings, tenant)
        counts = build_graph(settings, tenant)
        audit.write(
            ACTOR,
            "graph_rebuilt",
            parties=resolved["global_party_id"].n_unique(),
            edges=sum(v for k, v in counts.items() if k.startswith("edges.")),
        )
    audit.write(ACTOR, "pass_done", outcomes=out)
    return out


def decide(settings: Settings, tenant: str, proposal_id: str, approve: bool, by: str) -> str:
    """A human's decision on a needs_human proposal. An approved mapping must still pass its gates."""
    audit = Audit(settings, tenant)
    p = proposals.load(settings, tenant, proposal_id)
    if p.status != "needs_human":
        raise ValueError(f"proposal {proposal_id} is {p.status}, not needs_human")
    if not approve:
        p.status = "rejected"
    elif p.kind == "mapping_spec" and p.action != "delete":
        result = pipeline.run_mapping(settings, tenant, MappingSpec.model_validate(p.payload))
        p.gates = result.gates
        if result.applied:
            p.status = "applied"
            proposals.write_artifact(settings, p)
    else:
        p.status = "applied"
        proposals.write_artifact(settings, p)
    proposals.save(settings, p)
    audit.write(by, "human_decision", proposal=p.id, approve=approve, status=p.status)
    return p.status


__all__ = ["decide", "run_once"]

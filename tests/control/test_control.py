"""Control-plane behaviour with a scripted model. Model quality itself needs live Bedrock access and is not tested here."""

import json
import re
import shutil

import polars as pl
import pytest
import yaml

from graph_platform.agents.llm import BedrockLLM, BudgetExceeded
from graph_platform.config import Settings
from graph_platform.contracts import AgentsConfig, MappingSpec, load_yaml
from graph_platform.control import monitor, proposals
from graph_platform.control.audit import Audit
from graph_platform.control.supervisor import decide, run_once
from graph_platform.simulator import SimConfig, simulate
from tests.conftest import ROOT

T = "alpha_group"
BNPL_SPEC = {
    "source": "bnpl_pay", "domain": "bnpl",
    "tables": [
        {"table": "shoppers", "nodes": [
            {"type": "Party", "subtype": "person", "key": {"col": "shopper_ref"}, "unique": True,
             "time": {"col": "enrolled_on", "tf": ["parse_timestamp"]}, "attrs": {"full_name": {"col": "display_name", "tf": ["trim"]}}},
            {"type": "Identifier", "subtype": "phone", "key": {"col": "msisdn", "tf": ["normalize_phone"]}},
            {"type": "Identifier", "subtype": "email", "key": {"col": "mail", "tf": ["normalize_email"]}},
            {"type": "Identifier", "subtype": "national_id", "key": {"col": "citizen_no", "tf": ["digits_only"]}}],
         "edges": [
            {"type": "HAS_IDENTIFIER", "src": {"type": "Party", "key": {"col": "shopper_ref"}},
             "dst": {"type": "Identifier", "subtype": kind, "key": {"col": col, "tf": [tf]}},
             "time": {"col": "enrolled_on", "tf": ["parse_timestamp"]}}
            for kind, col, tf in (("phone", "msisdn", "normalize_phone"), ("email", "mail", "normalize_email"),
                                  ("national_id", "citizen_no", "digits_only"))]},
        {"table": "orders", "nodes": [
            {"type": "Party", "subtype": "merchant", "key": {"col": "store_code"}},
            {"type": "Identifier", "subtype": "merchant_code", "key": {"col": "store_code"}}],
         "edges": [
            {"type": "HAS_IDENTIFIER", "src": {"type": "Party", "key": {"col": "store_code"}},
             "dst": {"type": "Identifier", "subtype": "merchant_code", "key": {"col": "store_code"}},
             "time": {"const": "2023-01-01 00:00:00", "tf": ["parse_timestamp"]}},
            {"type": "TRANSACTS", "src": {"type": "Party", "key": {"col": "shopper_ref"}},
             "dst": {"type": "Party", "key": {"col": "store_code"}}, "key": {"col": "order_ref"},
             "time": {"col": "ordered_at", "tf": ["parse_timestamp"]}, "attrs": {"amount": {"col": "basket_value"}}}]},
    ],
}  # fmt: skip


class ScriptedLLM:
    """Answers with the reference artifacts. `bad_rounds` makes the mapper wrong that many times first."""

    def __init__(self, bad_rounds: int = 0, rename: dict[str, str] | None = None, drop_table: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bad_rounds, self.rename, self.drop_table = bad_rounds, rename or {}, drop_table

    def complete(self, agent: str, system: str, user: str) -> str:
        self.calls.append((agent, system + user))
        if agent == "profiler":
            return user.split("Draft:\n", 1)[1]
        if agent == "triage":
            return json.dumps({"diagnosis": "x", "action": "needs_human", "reason": "y"})
        source = re.search(r"source `(\w+)`", user) or re.search(r'"source":\s*"(\w+)"', user)
        name = source.group(1)
        path = ROOT / "conf/tenants" / T / "mappings" / f"{name}.yaml"
        spec = yaml.safe_load(path.read_text()) if path.exists() else json.loads(json.dumps(BNPL_SPEC))
        text = json.dumps(spec)
        for old, new in self.rename.items():
            text = text.replace(f'"col": "{old}"', f'"col": "{new}"')
        spec = json.loads(text)
        if self.drop_table:
            spec["tables"] = [t for t in spec["tables"] if t["table"] != self.drop_table]
        if self.bad_rounds > 0:
            self.bad_rounds -= 1
            spec["tables"][0]["nodes"][0]["key"] = {"col": "no_such_column"}
        return "```json\n" + json.dumps(spec) + "\n```"


@pytest.fixture
def env(tmp_path):
    """A tenant with landing data and onboarding contracts, but no dictionaries and no mapping specs."""
    shutil.copytree(ROOT / "conf", tmp_path / "conf", ignore=shutil.ignore_patterns("dictionaries", "mappings"))
    s = Settings(root=tmp_path, data_dir=tmp_path / "data")
    simulate(s, SimConfig(tenant=T, parties=1500))
    return s


def test_agents_onboard_every_source_without_hand_written_artifacts(env):
    llm = ScriptedLLM()
    out = run_once(env, T, llm)
    assert len(out) == 7 and set(out.values()) == {"applied"}
    assert len(list(env.tenant(T).mappings.glob("*.yaml"))) == 7 and len(list(env.tenant(T).dictionaries.glob("*.yaml"))) == 7
    assert (env.data_dir / T / "shared/graph/nodes/Party.parquet").exists()
    applied = [p for p in proposals.load_all(env, T) if p.status == "applied"]
    assert {p.kind for p in applied} == {"dictionary", "mapping_spec"} and all(p.author in ("profiler", "mapper") for p in applied)
    actions = [r["action"] for r in Audit(env, T).rows()]
    assert actions.count("applied") == 7 and "graph_rebuilt" in actions and actions[-1] == "pass_done"

    again = ScriptedLLM()
    assert set(run_once(env, T, again).values()) == {"unchanged"} and again.calls == []


def test_prompts_never_contain_raw_identifiers(env):
    llm = ScriptedLLM()
    run_once(env, T, llm)
    sent = "\n".join(text for _, text in llm.calls)
    users = pl.read_parquet(env.data_dir / T / "landing/payment/wallet_pay/users.parquet")
    cust = pl.read_csv(env.data_dir / T / "landing/credit/credit_core/customers.csv", infer_schema_length=0)
    values = [
        *users["phone_number"].head(200),
        *users["email"].drop_nulls().head(200),
        *cust["id_card"].head(200),
        *cust["full_nm"].head(200),
    ]
    assert not [v for v in values if v in sent]
    assert "99999" in sent  # masked shapes are what the model sees


def test_failed_gates_are_repaired_then_escalated(env):
    out = run_once(env, T, ScriptedLLM(bad_rounds=1))
    assert set(out.values()) == {"applied"}
    by_status = {s: [p for p in proposals.load_all(env, T) if p.status == s and p.kind == "mapping_spec"] for s in ("rejected", "applied")}
    assert len(by_status["rejected"]) == 1 and by_status["rejected"][0].gates[0].gate == "spec_valid"
    assert {p.author for p in by_status["applied"]} == {"mapper", "maintainer"}


def test_persistent_failure_waits_for_a_human(env):
    out = run_once(env, T, ScriptedLLM(bad_rounds=99))
    assert set(out.values()) == {"needs_human"}
    assert not (env.data_dir / T / "canonical").exists() and not list(env.tenant(T).mappings.glob("*.yaml"))
    waiting = [p for p in proposals.load_all(env, T) if p.status == "needs_human"]
    assert len(waiting) == 7
    rounds = load_yaml(AgentsConfig, env.tenant(T).agents).max_repair_rounds
    assert len([p for p in proposals.load_all(env, T) if p.kind == "mapping_spec"]) == 7 * (rounds + 1)
    idle = ScriptedLLM()
    assert set(run_once(env, T, idle).values()) == {"waiting_for_human"} and idle.calls == []
    assert decide(env, T, waiting[0].id, approve=False, by="duc") == "rejected"


def test_unseen_source_and_schema_drift(env):
    run_once(env, T, ScriptedLLM())
    # a business unit declares a new domain and drops its data: nothing else is written by hand
    (env.tenant(T).domains / "bnpl.yaml").write_text(
        "domain: bnpl\nproduct_group: financial\nowner: bnpl-team\nsources:\n  - name: bnpl_pay\n"
    )
    simulate(env, SimConfig(tenant=T, parties=1500), extra_source=True)
    out = run_once(env, T, ScriptedLLM())
    assert out["bnpl_pay"] == "applied" and all(v == "unchanged" for k, v in out.items() if k != "bnpl_pay")
    resolved = pl.read_parquet(env.data_dir / T / "shared/party_resolved.parquet")
    linked = (
        resolved.group_by("global_party_id").agg(pl.col("source_system").unique().alias("s")).filter(pl.col("s").list.contains("bnpl_pay"))
    )
    assert (linked["s"].list.len() > 1).mean() > 0.5  # bnpl shoppers are tied to their other identities

    path = env.data_dir / T / "landing/movies/movie_tix/members.csv"
    pl.read_csv(path, infer_schema_length=0).rename({"phone": "phone_no"}).write_csv(path)
    out = run_once(env, T, ScriptedLLM(rename={"phone": "phone_no"}))
    assert out["movie_tix"] == "applied"
    spec = load_yaml(MappingSpec, env.tenant(T).mappings / "movie_tix.yaml")
    assert "phone_no" in json.dumps(spec.model_dump(mode="json"))
    drift_rows = [r for r in Audit(env, T).rows() if r["action"] == "profiled" and r.get("drift")]
    assert {d["column"] for d in drift_rows[-1]["drift"]} == {"phone", "phone_no"}


def test_destructive_change_needs_human_approval(env):
    run_once(env, T, ScriptedLLM())
    before = (env.tenant(T).mappings / "ota_travel.yaml").read_text()
    path = env.data_dir / T / "landing/ota/ota_travel/bookings.jsonl"
    pl.read_ndjson(path).rename({"device": "device_fp"}).write_ndjson(path)
    out = run_once(env, T, ScriptedLLM(drop_table="bookings"))  # the agent "fixes" drift by dropping the table
    assert out["ota_travel"] == "needs_human" and (env.tenant(T).mappings / "ota_travel.yaml").read_text() == before
    p = next(p for p in proposals.load_all(env, T) if p.status == "needs_human")
    assert p.destructive and p.author == "maintainer"
    assert decide(env, T, p.id, approve=True, by="duc") == "applied"
    assert "bookings" not in (env.tenant(T).mappings / "ota_travel.yaml").read_text()
    assert Audit(env, T).rows()[-1]["action"] == "human_decision"


def test_daily_token_budget_stops_model_calls(env, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    audit = Audit(env, T)
    cfg = load_yaml(AgentsConfig, env.tenant(T).agents)
    audit.write("mapper", "llm_call", input_tokens=cfg.daily_token_limit, output_tokens=1)
    llm = BedrockLLM(cfg, audit)
    with pytest.raises(BudgetExceeded):
        llm.complete("mapper", "s", "u")
    assert audit.rows()[-1]["action"] == "budget_exceeded"
    out = run_once(env, T, llm)
    assert set(out.values()) == {"budget_exceeded"}


def test_feature_drift_monitor(built):
    from datetime import date

    from graph_platform import features

    features.build(built, T, [date(2023, 9, 1), date(2025, 6, 1)], ["degree"])
    drift = monitor.feature_drift(built, T, "degree", date(2023, 9, 1), date(2025, 6, 1))
    assert drift and max(drift.values()) > monitor.DRIFT_THRESHOLD  # activity ramps up over two years
    same = monitor.feature_drift(built, T, "degree", date(2025, 6, 1), date(2025, 6, 1))
    assert max(same.values()) < 1e-9

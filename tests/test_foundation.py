import copy

import pytest
import yaml
from pydantic import ValidationError

from graph_platform.contracts import SCHEMAS, DomainContract, Expr, MappingSpec, Tf, load_yaml
from graph_platform.naming import FeatureName, feature_name
from graph_platform.ontology import OntologyError, load_ontology
from graph_platform.storage import AccessDenied, Principal, resolve


def _raw(settings):
    return yaml.safe_load((settings.conf_dir / "ontology" / "core.yaml").read_text())


def _load(tmp_path, raw):
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_ontology(p)


def test_core_ontology_loads(settings):
    o = settings.ontology
    assert {n.name for n in o.nodes} >= {
        "Party",
        "Account",
        "Device",
        "Identifier",
        "Item",
        "Event",
    }
    assert o.node("Identifier").scope == "global"
    assert o.edge("TRANSACTS").attribute("amount").type == "double"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["edges"][0].update(target=["Nope"]), "unknown target node types"),
        (lambda r: r["nodes"].append(copy.deepcopy(r["nodes"][0])), "duplicate node types"),
        (lambda r: r["nodes"][0].update(name="party"), "nodes.0.name"),
        (lambda r: r["edges"][0].update(name="Owns"), "edges.0.name"),
        (
            lambda r: r["nodes"][0]["attributes"].append({"name": "node_id", "type": "string"}),
            "reserved",
        ),
        (lambda r: r["nodes"][0].pop("time_column"), "time_column"),
        (lambda r: r["lineage_columns"].remove("domain"), "lineage_columns must include 'domain'"),
    ],
)
def test_invalid_ontology_rejected(settings, tmp_path, mutate, message):
    raw = _raw(settings)
    mutate(raw)
    with pytest.raises(OntologyError, match=message):
        _load(tmp_path, raw)


def test_feature_name_standard():
    assert feature_name("party", "degree", "cnt", "transacts_out", "90d") == "party__degree__cnt__transacts_out__90d"
    assert FeatureName.parse("party__khop__default_rate__refers_any_h1__all").metric == "default_rate"
    assert FeatureName.parse("party__embedding__dim007__all__all").family == "embedding"
    for bad in (
        "party_degree_cnt",  # wrong separators
        "party__degree__Cnt__all__90d",  # case
        "party__mystuff__cnt__all__90d",  # unknown family
        "party__degree__cnt__all__7d",  # unknown window
        "party__degree__transactions__all__90d",  # metric without statistic suffix
    ):
        with pytest.raises(ValueError):
            FeatureName.parse(bad)


def test_contract_names_must_be_snake_case():
    base = {
        "domain": "credit",
        "product_group": "financial",
        "owner": "x",
        "sources": [{"name": "s"}],
    }
    DomainContract.model_validate(base)
    for bad in ({"domain": "Credit"}, {"sources": [{"name": "credit-core"}]}):
        with pytest.raises(ValidationError):
            DomainContract.model_validate(base | bad)


def test_mapping_spec_transforms_are_whitelisted():
    assert Tf.model_validate("parse_timestamp:%d/%m/%Y").arg == "%d/%m/%Y"
    assert Tf.model_validate({"map_values": {"M": "male"}}).arg == {"M": "male"}
    with pytest.raises(ValidationError):
        Tf.model_validate("exec:rm -rf /")
    with pytest.raises(ValidationError):
        Expr.model_validate({"col": "a", "const": 1})
    with pytest.raises(ValidationError):
        MappingSpec.model_validate({"source": "s", "domain": "d", "tables": [{"table": "t", "sql": "x"}]})


def test_schemas_export():
    for model in SCHEMAS.values():
        assert model.model_json_schema()["type"] == "object"


def test_all_tenant_contracts_valid(settings):
    assert settings.tenants() == ["alpha_group", "beta_group"]
    alpha = sorted(p.stem for p in settings.tenant("alpha_group").domains.glob("*.yaml"))
    assert alpha == ["credit", "insurance", "merchant", "movies", "ota", "payment", "savings"]
    for t in settings.tenants():
        for p in settings.tenant(t).domains.glob("*.yaml"):
            assert load_yaml(DomainContract, p).domain == p.stem


A = "alpha_group"


def P(text):
    return Principal.parse(text)


def test_domain_access(settings):
    own = resolve(settings, P(f"{A}/domain:credit"), "canonical", tenant=A, domain="credit")
    assert own == settings.data_dir / A / "canonical" / "credit"
    # payment lists credit as reader; credit lists nobody
    resolve(settings, P(f"{A}/domain:credit"), "canonical", tenant=A, domain="payment")
    with pytest.raises(AccessDenied):
        resolve(settings, P(f"{A}/domain:payment"), "canonical", tenant=A, domain="credit")
    with pytest.raises(AccessDenied):
        resolve(settings, P(f"{A}/domain:credit"), "canonical", tenant=A, domain="payment", mode="w")
    with pytest.raises(AccessDenied):
        resolve(settings, P(f"{A}/domain:credit"), "shared", tenant=A)


def test_tenant_isolation(settings):
    for principal in (
        "beta_group/domain:credit",
        "beta_group/platform:trunk",
        "beta_group/platform:branches",
    ):
        for zone, domain in (
            ("canonical", "credit"),
            ("landing", "credit"),
            ("shared", None),
            ("features", None),
        ):
            with pytest.raises(AccessDenied):
                resolve(settings, P(principal), zone, tenant=A, domain=domain)


def test_ground_truth_only_for_simulator_and_evaluator(settings):
    resolve(settings, P(f"{A}/platform:simulator"), "ground_truth", tenant=A, mode="w")
    resolve(settings, P(f"{A}/platform:evaluator"), "ground_truth", tenant=A)
    for role in ("trunk", "resolution", "graph", "features", "branches"):
        with pytest.raises(AccessDenied):
            resolve(settings, P(f"{A}/platform:{role}"), "ground_truth", tenant=A)

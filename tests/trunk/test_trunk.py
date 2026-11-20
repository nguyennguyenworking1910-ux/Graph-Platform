import json

import polars as pl
import pytest

from graph_platform.config import Settings
from graph_platform.contracts import DataDictionary, MappingSpec, load_yaml
from graph_platform.contracts.mapping_spec import When
from graph_platform.simulator import SimConfig, simulate
from graph_platform.trunk import landing, pipeline
from graph_platform.trunk.profiler import mask
from tests.conftest import ROOT

T = "alpha_group"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Isolated data + a private copy of tenant conf so tests never touch repo dictionaries."""
    import shutil

    root = tmp_path_factory.mktemp("root")
    shutil.copytree(ROOT / "conf", root / "conf", ignore=shutil.ignore_patterns("dictionaries"))
    s = Settings(root=root, data_dir=root / "data")
    simulate(s, SimConfig(tenant=T, parties=3000))
    return s, pipeline.onboard(s, T)


def _spec(s, source):
    return load_yaml(MappingSpec, s.tenant(T).mappings / f"{source}.yaml")


def test_all_sources_pass_the_trunk(env):
    s, results = env
    assert len(results) == 7
    for name, r in results.items():
        assert r.applied, (name, [g.model_dump() for g in r.failed])
    canon = s.data_dir / T / "canonical"
    assert sorted(p.name for p in canon.iterdir()) == [
        "credit",
        "insurance",
        "merchant",
        "movies",
        "ota",
        "payment",
        "savings",
    ]


def test_canonical_rows_follow_the_ontology(env):
    s, _ = env
    party = pl.read_parquet(s.data_dir / T / "canonical/credit/nodes/Party/credit_core.parquet")
    assert party["node_id"].str.starts_with("credit_core:Party:C").all() and party["node_id"].n_unique() == party.height
    assert party["birth_date"].dtype == pl.Date and party["valid_from"].null_count() == 0
    assert set(party["gender"].unique()) <= {"male", "female"}
    assert set(party.columns) >= {"source_system", "domain", "source_pk", "ingested_at", "full_name", "income"}
    phone = pl.read_parquet(s.data_dir / T / "canonical/credit/nodes/Identifier/credit_core.parquet").filter(pl.col("subtype") == "phone")
    assert phone["node_id"].str.contains(r"^Identifier:phone:84\d{9}$").all()
    tx = pl.read_parquet(s.data_dir / T / "canonical/payment/edges/TRANSACTS/wallet_pay.parquet")
    assert tx["event_time"].null_count() == 0 and tx["amount"].dtype == pl.Float64
    assert tx["event_time"].min().year == 2023


def test_idempotent(env):
    s, results = env
    again = pipeline.run_mapping(s, T, _spec(s, "credit_core"))
    assert again.applied and again.counts == results["credit_core"].counts
    files = list((s.data_dir / T / "canonical/credit").rglob("*.parquet"))
    assert len(files) == len({f.parent for f in files})  # one file per type for the source


def test_rejected_spec_publishes_nothing(env):
    s, _ = env
    before = (s.data_dir / T / "canonical/movies/nodes/Party/movie_tix.parquet").stat().st_mtime_ns

    unknown_col = _spec(s, "movie_tix")
    unknown_col.tables[0].nodes[0].key.col = "no_such_column"
    r = pipeline.run_mapping(s, T, unknown_col)
    assert not r.applied and r.failed[0].gate == "spec_valid"

    wrong_key = _spec(s, "movie_tix")
    wrong_key.tables[0].nodes[0].key.col = "joined"  # a date is not a member key
    r = pipeline.run_mapping(s, T, wrong_key)
    assert not r.applied and {g.gate for g in r.failed} >= {"duplicate_rate"}

    bad_time = _spec(s, "movie_tix")
    bad_time.tables[0].nodes[0].time.tf[0].arg = "%d-%m-%Y"
    r = pipeline.run_mapping(s, T, bad_time)
    assert not r.applied and {g.gate for g in r.failed} >= {"time_parse_rate"}

    dangling = _spec(s, "movie_tix")
    dangling.tables[1].edges[0].src.key.col = "ticket_id"  # edge source that is not a member
    r = pipeline.run_mapping(s, T, dangling)
    assert not r.applied and {g.gate for g in r.failed} >= {"endpoint_coverage"}

    assert (s.data_dir / T / "canonical/movies/nodes/Party/movie_tix.parquet").stat().st_mtime_ns == before


def test_schema_drift_detected_and_blocked(env):
    s, _ = env
    assert pipeline.schema_drift(s, T, "ota", "ota_travel") == []
    path = s.data_dir / T / "landing/ota/ota_travel/travellers.jsonl"
    original = path.read_text()
    try:
        pl.read_ndjson(path).rename({"contact_phone": "phone_no"}).write_ndjson(path)
        changes = pipeline.schema_drift(s, T, "ota", "ota_travel")
        assert {(c["column"], c["change"]) for c in changes} == {("contact_phone", "removed"), ("phone_no", "added")}
        r = pipeline.run_mapping(s, T, _spec(s, "ota_travel"))
        assert not r.applied and r.failed[0].gate == "spec_valid"
    finally:
        path.write_text(original)


def test_dictionary_describes_every_column_and_links_to_ontology(env):
    s, _ = env
    for path in s.tenant(T).dictionaries.glob("*.yaml"):
        d = load_yaml(DataDictionary, path)
        domain = d.domain
        tables = landing.scan(s.data_dir / T / "landing" / domain / d.source)
        for t in tables.values():
            assert set(t.columns) == {c.name for c in d.table(t.name).columns}
    d = load_yaml(DataDictionary, s.tenant(T).dictionaries / "credit_core.yaml")
    cols = {c.name: c for c in d.table("customers").columns}
    assert cols["mobile"].semantic_type == "phone" and cols["mobile"].pii
    assert cols["cust_id"].semantic_type == "id" and cols["cust_id"].maps_to == "Party.key"
    assert cols["full_nm"].maps_to == "Party.full_name"
    assert d.table("loans").column("app_id").references == "loan_applications.app_id"


def test_mapping_requires_dictionary(env):
    s, _ = env
    path = s.tenant(T).dictionaries / "insure_hub.yaml"
    text = path.read_text()
    path.unlink()
    try:
        r = pipeline.run_mapping(s, T, _spec(s, "insure_hub"))
        assert not r.applied and "dictionary" in r.failed[0].detail["problems"][0]
    finally:
        path.write_text(text)


def test_agent_profile_has_no_raw_pii(env):
    s, _ = env
    assert mask("Nguyen Van An 0901 234") == "Aaaaaa Aaa Aa 9999 999"
    meta = s.data_dir / T / "meta/credit/credit_core"
    raw = json.loads((meta / "profile.json").read_text())
    view = (meta / "profile_agent.json").read_text()
    customers = next(t for t in raw["tables"] if t["name"] == "customers")
    for col in ("full_nm", "mobile", "email_addr", "id_card"):
        c = next(x for x in customers["columns"] if x["name"] == col)
        for v in [c["min"], c["max"], *c["top_values"]]:
            assert v not in view
    assert '"decision"' in view and "APPROVED" in view  # low-cardinality codes stay readable


def test_hostile_column_and_table_names_stay_data(tmp_path):
    """Names inside a landed file are quoted, never executed."""
    from graph_platform.trunk.profiler import profile_source

    marker = tmp_path / "pwned.txt"
    evil = f'id") FROM "t"; COPY (SELECT 42) TO \'{marker}\'; --'
    src = tmp_path / "src"
    src.mkdir()
    pl.DataFrame({evil: ["1", "2"], 'na"me': ["a", "b"]}).write_csv(src / "t.csv")
    prof = profile_source("s", "d", landing.scan(src))
    assert not marker.exists()
    assert {c["name"] for c in prof["tables"][0]["columns"]} == {evil, 'na"me'}


def test_spec_cannot_loosen_gates_or_target_another_source(env):
    import pydantic

    s, _ = env
    raw = _spec(s, "movie_tix").model_dump(mode="json")
    with pytest.raises(pydantic.ValidationError):
        MappingSpec.model_validate(raw | {"gates": {"max_duplicate_rate": 1.0}})

    stolen = _spec(s, "movie_tix")
    stolen.source = "credit_core"  # declared by another domain
    r = pipeline.run_mapping(s, T, stolen)
    assert not r.applied and "not declared by domain" in r.failed[0].detail["problems"][0]

    timeless = _spec(s, "movie_tix")
    timeless.tables[1].edges[0].time = timeless.tables[1].edges[0].time.model_copy(update={"col": None, "const": "2023-01-01", "tf": []})
    r = pipeline.run_mapping(s, T, timeless)
    assert not r.applied and any("constants are allowed only" in x for x in r.failed[0].detail["problems"])


def test_partitioned_loads_append_and_snapshot(env):
    """Daily loads land as sub-directories: append tables union every load, snapshot tables keep only the latest."""
    import shutil

    s, _ = env
    src = s.data_dir / T / "landing/movies/movie_tix"
    backup = s.data_dir / T / "_movie_tix_backup"
    shutil.copytree(src, backup)
    contract = s.tenant(T).domains / "movies.yaml"
    original = contract.read_text()
    try:
        members, tickets = pl.read_csv(src / "members.csv", infer_schema_length=0), pl.read_csv(src / "tickets.csv", infer_schema_length=0)
        (src / "members.csv").unlink()
        (src / "tickets.csv").unlink()
        half = tickets.height // 2
        for day, part in (("2025-01-01", tickets[:half]), ("2025-01-02", tickets[half:])):
            (src / "tickets" / f"load_date={day}").mkdir(parents=True)
            part.write_csv(src / "tickets" / f"load_date={day}" / "part.csv")
        for day in ("2025-01-01", "2025-01-02"):  # the member list is re-extracted in full every day
            (src / "members" / f"load_date={day}").mkdir(parents=True)
            members.write_csv(src / "members" / f"load_date={day}" / "part.csv")
        contract.write_text(original.replace("  - name: movie_tix\n", "  - name: movie_tix\n    snapshot_tables: [members]\n"))

        tables = pipeline.scan_source(s, T, "movies", "movie_tix")
        assert tables["tickets"].rows == tickets.height and len(tables["tickets"].paths) == 2
        assert tables["members"].rows == members.height and len(tables["members"].paths) == 1
        r = pipeline.run_mapping(s, T, _spec(s, "movie_tix"))
        assert r.applied and r.counts["edges.PURCHASES"] == tickets.height

        contract.write_text(original)  # treated as append, two identical extracts double every member
        r = pipeline.run_mapping(s, T, _spec(s, "movie_tix"))
        assert not r.applied and "duplicate_rate" in {g.gate for g in r.failed}
    finally:
        contract.write_text(original)
        shutil.rmtree(src)
        shutil.move(backup, src)


def test_one_table_with_several_kinds_of_record_is_split_by_a_column(env):
    """A ledger mixes transfers and purchases; `when` sends each kind to its own relationship."""
    s, _ = env
    spec = _spec(s, "wallet_pay")
    transfers = next(t for t in spec.tables if t.table == "transfers")
    edge = next(e for e in transfers.edges if e.type == "TRANSACTS")
    all_rows = pipeline.run_mapping(s, T, spec)
    assert all_rows.applied, all_rows.failed
    edge.when = When(col="channel", is_in=["qr"])
    only_qr = pipeline.run_mapping(s, T, spec)
    assert only_qr.applied, only_qr.failed
    assert 0 < only_qr.counts["edges.TRANSACTS"] < all_rows.counts["edges.TRANSACTS"]
    edge.when = When(col="no_such_column", is_in=["x"])
    bad = pipeline.run_mapping(s, T, spec)
    assert not bad.applied and any("unknown column 'no_such_column'" in p for p in bad.failed[0].detail["problems"])
    edge.when = None
    assert pipeline.run_mapping(s, T, spec).applied  # leave the shared environment as it was

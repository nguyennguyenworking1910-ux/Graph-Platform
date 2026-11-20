"""A source database is pulled into landing and goes through the same trunk as delivered files. Runs on a real Postgres."""

import os
import shutil
from datetime import datetime

import pgserver
import polars as pl
import pydantic
import pytest
import sqlalchemy as sa
import yaml

from graph_platform.config import Settings
from graph_platform.contracts.onboarding import Connection
from graph_platform.trunk import extract, pipeline
from tests.conftest import ROOT

T = "alpha_group"
SPEC = {
    "source": "loyalty_db", "domain": "loyalty",
    "tables": [
        {
            "table": "members",
            "nodes": [
                {"type": "Party", "subtype": "person", "key": {"col": "member_no"}, "unique": True, "time": {"col": "joined_at"},
                 "attrs": {"city": {"col": "home_city"}}},
                {"type": "Identifier", "subtype": "phone", "key": {"col": "phone", "tf": ["normalize_phone"]}},
            ],
            "edges": [
                {"type": "HAS_IDENTIFIER", "src": {"type": "Party", "key": {"col": "member_no"}}, "time": {"col": "joined_at"},
                 "dst": {"type": "Identifier", "subtype": "phone", "key": {"col": "phone", "tf": ["normalize_phone"]}}},
            ],
        },
        {
            "table": "redemptions",
            "nodes": [{"type": "Item", "subtype": "voucher", "key": {"col": "reward_code"}, "attrs": {"title": {"col": "reward_code"}}}],
            "edges": [
                {"type": "PURCHASES", "src": {"type": "Party", "key": {"col": "member_no"}},
                 "dst": {"type": "Item", "key": {"col": "reward_code"}}, "key": {"col": "redemption_id"},
                 "time": {"col": "redeemed_at"}, "attrs": {"amount": {"col": "points"}}},
            ],
        },
    ],
}  # fmt: skip


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    engine = sa.create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg://"))
    with engine.begin() as c:
        for statement in (
            "CREATE SCHEMA crm",
            "CREATE TABLE crm.members (member_no text, phone text, home_city text, joined_at timestamp)",
            "CREATE TABLE crm.redemptions (redemption_id bigint, member_no text, reward_code text, points numeric, redeemed_at timestamp)",
            "INSERT INTO crm.members VALUES ('M1', '0901234567', 'hanoi', '2024-01-05'), ('M2', '0907654321', 'hue', '2024-02-01')",
            "INSERT INTO crm.redemptions VALUES (1, 'M1', 'R10', 500, '2024-03-01 10:00'), (2, 'M2', 'R10', 500, '2024-03-02 11:00')",
        ):
            c.execute(sa.text(statement))
    yield engine
    engine.dispose()
    server.cleanup()


@pytest.fixture
def env(tmp_path, db, monkeypatch):
    shutil.copytree(ROOT / "conf", tmp_path / "conf", ignore=shutil.ignore_patterns("dictionaries"))
    s = Settings(root=tmp_path, data_dir=tmp_path / "data")
    contract = {
        "domain": "loyalty", "product_group": "payment", "owner": "loyalty-team",
        "sources": [{
            "name": "loyalty_db",
            "connection": {
                "url_env": "GP_TEST_LOYALTY_URL",
                "tables": [
                    {"table": "members", "relation": "crm.members"},
                    {"table": "redemptions", "relation": "crm.redemptions", "watermark": "redeemed_at"},
                ],
            },
        }],
    }  # fmt: skip
    (s.tenant(T).domains / "loyalty.yaml").write_text(yaml.safe_dump(contract))
    (s.tenant(T).mappings / "loyalty_db.yaml").write_text(yaml.safe_dump(SPEC))
    monkeypatch.setenv("GP_TEST_LOYALTY_URL", db.url.render_as_string(hide_password=False))
    return s


def test_database_source_lands_and_maps_like_files(env):
    assert extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 1, 1)) == {"members": 2, "redemptions": 2}
    result = pipeline.onboard(env, T, ["loyalty"])["loyalty_db"]
    assert result.applied, result.failed
    assert result.counts["nodes.Party"] == 2 and result.counts["edges.PURCHASES"] == 2
    bought = pl.read_parquet(env.data_dir / T / "canonical/loyalty/edges/PURCHASES/loyalty_db.parquet")
    assert bought["amount"].sum() == 1000 and bought["event_time"].dtype == pl.Datetime


def test_watermark_pulls_only_new_rows_and_full_extracts_replace(env, db):
    extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 1, 1))
    assert extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 1, 2))["redemptions"] == 0  # nothing new
    with db.begin() as c:
        c.execute(sa.text("INSERT INTO crm.redemptions VALUES (3, 'M1', 'R20', 900, '2024-04-01 09:00')"))
        c.execute(sa.text("INSERT INTO crm.members VALUES ('M3', '0911111111', 'vinh', '2024-04-01')"))
    try:
        assert extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 1, 3)) == {"members": 3, "redemptions": 1}
        tables = pipeline.scan_source(env, T, "loyalty", "loyalty_db")
        assert tables["redemptions"].rows == 3  # appended loads add up
        assert tables["members"].rows == 3  # a full extract counts once, however often it is pulled
        assert pipeline.onboard(env, T, ["loyalty"])["loyalty_db"].counts["edges.PURCHASES"] == 3
    finally:
        with db.begin() as c:
            c.execute(sa.text("DELETE FROM crm.redemptions WHERE redemption_id = 3"))
            c.execute(sa.text("DELETE FROM crm.members WHERE member_no = 'M3'"))


def test_first_pull_can_start_from_a_date(env):
    path = env.tenant(T).domains / "loyalty.yaml"
    contract = yaml.safe_load(path.read_text())
    contract["sources"][0]["connection"]["tables"][1]["since"] = datetime(2024, 3, 1, 12)
    path.write_text(yaml.safe_dump(contract))
    assert extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 1, 1))["redemptions"] == 1  # only the row after noon
    with pytest.raises(pydantic.ValidationError, match="since needs a watermark"):
        Connection(url_env="U", tables=[{"table": "a", "relation": "p.a", "since": 5}])


@pytest.mark.skipif("GP_TEST_BIGQUERY_URL" not in os.environ, reason="set GP_TEST_BIGQUERY_URL=bigquery://<project> to run")
def test_bigquery_public_sample_is_pulled_incrementally(env):
    """Reads Google's public sample table only; needs the bigquery extra and application default credentials."""
    pulls = [{"table": "works", "relation": "bigquery-public-data.samples.shakespeare", "watermark": "corpus_date", "since": 1600}]
    source = {"name": "public_samples", "connection": {"url_env": "GP_TEST_BIGQUERY_URL", "tables": pulls}}
    contract = {"domain": "library", "product_group": "entertainment", "owner": "demo", "sources": [source]}
    (env.tenant(T).domains / "library.yaml").write_text(yaml.safe_dump(contract))
    first = extract.pull(env, T, "library", "public_samples", now=datetime(2025, 1, 1))["works"]
    assert 0 < first < 164656  # `since` cut the earliest works
    assert extract.pull(env, T, "library", "public_samples", now=datetime(2025, 1, 2))["works"] == 0
    assert pipeline.scan_source(env, T, "library", "public_samples")["works"].rows == first


def test_credentials_stay_out_of_config_and_files_only_sources_are_refused(env):
    assert "postgresql" not in (env.tenant(T).domains / "loyalty.yaml").read_text()
    with pytest.raises(ValueError, match="no connection"):
        extract.pull(env, T, "credit", "credit_core")
    twice = [{"table": "a", "relation": "p.a"}, {"table": "a", "relation": "p.b"}]
    with pytest.raises(pydantic.ValidationError, match="duplicate landing table"):
        Connection(url_env="U", tables=twice)


def test_special_data_has_a_place_and_what_is_left_out_is_reported(env, db):
    """A unit's own fields become tenant attributes, a wide table rides along whole, and unused columns are named."""
    from datetime import date

    from graph_platform import sdk
    from graph_platform.features.snapshot import open_snapshot
    from graph_platform.graph import build_graph, graph_dir
    from graph_platform.resolution import resolve_parties

    with db.begin() as c:
        c.execute(sa.text("ALTER TABLE crm.members ADD COLUMN tier text, ADD COLUMN lifetime_points numeric, ADD COLUMN staff_note text"))
        c.execute(sa.text("UPDATE crm.members SET tier = 'gold', lifetime_points = 12000, staff_note = 'x' WHERE member_no = 'M1'"))
        c.execute(sa.text("CREATE TABLE crm.member_scores (member_no text, scored_at timestamp, full_name text, s1 numeric, s2 numeric)"))
        c.execute(sa.text("INSERT INTO crm.member_scores VALUES ('M1', '2024-05-01', 'A', 0.1, 7), ('M1', '2024-06-01', 'A', 0.4, 9)"))
    try:
        paths = env.tenant(T)
        contract = yaml.safe_load((paths.domains / "loyalty.yaml").read_text())
        contract["sources"][0]["connection"]["tables"].append(
            {"table": "member_scores", "relation": "crm.member_scores", "watermark": "scored_at"}
        )
        (paths.domains / "loyalty.yaml").write_text(yaml.safe_dump(contract))
        extension = {
            "nodes": {"Party": {"attributes": [{"name": "loyalty_tier", "type": "string"}, {"name": "lifetime_points", "type": "double"}]}}
        }
        (paths.conf / "ontology.yaml").write_text(yaml.safe_dump(extension))
        spec = yaml.safe_load(yaml.safe_dump(SPEC))
        spec["tables"][0]["nodes"][0]["attrs"] |= {"loyalty_tier": {"col": "tier"}, "lifetime_points": {"col": "lifetime_points"}}
        spec["tables"].append(
            {"table": "member_scores", "attached": {"name": "loyalty_scores", "party": {"col": "member_no"}, "time": {"col": "scored_at"}}}
        )
        (paths.mappings / "loyalty_db.yaml").write_text(yaml.safe_dump(spec))

        extract.pull(env, T, "loyalty", "loyalty_db", now=datetime(2025, 2, 1))
        result = pipeline.onboard(env, T, ["loyalty"])["loyalty_db"]
        assert result.applied, result.failed
        assert result.unused_columns == {"members": ["staff_note"]}  # named, not silently dropped
        assert result.counts["tables.loyalty_scores"] == 2

        resolve_parties(env, T)
        build_graph(env, T)
        party = pl.read_parquet(graph_dir(env, T) / "nodes/Party.parquet")
        assert party.filter(pl.col("loyalty_tier") == "gold")["lifetime_points"].to_list() == [12000.0]
        snap = open_snapshot(graph_dir(env, T), date(2025, 1, 1), env.ontology_of(T), features_scope(env))
        assert dict(snap.party_extra) == {"loyalty_tier": "string", "lifetime_points": "double"}
        assert "loyalty_tier" not in [a.name for a in env.ontology_of("beta_group").node("Party").attributes]  # another tenant is untouched

        import os

        os.environ["GP_ROOT"], os.environ["GP_DATA_DIR"] = str(env.root), str(env.data_dir)
        try:
            early, late = sdk.party_table(T, "loyalty_scores", date(2024, 5, 15)), sdk.party_table(T, "loyalty_scores", date(2024, 7, 1))
        finally:
            del os.environ["GP_ROOT"], os.environ["GP_DATA_DIR"]
        assert early["s1"].cast(pl.Float64).to_list() == [0.1] and late["s1"].cast(pl.Float64).to_list() == [
            0.4
        ]  # as known at the date asked
        assert "full_name" not in late.columns and late["party_id"][0].startswith("gp_")  # personal columns stay behind
    finally:
        with db.begin() as c:
            c.execute(sa.text("DROP TABLE crm.member_scores"))
            c.execute(sa.text("ALTER TABLE crm.members DROP COLUMN tier, DROP COLUMN lifetime_points, DROP COLUMN staff_note"))


def features_scope(settings):
    from graph_platform import features

    return features.load_scope(settings, T)

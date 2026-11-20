import hashlib

import numpy as np
import polars as pl
import pytest

from graph_platform.config import Settings
from graph_platform.simulator import SimConfig, simulate
from graph_platform.simulator.behaviors import build_facts
from graph_platform.simulator.world import build_world

N = 4000


def _hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.*"))}


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    from tests.conftest import ROOT

    s = Settings(root=ROOT, data_dir=tmp_path_factory.mktemp("data"))
    counts = simulate(s, SimConfig(tenant="alpha_group", parties=N))
    return s, counts


def test_deterministic(sim, tmp_path):
    s, _ = sim
    other = Settings(root=s.root, data_dir=tmp_path)
    simulate(other, SimConfig(tenant="alpha_group", parties=N))
    assert _hashes(s.data_dir / "alpha_group") == _hashes(tmp_path / "alpha_group")
    simulate(other, SimConfig(tenant="alpha_group", parties=N, seed=8))
    assert _hashes(s.data_dir / "alpha_group") != _hashes(tmp_path / "alpha_group")


def test_seven_sources_with_distinct_schemas(sim):
    s, counts = sim
    landing = s.data_dir / "alpha_group" / "landing"
    sources = sorted(p.name for d in landing.iterdir() for p in d.iterdir())
    assert sources == [
        "credit_core", "insure_hub", "merchant_portal", "movie_tix", "ota_travel", "savings_invest", "wallet_pay",
    ]  # fmt: skip
    assert all(v > 0 for v in counts.values())
    id_cols = {
        "cust_id": pl.read_csv(landing / "credit/credit_core/customers.csv", n_rows=1),
        "user_id": pl.read_parquet(landing / "payment/wallet_pay/users.parquet", n_rows=1),
        "holder_no": pl.read_csv(landing / "insurance/insure_hub/policy_holders.csv", n_rows=1),
        "client_id": pl.read_parquet(landing / "savings/savings_invest/clients.parquet", n_rows=1),
        "traveller_id": pl.read_ndjson(landing / "ota/ota_travel/travellers.jsonl").head(1),
        "member_id": pl.read_csv(landing / "movies/movie_tix/members.csv", n_rows=1),
    }
    for col, df in id_cols.items():
        assert col in df.columns


def test_ground_truth_outside_landing(sim):
    s, _ = sim
    gt = s.data_dir / "alpha_group" / "ground_truth"
    ids = pl.read_parquet(gt / "party_ids.parquet")
    assert set(ids["domain"]) == {"credit", "payment", "insurance", "savings", "ota", "movies"}
    assert pl.read_parquet(gt / "rings.parquet").height > 0
    landing_cols = {c for p in (s.data_dir / "alpha_group/landing").rglob("*.parquet") for c in pl.read_parquet_schema(p)}
    assert not landing_cols & {"person", "risk_net", "ring_id", "community"}


def test_identifier_noise_present(sim):
    s, _ = sim
    cust = pl.read_csv(s.data_dir / "alpha_group/landing/credit/credit_core/customers.csv", infer_schema_length=0)
    assert cust["cust_id"].n_unique() < cust.height  # duplicate rows
    assert cust["mobile"].str.starts_with("+84").any() and cust["mobile"].str.starts_with("0").any()
    assert cust["email_addr"].null_count() > 0


def test_default_signal_lives_in_network():
    w = build_world(SimConfig(tenant="alpha_group", parties=30_000))
    loans = build_facts(w).tables["loans"].filter(pl.col("approved"))
    person, default = loans["person"].to_numpy(), loans["default"].to_numpy()
    ring = w.persons["ring_id"].to_numpy() >= 0
    rate = default[~ring[person]].mean()
    assert 0.04 < rate < 0.20

    defaulter = np.zeros(w.n)
    defaulter[person[default]] = 1
    exposure = w.neighbor_mean(defaulter)[person]
    clean = ~ring[person]
    hi, lo = exposure > np.quantile(exposure[clean], 0.8), exposure <= np.quantile(exposure[clean], 0.5)
    assert default[clean & hi].mean() > 1.5 * default[clean & lo].mean()
    assert default[ring[person]].mean() > 0.6


def test_second_tenant_uses_another_dialect(sim, tmp_path):
    s, _ = sim
    other = Settings(root=s.root, data_dir=tmp_path)
    counts = simulate(other, SimConfig(tenant="beta_group", parties=1500))
    assert {k.split(".")[0] for k in counts} == {"lendbook", "paystream", "cinemax"}
    cols = pl.read_csv(tmp_path / "beta_group/landing/credit/lendbook/tbl_customers.csv", n_rows=1).columns
    assert "custId" in cols and "cust_id" not in cols
    assert not (tmp_path / "alpha_group").exists()


def test_extra_source(sim, tmp_path):
    s, _ = sim
    other = Settings(root=s.root, data_dir=tmp_path)
    counts = simulate(other, SimConfig(tenant="alpha_group", parties=1500), extra_source=True)
    assert set(counts) == {"bnpl_pay.shoppers", "bnpl_pay.orders"}

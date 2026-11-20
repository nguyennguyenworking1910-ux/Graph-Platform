"""Hidden world: people, merchants, social graph, latent risk, fraud rings."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import polars as pl

from graph_platform.simulator.catalog import CINEMAS, shop_name

FAMILY = ["Nguyen", "Tran", "Le", "Pham", "Hoang", "Phan", "Vu", "Dang", "Bui", "Do", "Ho", "Ngo"]
MIDDLE = ["Van", "Thi", "Minh", "Duc", "Ngoc", "Quang", "Thanh", "Hong", "Xuan", "Gia", "Bao"]
GIVEN = [
    "An", "Binh", "Chau", "Dung", "Giang", "Ha", "Hieu", "Hung", "Khanh", "Lan", "Linh", "Long",
    "Mai", "Nam", "Nga", "Phuc", "Quan", "Son", "Tam", "Thao", "Trang", "Tuan", "Vy", "Yen",
]  # fmt: skip
CITIES = [
    "hanoi", "ho_chi_minh", "da_nang", "hai_phong", "can_tho", "hue", "nha_trang", "vung_tau",
    "bien_hoa", "da_lat", "quy_nhon", "vinh", "thai_nguyen", "nam_dinh", "buon_ma_thuot",
]  # fmt: skip
OCCUPATIONS = [
    "office",
    "worker",
    "driver",
    "trader",
    "teacher",
    "engineer",
    "freelancer",
    "farmer",
]
OCC_RISK = np.array([-0.3, 0.2, 0.3, 0.1, -0.4, -0.5, 0.4, 0.2])
MERCHANT_CATEGORIES = [
    "grocery",
    "restaurant",
    "fashion",
    "electronics",
    "pharmacy",
    "fuel",
    "cafe",
]

EPOCH_START = np.datetime64("2023-01-01", "s")


@dataclass(frozen=True)
class SimConfig:
    tenant: str
    parties: int = 20_000
    months: int = 36
    seed: int = 7
    merchant_ratio: float = 0.02
    ring_ratio: float = 1 / 2000  # rings per party
    # default model: logit = c0 + w_obs*observable + w_net*latent_network + w_exp*exposure
    c0: float = -3.1
    w_obs: float = 0.7
    w_net: float = 1.0
    w_exp: float = 2.5

    def rng(self, stream: str) -> np.random.Generator:
        return np.random.default_rng([self.seed, zlib.crc32(f"{self.tenant}/{stream}".encode())])

    def month_start(self, m: np.ndarray | int) -> np.ndarray:
        """Epoch seconds of the first day of month offset m."""
        return (np.datetime64("2023-01", "M") + np.asarray(m)).astype("datetime64[s]").astype(np.int64)

    @property
    def end_ts(self) -> int:
        return int(self.month_start(self.months))


@dataclass
class World:
    cfg: SimConfig
    persons: pl.DataFrame
    merchants: pl.DataFrame
    social: np.ndarray  # (E, 2) undirected person index pairs, a < b
    ring_members: pl.DataFrame  # ring_id, person

    @property
    def n(self) -> int:
        return self.persons.height

    def neighbor_mean(self, values: np.ndarray) -> np.ndarray:
        a, b = self.social[:, 0], self.social[:, 1]
        total = np.bincount(a, values[b], self.n) + np.bincount(b, values[a], self.n)
        deg = np.bincount(a, minlength=self.n) + np.bincount(b, minlength=self.n)
        return total / np.maximum(deg, 1)


def _social_graph(rng: np.random.Generator, community: np.ndarray) -> np.ndarray:
    """Dense inside communities, sparse across. Persons are sorted by community."""
    n = community.size
    starts = np.flatnonzero(np.r_[True, community[1:] != community[:-1]])
    sizes = np.diff(np.r_[starts, n])
    lo, size = np.repeat(starts, sizes), np.repeat(sizes, sizes)

    k_in = rng.poisson(5, n)
    src = np.repeat(np.arange(n), k_in)
    dst = np.repeat(lo, k_in) + rng.integers(0, np.repeat(size, k_in))
    k_out = rng.poisson(1.2, n)
    src2 = np.repeat(np.arange(n), k_out)
    dst2 = rng.integers(0, n, src2.size)

    a, b = np.r_[src, src2], np.r_[dst, dst2]
    keep = a != b
    pairs = np.stack([np.minimum(a, b)[keep], np.maximum(a, b)[keep]], axis=1)
    return np.unique(pairs, axis=0)


def build_world(cfg: SimConfig) -> World:
    rng = cfg.rng("world")
    n = cfg.parties

    community = np.sort(rng.integers(0, max(n // 60, 1), n))
    household = np.cumsum(rng.random(n) < 0.45)  # consecutive persons share a household
    social = _social_graph(rng, community)

    income_z = rng.normal(0, 1, n)
    occupation = rng.integers(0, len(OCCUPATIONS), n)
    age = rng.integers(20, 65, n)
    risk_obs = -0.6 * income_z + OCC_RISK[occupation] - 0.01 * (age - 40)

    # latent network risk: community effect + own noise, smoothed over neighbors (homophily)
    comm_effect = rng.normal(0, 1, community.max() + 1)[community]
    own = 0.7 * comm_effect + 0.7 * rng.normal(0, 1, n)
    a, b = social[:, 0], social[:, 1]
    deg = np.maximum(np.bincount(a, minlength=n) + np.bincount(b, minlength=n), 1)
    nb = (np.bincount(a, own[b], n) + np.bincount(b, own[a], n)) / deg
    risk_net = 0.5 * own + 0.5 * nb
    risk_net = (risk_net - risk_net.mean()) / risk_net.std()

    # fraud rings: consecutive members inside one community, sharing devices
    n_rings = max(int(n * cfg.ring_ratio), 1)
    ring_start = rng.choice(n - 20, n_rings, replace=False)
    ring_size = rng.integers(5, 13, n_rings)
    ring_id = np.full(n, -1)
    for r, (s, k) in enumerate(zip(ring_start, ring_size, strict=True)):
        ring_id[s : s + k] = r
    in_ring = ring_id >= 0

    birth = (np.datetime64("2023-01-01") - (age * 365 + rng.integers(0, 365, n))).astype("datetime64[D]")
    phone = rng.permutation(np.arange(300_000_000, 300_000_000 + 3 * n))[:n]
    phone_alt = np.where(rng.random(n) < 0.12, phone + 3 * n, phone)  # second SIM
    device_a = np.arange(n) * 2
    device_b = np.where(rng.random(n) < 0.35, device_a + 1, device_a)
    # ring members log in from the ring's shared device
    device_b = np.where(in_ring, 2 * n + ring_id, device_b)

    name = pl.Series(rng.choice(FAMILY, n)) + " " + pl.Series(rng.choice(MIDDLE, n)) + " " + pl.Series(rng.choice(GIVEN, n))
    idx = np.arange(n)
    persons = pl.DataFrame(
        {
            "person": idx,
            "full_name": name,
            "birth_date": birth,
            "gender": rng.choice(["M", "F"], n),
            "city": rng.choice(CITIES, n),
            "income": np.round(np.exp(16.2 + 0.5 * income_z), -4),
            "occupation": np.array(OCCUPATIONS)[occupation],
            "national_id": 1_000_000_000_00 + rng.permutation(10 * n)[:n],
            "phone": phone,
            "phone_alt": phone_alt,
            "community": community,
            "household": household,
            "device_a": device_a,
            "device_b": device_b,
            "risk_obs": risk_obs,
            "risk_net": risk_net,
            "ring_id": ring_id,
        }
    ).with_columns(
        email=pl.format(
            "{}.{}@{}",
            pl.col("full_name").str.to_lowercase().str.replace_all(" ", ""),
            pl.col("person"),
            pl.Series(rng.choice(["gmail.com", "yahoo.com", "outlook.com"], n)),
        )
    )

    m = max(int(n * cfg.merchant_ratio), 5)
    merchants = pl.DataFrame(
        {
            "merchant": np.arange(m),
            "owner": rng.choice(n, m, replace=False),
            "category": rng.choice(MERCHANT_CATEGORIES, m),
            "city": rng.choice(CITIES, m),
            "tax_id": 8_000_000_000 + rng.permutation(10 * m)[:m],
            "popularity": rng.zipf(1.6, m).astype(np.float64),
        }
    )
    # the first merchants are cinema sites; every other merchant is an invented shop of its category
    category = [*["cinema"] * min(len(CINEMAS), max(m // 10, 2)), *merchants["category"]][:m]
    nth = pl.Series(category).to_frame("c").select(nth=pl.int_range(pl.len()).over("c"))["nth"].to_list()
    names = [CINEMAS[i] if c == "cinema" else shop_name(c, i) for c, i in zip(category, nth, strict=True)]
    merchants = merchants.with_columns(category=pl.Series(category), biz_name=pl.Series(names))

    ring_members = pl.DataFrame({"ring_id": ring_id[in_ring], "person": idx[in_ring]})
    return World(cfg, persons, merchants, social, ring_members)

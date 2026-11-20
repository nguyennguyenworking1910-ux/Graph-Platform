"""Behavior over time, expressed on hidden person/merchant indexes. Timestamps are epoch seconds."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from graph_platform.simulator.catalog import DESTINATIONS, GENRES, MOVIE_TITLES, MOVIES_PER_GENRE, TRIP_KINDS
from graph_platform.simulator.world import World

PARTICIPATION = {
    "payment": 0.80, "credit": 0.35, "insurance": 0.25, "savings": 0.30,
    "ota": 0.30, "movies": 0.50, "bnpl": 0.20,
}  # fmt: skip
P2P_PER_PARTY, MERCHANT_PAY_PER_PARTY, B2B_PER_PARTY = 25, 20, 1
QUASI_PAYERS, QUASI_REPEATS = 8, 25
CORRIDORS = ["SG", "TH", "KR", "JP", "US", "AU", "KY"]
CORRIDOR_SHARE = [0.3, 0.2, 0.15, 0.15, 0.12, 0.079, 0.001]
ANOMALOUS_CORRIDOR = "KY"
MONTH_S = 30 * 86400
N_MOVIES, N_TRIPS = len(MOVIE_TITLES), len(DESTINATIONS) * len(TRIP_KINDS)


@dataclass
class Facts:
    member: dict[str, np.ndarray]  # domain -> bool per person
    joined: dict[str, np.ndarray]  # domain -> ts per person
    tables: dict[str, pl.DataFrame]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def _between(rng: np.random.Generator, lo: np.ndarray, hi: int) -> np.ndarray:
    return (lo + rng.random(lo.size) * (hi - lo)).astype(np.int64)


def _random_neighbor(rng: np.random.Generator, w: World) -> np.ndarray:
    """One random neighbor per person, -1 when isolated."""
    both = np.r_[w.social, w.social[:, ::-1]]
    both = both[rng.permutation(both.shape[0])]
    out = np.full(w.n, -1)
    first = np.unique(both[:, 0], return_index=True)
    out[first[0]] = both[first[1], 1]
    return out


def _loans(rng: np.random.Generator, w: World, member: np.ndarray, joined: np.ndarray) -> pl.DataFrame:
    cfg, p = w.cfg, w.persons
    ring = p["ring_id"].to_numpy() >= 0
    risk_obs, risk_net = p["risk_obs"].to_numpy(), p["risk_net"].to_numpy()

    who = np.flatnonzero(member)
    person = np.repeat(who, 1 + rng.poisson(0.5, who.size))
    app_ts = _between(rng, joined[person], cfg.end_ts - 2 * MONTH_S)
    month = (app_ts.astype("datetime64[s]").astype("datetime64[M]") - np.datetime64("2023-01", "M")).astype(int)
    approved = rng.random(person.size) < _sigmoid(2.0 - 0.8 * risk_obs[person])

    a, b = w.social[:, 0], w.social[:, 1]
    deg = np.maximum(np.bincount(a, minlength=w.n) + np.bincount(b, minlength=w.n), 1)
    person_default_month = np.full(w.n, 10**6)
    default = np.zeros(person.size, bool)
    offset = np.zeros(person.size, int)
    for m in range(cfg.months):
        cur = np.flatnonzero(approved & (month == m))
        if cur.size == 0:
            continue
        seen = (person_default_month <= m).astype(float)
        exposure = (np.bincount(a, seen[b], w.n) + np.bincount(b, seen[a], w.n)) / deg
        who_m = person[cur]
        logit = cfg.c0 + cfg.w_obs * risk_obs[who_m] + cfg.w_net * risk_net[who_m] + cfg.w_exp * exposure[who_m]
        prob = np.where(ring[who_m], 0.85, _sigmoid(logit))
        default[cur] = rng.random(cur.size) < prob
        offset[cur] = np.where(ring[who_m], rng.integers(3, 5, cur.size), rng.integers(3, 11, cur.size))
        hit = cur[default[cur]]
        np.minimum.at(person_default_month, person[hit], m + offset[hit])

    return pl.DataFrame(
        {
            "person": person,
            "app_ts": app_ts,
            "amount": np.round(np.exp(rng.normal(16.5, 0.7, person.size)), -5),
            "term": rng.choice([6, 12, 18, 24], person.size),
            "approved": approved,
            "default": default,
            "default_offset": offset,  # months from disbursement to first 90+ DPD
            "use_shared_device": rng.random(person.size) < 0.5,
        }
    ).with_row_index("loan")


def build_facts(w: World) -> Facts:
    cfg, p, n = w.cfg, w.persons, w.n
    rng = cfg.rng("behaviors")
    ring = p["ring_id"].to_numpy() >= 0
    end = cfg.end_ts

    member, joined = {}, {}
    for domain, rate in PARTICIPATION.items():
        member[domain] = rng.random(n) < rate
        joined[domain] = _between(rng, np.full(n, cfg.month_start(0)), int(cfg.month_start(12)))
    member["payment"] |= ring
    member["credit"] |= ring
    t: dict[str, pl.DataFrame] = {}

    # --- payment: P2P along social edges, plus ring cycles
    pay, pj = member["payment"], joined["payment"]
    edges = w.social[pay[w.social[:, 0]] & pay[w.social[:, 1]]]
    pick = rng.choice(edges.shape[0], P2P_PER_PARTY * n, p=(g := rng.gamma(0.7, 1, edges.shape[0])) / g.sum())
    flip = rng.random(pick.size) < 0.5
    src = np.where(flip, edges[pick, 1], edges[pick, 0])
    dst = np.where(flip, edges[pick, 0], edges[pick, 1])
    p2p = pl.DataFrame(
        {
            "src": src,
            "dst": dst,
            "amount": np.round(np.exp(rng.normal(12.5, 1.0, pick.size)), -3),
            "ts": _between(rng, np.maximum(pj[src], pj[dst]), end),
            "cross_border": rng.random(pick.size) < 0.03,
            "disputed": rng.random(pick.size) < 0.0005,
        }
    )
    rm = w.ring_members.sort("ring_id", "person").with_columns(
        nxt=pl.col("person").shift(-1).over("ring_id").fill_null(pl.col("person").first().over("ring_id"))
    )
    rsrc, rdst = np.repeat(rm["person"].to_numpy(), 30), np.repeat(rm["nxt"].to_numpy(), 30)
    cycles = pl.DataFrame(
        {
            "src": rsrc,
            "dst": rdst,
            "amount": np.round(np.exp(rng.normal(15.5, 0.3, rsrc.size)), -3),
            "ts": _between(rng, np.maximum(pj[rsrc], pj[rdst]), end),
            "cross_border": np.zeros(rsrc.size, bool),
            "disputed": rng.random(rsrc.size) < 0.25,  # victims of ring transfers dispute them
        }
    )
    t["p2p"] = pl.concat([p2p, cycles]).with_row_index("txn")

    payers = np.flatnonzero(pay)
    act = rng.gamma(0.8, 1, payers.size)
    payer = rng.choice(payers, MERCHANT_PAY_PER_PARTY * n, p=act / act.sum())
    pop = w.merchants["popularity"].to_numpy()
    organic = pl.DataFrame(
        {
            "payer": payer,
            "merchant": rng.choice(pop.size, payer.size, p=pop / pop.sum()),
            "amount": np.round(np.exp(rng.normal(11.8, 0.9, payer.size)), -3),
            "ts": _between(rng, pj[payer], end),
            "voucher": rng.random(payer.size) < np.where(ring[payer], 0.45, 0.04),
        }
    )
    # quasi-cash merchants: a handful of payers push large round amounts through them again and again
    quasi = rng.choice(np.flatnonzero(w.merchants["category"].to_numpy() != "cinema"), max(pop.size // 40, 1), replace=False)
    q_merchant = np.repeat(quasi, QUASI_PAYERS * QUASI_REPEATS)
    q_payer = np.repeat(rng.choice(payers, (quasi.size, QUASI_PAYERS)).ravel(), QUASI_REPEATS)
    cash_out = pl.DataFrame(
        {
            "payer": q_payer,
            "merchant": q_merchant,
            "amount": rng.integers(5, 21, q_payer.size) * 1_000_000.0,
            "ts": _between(rng, pj[q_payer], end),
            "voucher": np.zeros(q_payer.size, bool),
        }
    )
    t["merchant_pay"] = pl.concat([organic, cash_out]).with_row_index("txn")
    t["quasi_merchants"] = pl.DataFrame({"merchant": np.sort(quasi)})

    m = w.merchants.height
    bsrc = rng.integers(0, m, B2B_PER_PARTY * n)
    t["b2b"] = pl.DataFrame(
        {
            "src": bsrc,
            "dst": (bsrc + 1 + rng.integers(0, 3, bsrc.size)) % m,  # each merchant has 3 suppliers
            "amount": np.round(np.exp(rng.normal(15, 0.8, bsrc.size)), -3),
            "ts": _between(rng, np.full(bsrc.size, cfg.month_start(0)), end),
        }
    ).with_row_index("txn")

    nb = _random_neighbor(rng, w)
    ok = pay & (nb >= 0) & (rng.random(n) < 0.3)
    ok &= pay[np.maximum(nb, 0)] & (pj[np.maximum(nb, 0)] < pj)
    t["referrals"] = pl.DataFrame({"person": np.flatnonzero(ok), "referrer": nb[ok]})

    # --- credit
    t["loans"] = _loans(rng, w, member["credit"], joined["credit"])

    # --- insurance
    who = np.flatnonzero(member["insurance"])
    holder = np.repeat(who, rng.integers(1, 3, who.size))
    start = _between(rng, joined["insurance"][holder], end - MONTH_S)
    claimed = rng.random(holder.size) < np.where(ring[holder], 0.7, 0.12)
    t["policies"] = pl.DataFrame(
        {
            "person": holder,
            "product": rng.choice(["life", "health", "motor", "travel"], holder.size),
            "premium": np.round(np.exp(rng.normal(14.5, 0.5, holder.size)), -4),
            "start_ts": start,
            "claimed": claimed,
            "claim_amount": np.round(np.exp(rng.normal(16, 0.8, holder.size)) * np.where(ring[holder], 3, 1), -4),
            "claim_ts": _between(rng, start, end),
        }
    ).with_row_index("policy")

    # --- savings
    who = np.flatnonzero(member["savings"])
    mover = np.repeat(who, rng.poisson(6, who.size))
    t["savings_moves"] = pl.DataFrame(
        {
            "person": mover,
            "kind": rng.choice(["D", "W"], mover.size, p=[0.65, 0.35]),
            "amount": np.round(np.exp(rng.normal(15, 1, mover.size)), -4),
            "ts": _between(rng, joined["savings"][mover], end),
        }
    ).with_row_index("mov")

    # --- ota: bookings, 30% copied by a social neighbor (co-booking group)
    who = np.flatnonzero(member["ota"])
    traveller = np.repeat(who, rng.poisson(2, who.size))
    trip = rng.integers(0, N_TRIPS, traveller.size)
    ts = _between(rng, joined["ota"][traveller], end)
    co = (rng.random(traveller.size) < 0.3) & (nb[traveller] >= 0)
    co &= member["ota"][np.maximum(nb[traveller], 0)]
    traveller, trip = np.r_[traveller, nb[traveller][co]], np.r_[trip, trip[co]]
    ts = np.r_[ts, ts[co] + rng.integers(60, 3600, co.sum())]
    t["bookings"] = pl.DataFrame(
        {
            "person": traveller,
            "trip": trip,
            "price": np.round(np.exp(rng.normal(15, 0.6, traveller.size)), -4),
            "ts": ts,
            "promo": rng.random(traveller.size) < np.where(ring[traveller], 0.5, 0.08),
        }
    ).with_row_index("booking")

    # --- movies: genre preference follows the community
    who = np.flatnonzero(member["movies"])
    fan = np.repeat(who, rng.poisson(6, who.size))
    community = p["community"].to_numpy()
    liked = rng.integers(0, len(GENRES), community.max() + 1)[community[fan]]
    genre = np.where(rng.random(fan.size) < 0.6, liked, rng.integers(0, len(GENRES), fan.size))
    organic_tickets = pl.DataFrame(
        {
            "person": fan,
            "movie": genre * MOVIES_PER_GENRE + rng.integers(0, MOVIES_PER_GENRE, fan.size),
            "seats": rng.integers(1, 5, fan.size),
            "ts": _between(rng, joined["movies"][fan], end),
        }
    )
    # scalpers: a few members buy the same film dozens of times within two hours
    scalpers = rng.choice(who, max(who.size // 500, 1), replace=False)
    burst = np.repeat(scalpers, 3)
    burst_ts = _between(rng, joined["movies"][burst], end - 7200)
    scalped = pl.DataFrame(
        {
            "person": np.repeat(burst, 15),
            "movie": np.repeat(rng.integers(0, N_MOVIES, burst.size), 15),
            "seats": np.full(burst.size * 15, 4),
            "ts": np.repeat(burst_ts, 15) + rng.integers(0, 7200, burst.size * 15),
        }
    )
    t["tickets"] = pl.concat([organic_tickets, scalped]).with_row_index("ticket")
    t["scalpers"] = pl.DataFrame({"person": np.sort(scalpers)})

    # --- bnpl (the unseen extra source)
    who = np.flatnonzero(member["bnpl"])
    shopper = np.repeat(who, rng.poisson(3, who.size))
    t["bnpl_orders"] = pl.DataFrame(
        {
            "person": shopper,
            "merchant": rng.choice(pop.size, shopper.size, p=pop / pop.sum()),
            "amount": np.round(np.exp(rng.normal(13.5, 0.7, shopper.size)), -3),
            "ts": _between(rng, joined["bnpl"][shopper], end),
        }
    ).with_row_index("order")

    # --- outcomes other branches learn from
    # churn clusters by community, so a customer's peers leaving is informative
    sav = np.flatnonzero(member["savings"])
    leave = rng.random(sav.size) < _sigmoid(-2.2 + 1.5 * rng.normal(0, 1, community.max() + 1)[community[sav]])
    t["closures"] = pl.DataFrame(
        {"person": sav[leave], "ts": _between(rng, joined["savings"][sav[leave]] + 6 * MONTH_S, end)}
    ).with_row_index("closure")

    # reported fraud: ring members are mules; takeovers hit users of a second SIM more often
    mule = np.flatnonzero(ring & (rng.random(n) < 0.8))
    swap = p["phone_alt"].to_numpy() != p["phone"].to_numpy()
    taken = np.flatnonzero(pay & ~ring & (rng.random(n) < np.where(swap, 0.03, 0.004)))
    who = np.r_[mule, taken]
    t["fraud_cases"] = pl.DataFrame(
        {
            "person": who,
            "case_type": np.r_[np.full(mule.size, "mule"), np.full(taken.size, "takeover")],
            "ts": _between(rng, np.full(who.size, cfg.month_start(18)), end),
        }
    ).with_row_index("case")

    # chargebacks pile up on quasi-cash merchants
    cb = rng.poisson(np.where(np.isin(np.arange(pop.size), quasi), 30.0, 0.3))
    cb_merchant = np.repeat(np.arange(pop.size), cb)
    t["chargebacks"] = pl.DataFrame(
        {
            "merchant": cb_merchant,
            "amount": np.round(np.exp(rng.normal(13, 0.8, cb_merchant.size)), -3),
            "ts": _between(rng, np.full(cb_merchant.size, cfg.month_start(6)), end),
        }
    ).with_row_index("cb")

    # --- cross-border corridors, with one corridor that suddenly takes off in the last three months
    p2p = t["p2p"].drop("txn")
    country = pl.Series("corridor", rng.choice(CORRIDORS, p2p.height, p=CORRIDOR_SHARE))
    burst_src = np.repeat(rng.choice(payers, 40, replace=False), 25)
    burst = pl.DataFrame(
        {
            "src": burst_src,
            "dst": rng.choice(payers, burst_src.size),
            "amount": np.round(np.exp(rng.normal(15, 0.4, burst_src.size)), -3),
            "ts": _between(rng, np.full(burst_src.size, end - 3 * MONTH_S), end),
            "cross_border": np.ones(burst_src.size, bool),
            "disputed": np.zeros(burst_src.size, bool),
            "corridor": np.full(burst_src.size, ANOMALOUS_CORRIDOR),
        }
    )
    t["p2p"] = pl.concat(
        [p2p.with_columns(pl.when(pl.col("cross_border")).then(country).otherwise(None).alias("corridor")), burst]
    ).with_row_index("txn")

    # --- merchant working-capital loans; quasi-cash merchants default far more often
    borrowers = np.flatnonzero(rng.random(pop.size) < 0.6)
    m_loan = np.repeat(borrowers, rng.integers(1, 3, borrowers.size))
    risky = np.isin(m_loan, quasi)
    t["merchant_loans"] = pl.DataFrame(
        {
            "merchant": m_loan,
            "ts": _between(rng, np.full(m_loan.size, cfg.month_start(6)), int(cfg.month_start(cfg.months - 14))),
            "amount": np.round(np.exp(rng.normal(18, 0.6, m_loan.size)), -5),
            "term": rng.choice([6, 12], m_loan.size),
            "default": rng.random(m_loan.size) < np.where(risky, 0.7, 0.07),
            "default_offset": rng.integers(3, 9, m_loan.size),
        }
    ).with_row_index("loan")

    return Facts(member, joined, t)

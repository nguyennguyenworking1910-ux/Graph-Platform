"""Project the hidden world into messy per-domain source systems, each with its own schema."""

from __future__ import annotations

import re
from collections.abc import Callable

import numpy as np
import polars as pl

from graph_platform.simulator.behaviors import MONTH_S, Facts
from graph_platform.simulator.catalog import (
    DESTINATIONS,
    GENRES,
    INSURANCE_PRODUCTS,
    MOVIE_TITLES,
    MOVIES_PER_GENRE,
    SAVINGS_PRODUCTS,
    TRIP_KINDS,
)
from graph_platform.simulator.world import World

Tables = dict[str, pl.DataFrame]
C = pl.col


def _sid(prefix: str, col: str, width: int = 8) -> pl.Expr:
    return pl.format(f"{prefix}{{}}", C(col).cast(pl.String).str.zfill(width))


def _dev(col: str) -> pl.Expr:
    return _sid("dev-", col, 9)


def _dt(col: str, fmt: str) -> pl.Expr:
    return pl.from_epoch(C(col), "s").dt.strftime(fmt)


def _ms(col: str) -> pl.Expr:
    return C(col) * 1000


def _members(w: World, f: Facts, domain: str, rng: np.random.Generator, *, shared_phone: float = 0.0,
             email_missing: float = 0.2) -> tuple[pl.DataFrame, np.ndarray]:  # fmt: skip
    """Members of a domain with source-local ids and noisy identifiers. Returns (frame, person->sid)."""
    idx = np.flatnonzero(f.member[domain])
    k, p = idx.size, w.persons
    phone = np.where(rng.random(k) < 0.5, p["phone_alt"].to_numpy()[idx], p["phone"].to_numpy()[idx])
    if shared_phone:
        hh = p["household"].to_numpy()
        _, first, inv = np.unique(hh, return_index=True, return_inverse=True)
        head_phone = p["phone"].to_numpy()[first][inv][idx]
        phone = np.where(rng.random(k) < shared_phone, head_phone, phone)
    sid = rng.permutation(10 * k)[:k] + 1
    lut = np.full(w.n, -1)
    lut[idx] = sid

    ph = C("_phone").cast(pl.String)
    df = p[idx].with_columns(
        sid=sid, joined=f.joined[domain][idx], _phone=phone,
        _r1=rng.random(k), _r2=rng.random(k), _r3=rng.random(k),
    ).with_columns(  # fmt: skip
        name_n=pl.when(C("_r1") < 0.10).then(C("full_name").str.to_uppercase())
        .when(C("_r1") < 0.20).then(C("full_name").str.to_lowercase())
        .when(C("_r1") < 0.30).then(C("full_name").str.replace(r"^(\S+) \S+ (\S+)$", "$1 $2"))
        .when(C("_r1") < 0.35).then(pl.format(" {}  ", C("full_name")))
        .otherwise(C("full_name")),
        phone_n=pl.when(C("_r2") < 0.4).then(pl.format("0{}", ph))
        .when(C("_r2") < 0.6).then(pl.format("+84{}", ph))
        .when(C("_r2") < 0.75).then(pl.format("84{}", ph))
        .when(C("_r2") < 0.9).then(pl.format("0{} {} {}", ph.str.slice(0, 2), ph.str.slice(2, 3), ph.str.slice(5)))
        .otherwise(pl.format("(+84) {}.{}", ph.str.slice(0, 3), ph.str.slice(3))),
        email_n=pl.when(C("_r3") < email_missing).then(None)
        .when(C("_r3") < email_missing + 0.1).then(C("email").str.to_uppercase())
        .otherwise(C("email")),
        nid=C("national_id").cast(pl.String),
    )  # fmt: skip
    return df, lut


def _truth(df: pl.DataFrame, pk: pl.Expr) -> pl.DataFrame:
    return df.select("person", source_pk=pk)


def credit(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "credit", rng, email_missing=0.3)
    cust = _sid("C", "sid")
    customers = mem.select(
        cust_id=cust, full_nm=C("name_n"), dob=C("birth_date").dt.strftime("%d/%m/%Y"), sex=C("gender"),
        id_card=C("nid"), mobile=C("phone_n"), email_addr=C("email_n"), monthly_income=C("income"),
        job=C("occupation"), city=C("city"), created_dt=_dt("joined", "%d/%m/%Y"),
    )  # fmt: skip
    customers = pl.concat([customers, customers.sample(fraction=0.01, seed=1)])  # duplicate rows

    p = w.persons
    loans = f.tables["loans"].with_columns(
        sid=pl.Series(lut[f.tables["loans"]["person"].to_numpy()]),
        dev=pl.when(C("use_shared_device"))
        .then(pl.Series(p["device_b"].to_numpy()[f.tables["loans"]["person"].to_numpy()]))
        .otherwise(pl.Series(p["device_a"].to_numpy()[f.tables["loans"]["person"].to_numpy()])),
    )
    applications = loans.select(
        app_id=_sid("A", "loan"), cust_id=cust, product_code=pl.format("CL{}", C("term")),
        product_name=pl.format("Vay tiêu dùng {} tháng", C("term")),
        req_amount=C("amount"), term=C("term"), app_date=_dt("app_ts", "%d/%m/%Y %H:%M:%S"),
        decision=pl.when(C("approved")).then(pl.lit("APPROVED")).otherwise(pl.lit("REJECTED")),
        device_fp=_dev("dev"),
    )  # fmt: skip
    booked = loans.filter(C("approved"))
    loan_tbl = booked.select(
        loan_id=_sid("L", "loan"), app_id=_sid("A", "loan"), cust_id=cust, principal=C("amount"),
        disb_date=_dt("app_ts", "%d/%m/%Y"), term_months=C("term"),
    )  # fmt: skip

    # monthly performance snapshots, up to 12 periods, only those already due
    rep = (
        booked.select("loan", "app_ts", "default", "default_offset", "term")
        .join(pl.DataFrame({"period_no": np.arange(1, 13)}), how="cross")
        .filter(C("period_no") <= C("term"))
        .with_columns(due_ts=C("app_ts") + C("period_no") * MONTH_S)
        .filter(C("due_ts") < w.cfg.end_ts)
    )
    rep = rep.with_columns(
        _late=pl.Series(rng.random(rep.height) < 0.05),
        _d=pl.Series(rng.integers(1, 30, rep.height)),
    )
    late = C("_late") & ~C("default")
    ramp = ((C("period_no") - (C("default_offset") - 3)) * 30).clip(0, 180)
    repayments = rep.with_columns(
        dpd=pl.when(C("default")).then(ramp).when(late).then(C("_d")).otherwise(0)
    ).select(
        loan_id=_sid("L", "loan"), period_no=C("period_no"), due_date=_dt("due_ts", "%d/%m/%Y"),
        dpd=C("dpd"), snapshot_dt=_dt("due_ts", "%d/%m/%Y"),
    )  # fmt: skip
    tables = {
        "customers": customers,
        "loan_applications": applications,
        "loans": loan_tbl,
        "repayments": repayments,
    }
    return tables, _truth(mem, cust)


def payment(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "payment", rng, email_missing=0.4)
    uid = _sid("U", "sid", 9)
    ref = f.tables["referrals"].with_columns(ref_sid=pl.Series(lut[f.tables["referrals"]["referrer"].to_numpy()]))
    users = mem.join(ref.select("person", "ref_sid"), on="person", how="left").select(
        user_id=uid, phone_number=C("phone_n"), name=C("name_n"), email=C("email_n"),
        kyc_id=pl.when(C("_r1") < 0.6).then(C("nid")), signup_ts=_ms("joined"),
        referrer_user_id=pl.when(C("ref_sid").is_not_null()).then(_sid("U", "ref_sid", 9)),
        home_city=C("city"),
    )  # fmt: skip
    devices = pl.concat([mem.select(user_id=uid, device_id=_dev(d), first_seen_ts=_ms("joined")) for d in ("device_a", "device_b")]).unique(
        maintain_order=True
    )

    p2p = f.tables["p2p"]
    k = p2p.height
    transfers = p2p.with_columns(
        s=pl.Series(lut[p2p["src"].to_numpy()]), d=pl.Series(lut[p2p["dst"].to_numpy()]),
        _c=pl.Series(rng.integers(0, 3, k)), _ok=pl.Series(rng.random(k) < 0.97),
    ).select(
        txn_id=_sid("T", "txn", 10), from_user=_sid("U", "s", 9), to_user=_sid("U", "d", 9),
        amount_vnd=C("amount"), ts=_ms("ts"),
        channel=C("_c").replace_strict({0: "app", 1: "qr", 2: "web"}, return_dtype=pl.String),
        status=pl.when(C("disputed")).then(pl.lit("DISPUTED")).when(C("_ok")).then(pl.lit("SUCCESS")).otherwise(pl.lit("FAILED")),
        is_cross_border=C("cross_border"),
        dest_country=C("corridor"),
        ccy=pl.when(C("cross_border")).then(pl.lit("USD")).otherwise(pl.lit("VND")),
    )  # fmt: skip
    mp = f.tables["merchant_pay"]
    merchant_payments = mp.with_columns(s=pl.Series(lut[mp["payer"].to_numpy()])).select(
        txn_id=_sid("P", "txn", 10), payer_user=_sid("U", "s", 9), merchant_code=_sid("M", "merchant", 6),
        amount=C("amount"), ts=_ms("ts"), status=pl.lit("SUCCESS"),
        voucher=pl.when(C("voucher")).then(pl.format("PROMO{}", C("merchant") % 20)),
    )  # fmt: skip
    b2b = f.tables["b2b"].select(
        txn_id=_sid("B", "txn", 10), from_merchant=_sid("M", "src", 6), to_merchant=_sid("M", "dst", 6),
        amount=C("amount"), ts=_ms("ts"),
    )  # fmt: skip
    fc = f.tables["fraud_cases"]
    fraud_cases = fc.with_columns(s=pl.Series(lut[fc["person"].to_numpy()])).select(
        case_id=_sid("FC", "case"), user_id=_sid("U", "s", 9), case_type=C("case_type"), reported_ts=_ms("ts")
    )
    tables = {
        "fraud_cases": fraud_cases,
        "users": users, "devices": devices, "transfers": transfers,
        "merchant_payments": merchant_payments, "b2b_settlements": b2b,
    }  # fmt: skip
    return tables, _truth(mem, uid)


def merchant(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    owners = w.persons.select(owner=C("person"), owner_phone=pl.format("0{}", C("phone")), nid=C("national_id"))
    m = w.merchants.join(owners, on="owner").sort("merchant")
    onboard = rng.integers(w.cfg.month_start(0), w.cfg.month_start(6), m.height)
    merchants = m.with_columns(_t=pl.Series(onboard)).select(
        merchant_code=_sid("M", "merchant", 6), biz_name=C("biz_name"), mcc_category=C("category"),
        owner_phone=C("owner_phone"), owner_national_id=C("nid").cast(pl.String),
        tax_id=C("tax_id").cast(pl.String), city=C("city"), onboard_date=_dt("_t", "%Y-%m-%d"),
    )  # fmt: skip
    chargebacks = f.tables["chargebacks"].select(
        cb_id=_sid("CB", "cb"), merchant_code=_sid("M", "merchant", 6), amount=C("amount"), cb_date=_dt("ts", "%Y-%m-%d")
    )
    ml = f.tables["merchant_loans"]
    merchant_loans = ml.select(
        loan_id=_sid("ML", "loan"), merchant_code=_sid("M", "merchant", 6), principal=C("amount"),
        disb_date=_dt("ts", "%Y-%m-%d"), term_months=C("term"),
    )  # fmt: skip
    rep = (
        ml.join(pl.DataFrame({"period_no": np.arange(1, 13)}), how="cross")
        .filter(C("period_no") <= C("term"))
        .with_columns(due_ts=C("ts") + C("period_no") * MONTH_S)
        .filter(C("due_ts") < w.cfg.end_ts)
    )
    merchant_repayments = rep.select(
        loan_id=_sid("ML", "loan"), period_no=C("period_no"), snapshot_date=_dt("due_ts", "%Y-%m-%d"),
        dpd=pl.when(C("default")).then(((C("period_no") - (C("default_offset") - 3)) * 30).clip(0, 180)).otherwise(0),
    )  # fmt: skip
    tables = {
        "merchants": merchants,
        "chargebacks": chargebacks,
        "merchant_loans": merchant_loans,
        "merchant_repayments": merchant_repayments,
    }
    return tables, pl.DataFrame(schema={"person": pl.Int64, "source_pk": pl.String})


def insurance(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "insurance", rng)
    holder = _sid("H", "sid")
    holders = mem.select(
        holder_no=holder, name=C("name_n"), birthdate=C("birth_date").dt.strftime("%Y-%m-%d"),
        phone=C("phone_n"), email=C("email_n"), nid=C("nid"),
    )  # fmt: skip
    pol = f.tables["policies"].with_columns(sid=pl.Series(lut[f.tables["policies"]["person"].to_numpy()]))
    policies = pol.select(
        policy_no=_sid("PL", "policy"), holder_no=holder, product=C("product"),
        product_name=C("product").replace_strict(INSURANCE_PRODUCTS), premium=C("premium"),
        start_date=_dt("start_ts", "%Y-%m-%d"),
    )  # fmt: skip
    claims = pol.filter(C("claimed")).select(
        claim_no=_sid("CM", "policy"), policy_no=_sid("PL", "policy"), holder_no=holder, claim_amount=C("claim_amount"),
        claim_date=_dt("claim_ts", "%Y-%m-%d"), status=pl.lit("PAID"),
    )  # fmt: skip
    return {"policy_holders": holders, "policies": policies, "claims": claims}, _truth(mem, holder)


def savings(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "savings", rng)
    clients = mem.select(
        client_id=C("sid"), fullname=C("name_n"), mobile_no=C("phone_n"), email=C("email_n"),
        national_id=C("nid"), opened_at=pl.from_epoch(C("joined"), "s"),
    )  # fmt: skip
    accounts = mem.select(
        acct_no=_sid("SA", "sid"), client_id=C("sid"),
        product=pl.Series(rng.choice(list(SAVINGS_PRODUCTS), mem.height)),
        opened_at=pl.from_epoch(C("joined"), "s"),
    ).with_columns(product_name=C("product").replace_strict(SAVINGS_PRODUCTS))  # fmt: skip
    mv = f.tables["savings_moves"]
    movements = mv.with_columns(sid=pl.Series(lut[mv["person"].to_numpy()])).select(
        mov_id=C("mov"), acct_no=_sid("SA", "sid"), kind=C("kind"), amt=C("amount"),
        ts=pl.from_epoch(C("ts"), "s"),
    )  # fmt: skip
    cl = f.tables["closures"]
    closures = cl.with_columns(sid=pl.Series(lut[cl["person"].to_numpy()])).select(
        closure_id=C("closure"), client_id=C("sid"), closed_at=pl.from_epoch(C("ts"), "s"), reason=pl.lit("customer_request")
    )
    truth = _truth(mem, C("sid").cast(pl.String))
    return {"clients": clients, "accounts": accounts, "movements": movements, "account_closures": closures}, truth


def ota(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "ota", rng, shared_phone=0.04, email_missing=0.1)
    tid = _sid("TR", "sid")
    travellers = mem.select(
        traveller_id=tid, contact_name=C("name_n"), contact_phone=C("phone_n"),
        contact_email=C("email_n"), registered=_dt("joined", "%Y-%m-%dT%H:%M:%S"),
    )  # fmt: skip
    b = f.tables["bookings"]
    place = (C("trip") % len(DESTINATIONS)).replace_strict(dict(enumerate(DESTINATIONS)), return_dtype=pl.String)
    kind = (C("trip") // len(DESTINATIONS)).replace_strict(dict(enumerate(TRIP_KINDS)), return_dtype=pl.String)
    person = b["person"].to_numpy()
    bookings = b.with_columns(
        sid=pl.Series(lut[person]), dev=pl.Series(w.persons["device_b"].to_numpy()[person])
    ).select(
        booking_ref=_sid("BK", "booking"), traveller_id=tid, trip_code=_sid("TRIP", "trip", 4),
        destination=place, trip_name=pl.format("{} {}", place, kind), total_price=C("price"),
        booked_at=_dt("ts", "%Y-%m-%dT%H:%M:%S"),
        promo_code=pl.when(C("promo")).then(pl.format("SALE{}", C("trip") % 10)), device=_dev("dev"),
    )  # fmt: skip
    return {"travellers": travellers, "bookings": bookings}, _truth(mem, tid)


def movies(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "movies", rng, shared_phone=0.04, email_missing=0.3)
    mid = _sid("MB", "sid")
    members = mem.select(
        member_id=mid, nickname=C("name_n").str.to_lowercase().str.replace_all(r"\s+", ""),
        phone=C("phone_n"), email=C("email_n"), joined=_dt("joined", "%Y/%m/%d"),
    )  # fmt: skip
    t = f.tables["tickets"]
    person = t["person"].to_numpy()
    cinemas = w.merchants.filter(C("category") == "cinema")
    site = rng.integers(0, cinemas.height, t.height)
    tickets = t.with_columns(
        sid=pl.Series(lut[person]), dev=pl.Series(w.persons["device_a"].to_numpy()[person]),
        merchant=pl.Series(cinemas["merchant"].to_numpy()[site]), cinema=pl.Series(cinemas["biz_name"].to_numpy()[site]),
    ).select(
        ticket_id=C("ticket"), member_id=mid, movie_code=_sid("MV", "movie", 4),
        movie_title=C("movie").replace_strict(dict(enumerate(MOVIE_TITLES)), return_dtype=pl.String),
        genre=(C("movie") // MOVIES_PER_GENRE).replace_strict(dict(enumerate(GENRES)), return_dtype=pl.String),
        cinema_code=_sid("M", "merchant", 6), cinema_name=C("cinema"),
        seats=C("seats"), price=C("seats") * 90_000, purchased_at=_ms("ts"), device_id=_dev("dev"),
    )  # fmt: skip
    return {"members": members, "tickets": tickets}, _truth(mem, mid)


def bnpl(w: World, f: Facts, rng: np.random.Generator) -> tuple[Tables, pl.DataFrame]:
    mem, lut = _members(w, f, "bnpl", rng, email_missing=0.15)
    sid = _sid("SH", "sid")
    shoppers = mem.select(
        shopper_ref=sid, display_name=C("name_n"), msisdn=C("phone_n"), mail=C("email_n"),
        citizen_no=pl.when(C("_r1") < 0.5).then(C("nid")), enrolled_on=_dt("joined", "%Y-%m-%d"),
    )  # fmt: skip
    o = f.tables["bnpl_orders"]
    orders = o.with_columns(sid=pl.Series(lut[o["person"].to_numpy()])).select(
        order_ref=_sid("OR", "order"), shopper_ref=sid, store_code=_sid("M", "merchant", 6),
        basket_value=C("amount"), instalments=pl.Series(rng.choice([3, 6, 12], o.height)),
        ordered_at=_dt("ts", "%Y-%m-%d %H:%M:%S"),
    )  # fmt: skip
    return {"shoppers": shoppers, "orders": orders}, _truth(mem, sid)


Emitter = Callable[[World, Facts, np.random.Generator], tuple[Tables, pl.DataFrame]]
EMITTERS: dict[str, tuple[Emitter, str]] = {
    "credit": (credit, "csv"),
    "payment": (payment, "parquet"),
    "merchant": (merchant, "jsonl"),
    "insurance": (insurance, "csv"),
    "savings": (savings, "parquet"),
    "ota": (ota, "jsonl"),
    "movies": (movies, "csv"),
    "bnpl": (bnpl, "csv"),
}


def camel_dialect(tables: Tables) -> Tables:
    """A second schema dialect: `tbl_` tables and camelCase columns."""

    def camel(name: str) -> str:
        return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), name)

    return {f"tbl_{t}": df.rename({c: camel(c) for c in df.columns}) for t, df in tables.items()}

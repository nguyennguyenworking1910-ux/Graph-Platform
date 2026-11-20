"""Web interface. Business users first: what the platform answers for their unit. Operations pages come after."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import polars as pl
import streamlit as st
from neo4j.exceptions import ServiceUnavailable

from graph_platform import auth, branches
from graph_platform.config import Settings
from graph_platform.contracts import BranchContract, DataDictionary, DomainContract, dump_yaml, load_yaml
from graph_platform.control import proposals
from graph_platform.control.audit import Audit
from graph_platform.control.supervisor import decide
from graph_platform.graph import GraphStore, graph_dir
from graph_platform.trunk import extract, pipeline
from graph_platform.ui import view
from graph_platform.ui.names import CITY, OCCUPATION, party_name, plain
from graph_platform.ui.network import ADVERSE, group_network, party_network, recommendation_network

st.set_page_config(page_title="Graph Platform", layout="wide")
st.markdown(
    "<style>[data-testid=stSkillsNudgeAnchor]{display:none} .block-container{max-width:1080px;padding-top:2.5rem} h1{font-weight:650;letter-spacing:-0.01em} "
    "h3{margin-top:1.6rem} [data-testid='stMetricValue']{font-variant-numeric:tabular-nums}</style>",
    unsafe_allow_html=True,
)
S = Settings.load()
INTRO, QUESTIONS, LOOKUP, DATA, SETUP, APPROVE = (
    "Giới thiệu",
    "Câu hỏi nghiệp vụ",
    "Tra cứu khách hàng",
    "Dữ liệu",
    "Thiết lập bài toán",
    "Phê duyệt",
)
UNIT = {
    "credit": "Tín dụng", "insurance": "Bảo hiểm", "savings": "Tiết kiệm và Sinh lời", "payment": "Thanh toán",
    "merchant": "Merchant", "ota": "Du lịch", "movies": "Vé xem phim",
}  # fmt: skip
COLUMN = {
    "rank": "Thứ hạng", "party_id": "Khách hàng", "score": "Điểm", "member_cnt": "Số thành viên",
    "internal_txn_cnt": "Giao dịch trong nhóm", "internal_amt_sum": "Tổng tiền trong nhóm", "corridor_cnt": "Số tuyến chuyển tiền",
    "item_id": "Gợi ý", "as_of": "Tính đến ngày", "predicted": "Dự đoán chi tiêu", "txn_cnt": "Số giao dịch", "payer_nuniq": "Số người trả",
    "amt_avg": "Giá trị trung bình", "amt_sum": "Tổng giá trị", "round_amt_ratio": "Tỷ lệ số tiền tròn", "top_payer_ratio": "Tỷ trọng 5 người trả lớn nhất",
    "repeat_ratio": "Tỷ lệ lặp lại", "inflow_sum": "Tổng nhận về", "outflow_sum": "Tổng chuyển đi", "source_cnt": "Số nguồn", "src_id": "Người chuyển",
    "dst_id": "Người nhận", "event_time": "Thời điểm", "amount": "Số tiền", "burst_purchase_max": "Số lần mua nhiều nhất trong 2 giờ",
    "burst_quantity_max": "Số vé nhiều nhất trong 2 giờ", "burst_cnt": "Số đợt mua dồn", "domain_cnt": "Số đơn vị đang dùng", "loan_cnt": "Khoản vay",
    "policy_cnt": "Hợp đồng bảo hiểm", "savings_account_cnt": "Tài khoản tiết kiệm", "trip_cnt": "Chuyến đi", "movie_cnt": "Vé phim",
    "txn_out_cnt": "Giao dịch chuyển đi", "txn_in_cnt": "Giao dịch nhận về", "peer_cnt": "Người có quan hệ", "city": "Thành phố",
    "last_activity_time": "Hoạt động gần nhất", "first_seen_time": "Xuất hiện từ", "occupation": "Nghề nghiệp", "income": "Thu nhập",
    "age": "Tuổi", "phone_nuniq": "Số điện thoại đã dùng", "email_nuniq": "Email đã dùng", "device_nuniq": "Thiết bị đã dùng",
    "loan_amt_sum": "Tổng tiền đã vay", "application_cnt": "Hồ sơ vay", "purchase_amt_sum": "Tổng tiền mua hàng", "amt_out_sum": "Tổng chuyển đi",
    "amt_in_sum": "Tổng nhận về", "action": "Đề xuất", "risk_score": "Mức rủi ro", "predicted_value": "Chi tiêu dự đoán", "corridor": "Tuyến",
    "recent_txn_cnt": "Giao dịch 90 ngày gần đây", "usual_txn_cnt": "Mức thường thấy mỗi 90 ngày", "sender_nuniq": "Số người gửi",
    "recent_amt_sum": "Tổng tiền 90 ngày gần đây",
}  # fmt: skip
# what a problem predicts -> the kind of past event worth pointing at among the people around a case
OUTCOME = {
    "loan_performance": "late", "merchant_loan_performance": "late", "churn": "churn", "fraud_case": "fraud",
    "chargeback": "chargeback", "claim": "claim",
}  # fmt: skip
GONE = "Không còn trong dữ liệu"
DRAW = view.network()

if "user" not in st.session_state:
    st.title("Graph Platform")
    st.write("Nơi trả lời các câu hỏi về khách hàng, merchant và giao dịch dựa trên mối liên hệ giữa chúng.")
    if not auth.load_users(S.users_path):
        st.warning("Chưa có tài khoản nào. Hãy liên hệ quản trị viên để được cấp tài khoản.")
        st.stop()
    with st.form("login"):
        username = st.text_input("Tên đăng nhập")
        password = st.text_input("Mật khẩu", type="password")
        if st.form_submit_button("Đăng nhập"):
            found = auth.sign_in(S.users_path, username, password)
            if found:
                st.session_state["user"] = found
                st.rerun()
            st.error("Sai tên đăng nhập hoặc mật khẩu.")
    st.stop()

USER: auth.User = st.session_state["user"]
st.sidebar.write(f"**{USER.name}**")
tenant = st.sidebar.selectbox(
    "Doanh nghiệp", [t for t in S.tenants() if t in USER.tenants], format_func=lambda t: t.replace("_", " ").title()
)
page = st.sidebar.radio("Trang", auth.PAGES[USER.role], key="page")
if st.sidebar.button("Đăng xuất"):
    del st.session_state["user"]
    st.rerun()
T = S.tenant(tenant)
AUDIT = Audit(S, tenant)


def domains() -> list[DomainContract]:
    return [load_yaml(DomainContract, p) for p in sorted(T.domains.glob("*.yaml"))]


def manifest(domain: str, source: str) -> dict | None:
    path = T.zone("meta") / domain / source / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else None


def experiments() -> list[BranchContract]:
    return [load_yaml(BranchContract, p) for p in sorted(T.branches.glob("*.yaml"))]


def runs(name: str) -> list[Path]:
    base = T.zone("branches") / name
    return sorted((p for p in base.iterdir() if p.is_dir()), reverse=True) if base.is_dir() else []


def waiting() -> list:
    return [p for p in proposals.load_all(S, tenant) if p.status == "needs_human"] if (T.zone("control") / "proposals").is_dir() else []


def updated(name: str) -> str | None:
    """Date of the latest result, as dd/mm/yyyy."""
    r = runs(name)
    return f"{r[0].name[6:8]}/{r[0].name[4:6]}/{r[0].name[:4]}" if r else None


def my_units() -> list[str]:
    owned = sorted({e.owner_domain for e in experiments()})
    return [u for u in owned if not USER.domains or u in USER.domains] or owned


def go(question: str) -> None:
    st.session_state["page"], st.session_state["question"] = QUESTIONS, question


@st.cache_data
def directory(tenant: str, version: str) -> pl.DataFrame:
    """Display name of every party and item of one graph version; `plain` is the name as typed without accents."""
    g = graph_dir(S, tenant)
    party = pl.read_parquet(g / "nodes/Party.parquet", columns=["node_id", "subtype", "trade_name"])
    party = party.select("node_id", party=pl.lit(True), name=pl.Series([party_name(*r) for r in party.iter_rows()], dtype=pl.String))
    item = pl.read_parquet(g / "nodes/Item.parquet", columns=["node_id", "title"]).select("node_id", party=pl.lit(False), name="title")
    return pl.concat([party, item]).with_columns(plain=pl.col("name").map_elements(plain, return_dtype=pl.String))


def names() -> pl.DataFrame:
    return directory(tenant, (graph_dir(S, tenant) / "VERSION").read_text(encoding="utf-8"))


def name_of(node_id: str) -> str:
    """Display name, safe to put in a heading or a sentence: names come from source data and are shown as plain text."""
    found = names().filter(pl.col("node_id") == node_id)["name"]
    return plain_text(found[0]) if found.len() else GONE


def plain_text(words: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~<])", r"\\\1", words)


def friendly(table: pl.DataFrame) -> pl.DataFrame:
    """Result table for business readers: names instead of ids, only columns that have a plain name, dd/mm/yyyy, rounded numbers."""
    scores = [c for c in table.columns if c.startswith("score_")]
    if scores:
        table = table.with_columns(score=pl.col(scores[-1]))
    for c in (c for c in ("party_id", "src_id", "dst_id", "item_id") if c in table.columns):
        known = names().select(pl.col("node_id").alias(c), _name="name")
        table = (
            table.join(known, on=c, how="left", maintain_order="left")
            .with_columns(pl.coalesce("_name", pl.lit(GONE)).alias(c))
            .drop("_name")
        )
    out = table.select([c for c in table.columns if c in COLUMN])
    out = out.with_columns(pl.col(pl.Float64, pl.Float32).round(3), pl.col(pl.Datetime, pl.Date).dt.strftime("%d/%m/%Y"))
    out = out.with_columns(pl.col(c).replace(words) for c, words in (("city", CITY), ("occupation", OCCUPATION)) if c in out.columns)
    return out.rename(COLUMN, strict=False)


def ranked(name: str) -> pl.DataFrame:
    """Latest result of a problem, most important rows first."""
    table = pl.read_parquet(runs(name)[0] / "output.parquet")
    order = "rank" if "rank" in table.columns else next((c for c in reversed(table.columns) if c.startswith("score_")), None)
    return table.sort(order, descending=order != "rank") if order else table


def graph_on() -> bool:
    """Pictures need the graph database: configured and answering."""
    if "GP_NEO4J_PASSWORD" not in os.environ:
        return False
    try:
        graph_store().driver.verify_connectivity()
    except ServiceUnavailable:
        return False
    return True


@st.cache_resource
def graph_store() -> GraphStore:
    return GraphStore(S)


def watch(e: BranchContract) -> frozenset[str]:
    event = (
        "loan_performance"
        if e.plugin in ("early_warning", "limit_review")
        else (e.label.outcome_event if e.label else e.params.get("outcome_event"))
    )
    return frozenset({OUTCOME[event]}) if event in OUTCOME else ADVERSE


def open_party(key: str) -> None:
    st.session_state["page"], st.session_state["party"], st.session_state["find"] = LOOKUP, st.session_state[key]["picked"], ""


def typical(profiles: pl.DataFrame) -> str:
    """A customer worth opening first: seen by the most units, with a readable number of connections."""
    return profiles.sort(pl.col("domain_cnt"), -(pl.col("peer_cnt") - 10).abs(), descending=True)["party_id"][0]


@st.cache_data
def picture(kind: str, tenant: str, version: str, subject: str | tuple[str, ...], as_of: date | None, extra: tuple[str, ...]) -> dict:
    """One picture per graph version and question; `extra` is what to flag, or for a suggestion the suggested items."""
    if kind == "taste":
        return recommendation_network(graph_store(), tenant, subject, list(extra), as_of)
    build = group_network if kind == "group" else party_network
    return build(graph_store(), tenant, list(subject) if kind == "group" else subject, as_of, frozenset(extra))


def drawn(kind: str, subject: str | list[str], as_of: date | None = None, extra: frozenset[str] | list[str] = ADVERSE) -> dict:
    version = (graph_dir(S, tenant) / "VERSION").read_text(encoding="utf-8")
    return picture(kind, tenant, version, tuple(subject) if kind == "group" else subject, as_of, tuple(sorted(extra)))


def show(picture: dict, key: str, height: int = 540, legend: bool = True) -> None:
    """Say what the picture shows, then draw it. Clicking a person or a shop opens them in the lookup page."""
    st.markdown("\n".join(f"- {plain_text(line)}" for line in picture["story"]))
    data = {k: picture[k] for k in ("nodes", "edges", "mode")} | {"height": height, "legend": legend}
    DRAW(data=data, key=key, on_picked_change=lambda: open_party(key))


if page == INTRO:
    st.title("Graph Platform")
    st.write(
        "Khách hàng, cửa hàng, thiết bị và giao dịch của công ty đều liên quan đến nhau. "
        "Platform nối các mối liên hệ đó lại từ dữ liệu của mọi đơn vị, "
        "để trả lời những câu hỏi mà nhìn từng bảng riêng lẻ không thấy được."
    )
    st.markdown(view.diagram([UNIT.get(d.domain, d.domain) for d in domains()]), unsafe_allow_html=True)
    exps = experiments()
    if graph_on() and runs("customer_360"):
        mine = [e for e in exps if e.owner_domain in my_units() and runs(e.name)]
        st.subheader("Nhìn thử trên dữ liệu đang có")
        profiles = pl.read_parquet(runs("customer_360")[0] / "output.parquet")
        r = profiles.filter(pl.col("party_id") == typical(profiles)).row(0, named=True)
        st.markdown(
            f"**Một khách hàng, nhiều đơn vị.** {name_of(r['party_id'])} đang dùng {', '.join(UNIT.get(d, d) for d in r['domains'])}. Mỗi đơn vị chỉ thấy phần của mình; nối lại mới thấy đủ."
        )
        show(drawn("party", r["party_id"], extra=frozenset()), "intro_customer", 400)
        ring = next((e for e in mine if e.plugin == "community_rings" and ranked(e.name).height), None)
        if ring:
            top = ranked(ring.name)
            members = top.filter(pl.col("rank") == top["rank"][0])["party_id"].to_list()
            st.markdown(f"**{ring.title}.** {ring.reading}")
            show(drawn("group", members, ring.as_of, watch(ring)), "intro_group", 400, legend=False)
            st.button("Xem cả danh sách", key=f"more_{ring.name}", on_click=go, args=(ring.name,))
        alert = max((e for e in mine if e.plugin == "early_warning"), key=lambda e: ranked(e.name).height, default=None)
        if alert and ranked(alert.name).height:
            case = ranked(alert.name).row(0, named=True)
            st.markdown(
                f"**{alert.title}.** {name_of(case['party_id'])} đang đứng đầu danh sách. Hình dưới cho thấy những người quanh họ đã có chuyện gì."
            )
            show(drawn("party", case["party_id"], case["as_of"], watch(alert)), "intro_case", 400, legend=False)
            st.button("Xem cả danh sách", key=f"more_{alert.name}", on_click=go, args=(alert.name,))
    st.subheader("Platform trả lời được gì cho đơn vị của bạn")
    for unit in my_units():
        st.markdown(f"**{UNIT.get(unit, unit)}**")
        for e in (x for x in exps if x.owner_domain == unit):
            left, mid, right = st.columns([6, 2, 1])
            left.write(e.question)
            when = updated(e.name)
            mid.caption(f"Cập nhật {when}" if when else "Chưa có kết quả")
            right.button("Xem", key=f"go_{e.name}", on_click=go, args=(e.name,), disabled=when is None)
    st.subheader("Cách dùng")
    st.markdown(
        "1. Chọn một câu hỏi ở trên.\n"
        "2. Đọc danh sách kết quả, bắt đầu từ những dòng đứng đầu.\n"
        "3. Bấm vào một dòng để xem hình vẽ của trường hợp đó. Bấm vào một người trong hình để mở hồ sơ của họ."
    )
    st.subheader("Điều cần biết")
    st.markdown(
        "- Kết quả là gợi ý để xem xét, không phải kết luận. Quyết định cuối cùng vẫn thuộc về bạn.\n"
        "- Số liệu được tính lại định kỳ, không phải theo thời gian thực. Ngày cập nhật ghi bên cạnh mỗi câu hỏi.\n"
        "- Tên khách hàng ở đây là tên đặt lại, không phải tên thật. Số điện thoại và giấy tờ không hiển thị. Tên cửa hàng là tên kinh doanh công khai."
    )

elif page == QUESTIONS:
    exps = [e for e in experiments() if e.owner_domain in my_units()]
    if not exps:
        st.title(QUESTIONS)
        st.info("Doanh nghiệp này chưa có câu hỏi nào được thiết lập.")
        st.stop()
    names_ = [e.name for e in exps]
    current = st.session_state.get("question")
    unit = st.selectbox(
        "Đơn vị", my_units(), format_func=lambda u: UNIT.get(u, u),
        index=my_units().index(next(e.owner_domain for e in exps if e.name == current)) if current in names_ else 0,
    )  # fmt: skip
    in_unit = [e for e in exps if e.owner_domain == unit]
    e = st.radio(
        "Câu hỏi", in_unit, format_func=lambda x: x.question, index=next((i for i, x in enumerate(in_unit) if x.name == current), 0)
    )
    st.title(e.title)
    st.write(e.question)
    history = runs(e.name)
    if not history:
        st.info("Chưa có kết quả cho câu hỏi này. Người phụ trách dữ liệu của đơn vị sẽ chạy và cập nhật.")
    else:
        st.caption(f"Cập nhật {updated(e.name)}. {e.reading}")
        table = ranked(e.name)
        top = st.slider("Số dòng hiển thị", 10, 500, 50, step=10)
        shown = friendly(table.head(top))
        numbers = {c: st.column_config.NumberColumn(format="localized") for c in shown.columns if shown[c].dtype.is_numeric()}
        chosen = st.dataframe(
            shown.to_pandas(),
            column_config=numbers,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"rows_{e.name}",
        ).selection.rows
        keys = [c for c in ("party_id", "src_id", "dst_id") if c in table.columns]
        # a name that starts like a formula must open as text in a spreadsheet
        export = shown.with_columns(pl.col(pl.String).str.replace(r"^([=+\-@])", "'$1"))
        export = export.with_columns(table.head(top).select(pl.col(c).alias(f"Mã đối chiếu ({COLUMN[c].lower()})") for c in keys))
        st.download_button("Tải danh sách (CSV)", export.write_csv(), file_name=f"{e.name}_{history[0].name}.csv")
        if not keys or table.is_empty():
            pass
        elif not graph_on():
            st.info("Hình vẽ chưa hiển thị được vì hệ thống chưa kết nối cơ sở dữ liệu đồ thị.")
        elif "cluster_id" in table.columns:
            groups = table.unique("cluster_id", keep="first", maintain_order=True)
            group = st.selectbox(
                "Xem nhóm",
                groups["cluster_id"].to_list(),
                format_func=lambda c: f"Nhóm hạng {groups.filter(pl.col('cluster_id') == c)['rank'][0]}",
            )
            members = table.filter(pl.col("cluster_id") == group)["party_id"].to_list()[:30]
            show(drawn("group", members, e.as_of, watch(e)), f"net_{e.name}")
        else:
            case = table.row(chosen[0] if chosen else 0, named=True)
            st.subheader(name_of(case[keys[0]]))
            st.caption("Bấm một dòng trong danh sách để xem trường hợp khác. Bấm một người trong hình để mở hồ sơ của họ.")
            if "item_id" in table.columns:
                suggested = table.filter(pl.col("party_id") == case["party_id"])["item_id"].to_list()[:5]
                show(drawn("taste", case["party_id"], e.as_of, suggested), f"net_{e.name}")
            else:
                show(drawn("party", case[keys[0]], case.get("as_of") or e.as_of, watch(e)), f"net_{e.name}")

elif page == LOOKUP:
    st.title("Tra cứu khách hàng")
    st.write("Tìm một khách hàng hoặc cửa hàng để xem họ dùng dịch vụ nào và có quan hệ với những ai.")
    history = runs("customer_360")
    if not history:
        st.info("Hồ sơ khách hàng chưa được tổng hợp. Người phụ trách dữ liệu sẽ chạy và cập nhật.")
    else:
        profiles = pl.read_parquet(history[0] / "output.parquet")
        find = st.text_input("Tìm theo tên", key="find", placeholder="Ví dụ: Trần Minh, CGV")
        if find:
            hits = names().filter(pl.col("party") & pl.col("plain").str.contains(plain(find), literal=True)).head(30)
            if hits.is_empty():
                st.warning("Không tìm thấy tên này.")
            else:
                label = dict(zip(hits["node_id"], hits["name"], strict=True))
                st.session_state["party"] = st.selectbox("Kết quả", list(label), format_func=label.get)
        row = profiles.filter(pl.col("party_id") == st.session_state.get("party"))
        if row.is_empty() and "party" in st.session_state:
            st.warning("Không tìm thấy hồ sơ của người này trong đợt tổng hợp gần nhất.")
            st.stop()
        if row.is_empty():
            row = profiles.filter(pl.col("party_id") == typical(profiles))
        r = row.row(0, named=True)
        st.subheader(name_of(r["party_id"]))
        a, b, c3, d4 = st.columns(4)
        a.metric("Đơn vị đang dùng", r["domain_cnt"])
        b.metric("Khoản vay", r["loan_cnt"])
        c3.metric("Giao dịch chuyển đi", f"{r['txn_out_cnt']:,}")
        d4.metric("Người có quan hệ", r["peer_cnt"])
        st.write("Đang dùng: " + ", ".join(UNIT.get(d, d) for d in r["domains"]))
        if graph_on():
            show(drawn("party", r["party_id"]), "net_lookup")
        else:
            st.info("Mạng quan hệ chưa hiển thị được vì hệ thống chưa kết nối cơ sở dữ liệu đồ thị.")
        with st.expander("Thông tin chi tiết"):
            st.dataframe(friendly(row).to_pandas().T.rename(columns={0: "Giá trị"}).astype(str), width="stretch")

elif page == DATA:
    st.title("Dữ liệu")
    st.write("Nạp dữ liệu của đơn vị vào platform và xem dữ liệu đã được kiểm tra chưa.")
    ds = [d for d in domains() if USER.may_change(d.domain)]
    st.dataframe(
        [
            {
                "Đơn vị": UNIT.get(d.domain, d.domain), "Nguồn": s.name,
                "Lần xử lý gần nhất": (manifest(d.domain, s.name) or {}).get("run_ts", "chưa có"),
                "Số dòng": sum(t["rows"] for t in (manifest(d.domain, s.name) or {"tables": {}})["tables"].values()),
            }
            for d in ds
            for s in d.sources
        ],
        width="stretch", hide_index=True,
    )  # fmt: skip
    dom = st.selectbox("Đơn vị", [d.domain for d in ds], format_func=lambda u: UNIT.get(u, u))
    contract = next(d for d in ds if d.domain == dom)
    source = st.selectbox("Nguồn", [s.name for s in contract.sources])

    st.subheader("Nạp file mới")
    table = st.text_input("Tên bảng", help="Ví dụ: customers. Mỗi lần nạp được lưu thành một đợt theo ngày.")
    files = st.file_uploader("File CSV, Parquet hoặc JSONL", accept_multiple_files=True, type=["csv", "parquet", "jsonl"])
    if st.button("Lưu vào hệ thống", disabled=not (table and files)):
        target = T.zone("landing") / dom / source / table / f"load_date={date.today().isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            (target / Path(f.name).name).write_bytes(f.getvalue())
        AUDIT.write(USER.username, "ui_upload", domain=dom, source=source, table=table, files=len(files))
        st.success(f"Đã lưu {len(files)} file cho bảng {table}.")

    declared = next(s for s in contract.sources if s.name == source)
    if declared.connection and st.button("Lấy dữ liệu mới từ hệ thống nguồn"):
        with st.spinner("Đang lấy dữ liệu..."):
            pulled = extract.pull(S, tenant, dom, source)
        AUDIT.write(USER.username, "ui_extract", source=source, rows=pulled)
        st.success("Đã lấy: " + ", ".join(f"{t} thêm {n:,} dòng" for t, n in pulled.items()))

    left_out = (manifest(dom, source) or {}).get("unused_columns", {})
    if left_out:
        st.caption("Cột có ở nguồn nhưng chưa đưa vào platform: " + "; ".join(f"{t}: {', '.join(c)}" for t, c in left_out.items()))

    if st.button("Kiểm tra và đưa vào platform"):
        if not (T.mappings / f"{source}.yaml").exists():
            st.warning("Nguồn mới chưa có quy tắc ánh xạ. Hệ thống sẽ tự lập khi được cấp quyền dùng mô hình AI.")
        else:
            with st.spinner("Đang kiểm tra và chuẩn hóa dữ liệu..."):
                result = pipeline.onboard(S, tenant, [dom])[source]
            AUDIT.write(USER.username, "ui_onboard", source=source, applied=result.applied)
            if result.applied:
                st.success("Dữ liệu đã qua kiểm tra và được đưa vào platform.")
            else:
                st.error("Dữ liệu chưa đạt kiểm tra. Platform giữ nguyên bản cũ.")
                st.json([g.model_dump() for g in result.failed])

    with st.expander("Chi tiết kỹ thuật: kết quả kiểm tra và từ điển dữ liệu"):
        m = manifest(dom, source)
        if m:
            st.dataframe(
                [
                    {"Kiểm tra": g["gate"], "Đạt": g["passed"], **{k: v for k, v in g["detail"].items() if k != "counts"}}
                    for g in m["gates"]
                ],
                width="stretch",
            )
        dict_path = T.dictionaries / f"{source}.yaml"
        if dict_path.exists():
            d = load_yaml(DataDictionary, dict_path)
            st.dataframe(
                [
                    {
                        "Bảng": t.name,
                        "Cột": c.name,
                        "Ý nghĩa": c.description,
                        "Loại": c.semantic_type,
                        "Dữ liệu cá nhân": c.pii,
                        "Đưa vào": c.maps_to or "",
                    }
                    for t in d.tables
                    for c in t.columns
                ],
                width="stretch",
            )

elif page == APPROVE:
    st.title("Phê duyệt")
    st.write("Những thay đổi hệ thống đề xuất nhưng cần người quyết định.")
    pending = waiting()
    if not pending:
        st.info("Không có đề xuất nào đang chờ.")
    for p in pending:
        with st.expander(f"{p.target}, do {p.author} đề xuất: {'sẽ xóa bớt dữ liệu đang có' if p.destructive else 'chưa qua kiểm tra'}"):
            st.write(p.rationale)
            failed = [g.model_dump() for g in p.gates if not g.passed]
            if failed:
                st.json(failed)
            st.json(p.payload)
            ok, no = st.columns(2)
            if ok.button("Duyệt", key=f"ok{p.id}"):
                st.success(f"Kết quả: {decide(S, tenant, p.id, True, USER.username)}")
            if no.button("Từ chối", key=f"no{p.id}"):
                st.success(f"Kết quả: {decide(S, tenant, p.id, False, USER.username)}")

elif page == SETUP:
    st.title("Thiết lập bài toán")
    st.write("Dành cho người phụ trách phân tích: chỉnh định nghĩa và chạy lại để cập nhật kết quả.")
    exps = experiments()
    if not exps:
        st.info("Doanh nghiệp này chưa có bài toán nào được thiết lập.")
        st.stop()
    name = st.selectbox("Bài toán", [e.name for e in exps], format_func=lambda n: next(e.title for e in exps if e.name == n))
    c = next(e for e in exps if e.name == name).model_copy(deep=True)
    st.caption(f"{c.question} Đơn vị phụ trách: {UNIT.get(c.owner_domain, c.owner_domain)}.")
    allowed = USER.may_change(c.owner_domain)
    if not allowed:
        st.info("Bạn chỉ được xem bài toán của đơn vị này.")

    if c.models:
        left, right = st.columns(2)
        with left:
            st.subheader("Thế nào là xấu")
            c.label.bad_threshold = st.number_input("Ngưỡng xấu (ví dụ số ngày quá hạn)", value=float(c.label.bad_threshold))
            band = st.number_input("Vùng xám bắt đầu từ (0 = không dùng)", value=float(c.label.indeterminate_from or 0))
            c.label.indeterminate_from = band or None
            c.label.performance_window_months = st.number_input(
                "Theo dõi trong bao nhiêu tháng", value=c.label.performance_window_months, min_value=1
            )
            c.population.exclude_prior_bad = st.checkbox("Loại khách đã từng xấu", value=c.population.exclude_prior_bad)
        with right:
            st.subheader("Dữ liệu dùng để học và để kiểm tra")
            c.split.train.start, c.split.train.end = st.date_input("Giai đoạn học", (c.split.train.start, c.split.train.end))
            c.split.oot.start, c.split.oot.end = st.date_input("Giai đoạn kiểm tra", (c.split.oot.start, c.split.oot.end))
            c.split.test_fraction = st.slider("Tỷ lệ giữ lại để kiểm tra", 0.05, 0.5, float(c.split.test_fraction))
    else:
        c.as_of = st.date_input("Tính đến ngày", c.as_of)
        with st.expander("Tham số"):
            c.params = {
                k: (
                    st.number_input(k, value=v)
                    if isinstance(v, int | float) and not isinstance(v, bool)
                    else st.text_input(k, value=str(v))
                )
                for k, v in c.params.items()
            }

    new_name = st.text_input("Lưu với tên", value=name, help="Giữ nguyên tên để ghi đè, hoặc đặt tên mới (chữ thường, gạch dưới).")
    save, run = st.columns(2)
    if save.button("Lưu thiết lập", disabled=not allowed):
        try:
            checked = BranchContract.model_validate(c.model_dump(mode="json") | {"name": new_name})
            dump_yaml(checked, T.branches / f"{new_name}.yaml")
            AUDIT.write(USER.username, "ui_save_experiment", experiment=new_name)
            st.success(f"Đã lưu {new_name}.")
        except ValueError as exc:
            st.error(f"Thiết lập không hợp lệ: {exc}")
    if run.button("Chạy lại để cập nhật kết quả", disabled=not allowed):
        with st.spinner("Đang chạy, có thể mất vài phút..."):
            _, out = branches.run_branch(S, tenant, name)
        AUDIT.write(USER.username, "ui_run_experiment", experiment=name, run=out.name)
        st.success("Đã cập nhật kết quả.")

    history = runs(name)
    if history:
        with st.expander("Báo cáo kỹ thuật của lần chạy gần nhất"):
            st.markdown((history[0] / "report.md").read_text(encoding="utf-8"))

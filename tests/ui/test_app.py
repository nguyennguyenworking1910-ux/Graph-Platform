"""Every page of the business-user interface renders on real pipeline output."""

import pytest
from streamlit.testing.v1 import AppTest

from graph_platform import branches
from tests.conftest import ROOT

APP = str(ROOT / "src/graph_platform/app.py")
PAGES = ["Giới thiệu", "Câu hỏi nghiệp vụ", "Tra cứu khách hàng", "Dữ liệu", "Thiết lập bài toán", "Phê duyệt"]


USERS = {"boss": ("admin", []), "ds_credit": ("analyst", ["credit"]), "guest": ("viewer", [])}


@pytest.fixture(scope="module")
def ready(built):
    from graph_platform.auth import User, hash_password, save_user

    for name in ("customer_360", "fraud_ring"):
        branches.run_branch(built, "alpha_group", name)
    built.users_path.unlink(missing_ok=True)
    for username, (role, domains) in USERS.items():
        save_user(
            built.users_path,
            User(
                username=username,
                name=username,
                role=role,
                tenants=["alpha_group"],
                domains=domains,
                password=hash_password("s3cret-" + username),
            ),
        )
    return built


def _open(ready, monkeypatch, username="boss", password=None):
    monkeypatch.setenv("GP_ROOT", str(ready.root))
    monkeypatch.setenv("GP_DATA_DIR", str(ready.data_dir))
    monkeypatch.delenv("GP_NEO4J_PASSWORD", raising=False)
    at = AppTest.from_file(APP, default_timeout=120).run()
    at.text_input[0].set_value(username)
    at.text_input[1].set_value(password or "s3cret-" + username)
    at.button[0].click().run()
    return at


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(ready, monkeypatch, page):
    at = _open(ready, monkeypatch)
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value


def test_sign_in_is_required_and_wrong_password_is_refused(ready, monkeypatch):
    at = _open(ready, monkeypatch, "boss", "wrong")
    assert not at.sidebar.radio and any("Sai tên đăng nhập" in e.value for e in at.error)
    stored = ready.users_path.read_text()
    assert "s3cret" not in stored and "scrypt$" in stored


def test_roles_limit_pages_and_business_units(ready, monkeypatch):
    guest = _open(ready, monkeypatch, "guest")
    assert guest.sidebar.radio[0].options == ["Giới thiệu", "Câu hỏi nghiệp vụ", "Tra cứu khách hàng"]

    ds = _open(ready, monkeypatch, "ds_credit")
    assert "Phê duyệt" not in ds.sidebar.radio[0].options
    ds.sidebar.radio[0].set_value("Thiết lập bài toán").run()
    next(x for x in ds.selectbox if x.label == "Bài toán").set_value("credit_score").run()
    assert not next(b for b in ds.button if b.label == "Lưu thiết lập").disabled

    other = _open(ready, monkeypatch, "ds_credit")
    other.sidebar.radio[0].set_value("Thiết lập bài toán").run()
    next(x for x in other.selectbox if x.label == "Bài toán").set_value("fraud_ring").run()  # owned by the payment unit
    assert next(b for b in other.button if b.label == "Lưu thiết lập").disabled
    ds = _open(ready, monkeypatch, "ds_credit")
    ds.sidebar.radio[0].set_value("Dữ liệu").run()
    assert next(x for x in ds.selectbox if x.label == "Đơn vị").options == ["Tín dụng"]


def test_business_user_changes_label_and_saves_a_new_experiment(ready, monkeypatch):
    at = _open(ready, monkeypatch, "ds_credit")
    at.sidebar.radio[0].set_value("Thiết lập bài toán").run()
    next(x for x in at.selectbox if x.label == "Bài toán").set_value("credit_score").run()
    at.number_input[0].set_value(60.0).run()
    at.text_input[0].set_value("credit_score_dpd60").run()
    next(b for b in at.button if b.label == "Lưu thiết lập").click().run()
    assert not at.exception and any("Đã lưu" in s.value for s in at.success)
    saved = branches.load_contract(ready, "alpha_group", "credit_score_dpd60")
    assert saved.label.bad_threshold == 60 and saved.name == "credit_score_dpd60"
    (ready.tenant("alpha_group").branches / "credit_score_dpd60.yaml").unlink()
    from graph_platform.control.audit import Audit

    last = Audit(ready, "alpha_group").rows()[-1]
    assert last["actor"] == "ds_credit" and last["action"] == "ui_save_experiment"


def test_introduction_speaks_to_the_users_unit_in_plain_words(ready, monkeypatch):
    ds = _open(ready, monkeypatch, "ds_credit")
    text = " ".join(m.value for m in [*ds.markdown, *ds.caption])
    assert "Khoản vay nào có khả năng nợ xấu?" in text and "Tín dụng" in text
    assert "Nhóm gian lận" not in text and "Những nhóm tài khoản nào" not in text  # another unit's questions stay out
    for jargon in ("experiment", "OOT", "gate", "plugin", "node_level", "credit_score"):
        assert jargon not in text, jargon

    boss = _open(ready, monkeypatch)
    next(b for b in boss.button if b.key == "go_fraud_ring").click().run()
    assert boss.title[0].value == "Nhóm gian lận" and not boss.exception
    shown = boss.dataframe[0].value
    assert "Khách hàng" in shown.columns and "cluster_id" not in shown.columns
    assert not shown["Khách hàng"].str.startswith("gp_").any()  # names, never ids

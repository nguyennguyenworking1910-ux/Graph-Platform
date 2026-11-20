import json
from datetime import date

import polars as pl
import pytest
from pydantic import ValidationError

from graph_platform import branches, sdk
from graph_platform.branches.dataset import KEYS, population
from graph_platform.contracts import BranchContract, GraphScope
from graph_platform.naming import FeatureName

A = "alpha_group"


@pytest.fixture(scope="module")
def credit(built):
    return branches.run_branch(built, A, "credit_score")


def test_credit_experiment_end_to_end(built, credit):
    metrics, out = credit
    assert set(metrics["models"]) == {"gbdt_profile", "gbdt_graph", "graphsage"}
    for m in metrics["models"].values():
        for split in ("test", "oot"):
            assert 0 <= m[split]["auc"] <= 1 and abs(m[split]["gini"] - (2 * m[split]["auc"] - 1)) < 1e-9
    up = metrics["uplift_vs_baseline"]["gbdt_graph"]["oot"]
    assert up["ci_low"] <= up["delta"] <= up["ci_high"]
    scores = pl.read_parquet(out / "output.parquet")
    assert set(KEYS) <= set(scores.columns) and scores["score_gbdt_graph"].is_between(0, 1).all()
    report = (out / "report.md").read_text()
    assert "OOT AUC" in report and "not business value" in report
    assert (out.parent.parent / "mlflow.db").exists() and (out / "graphsage.pt").exists()


def test_training_table_is_point_in_time_and_named_by_standard(built):
    c = branches.load_contract(built, A, "credit_score")
    df = branches.training_table(built, A, c)
    assert (df["as_of"] <= df["event_time"].dt.date()).all()
    assert set(df["split"].unique()) == {"train", "test", "oot"}
    oot = df.filter(pl.col("split") == "oot")["event_time"].dt.date()
    assert oot.min() >= c.split.oot.start and oot.max() <= c.split.oot.end
    # a party is never on both sides of the in-time split
    sides = df.filter(pl.col("split") != "oot").group_by("party_id").agg(pl.col("split").n_unique().alias("k"))
    assert sides["k"].max() == 1
    for col in df.columns:
        if col not in KEYS:
            FeatureName.parse(col)


def test_business_unit_can_redefine_label_split_and_scope(built):
    base = branches.load_contract(built, A, "credit_score")
    pop = population(built, A, base)

    softer = base.model_copy(deep=True)
    softer.label.bad_threshold, softer.label.indeterminate_from = 30, None
    assert population(built, A, softer)["label"].mean() > pop["label"].mean()

    no_band = base.model_copy(deep=True)
    no_band.label.indeterminate_from = None
    assert population(built, A, no_band).height > pop.height  # indeterminates come back as goods

    moved = base.model_copy(deep=True)
    moved.split.train.end, moved.split.oot.start = date(2024, 3, 31), date(2024, 4, 1)
    later = population(built, A, moved)
    assert later.filter(pl.col("split") == "oot").height > pop.filter(pl.col("split") == "oot").height

    big = base.model_copy(deep=True)
    big.population.min_amount = pop["party__profile__event_amt_val__application__all"].median()
    assert population(built, A, big).height < pop.height

    scoped = base.model_copy(deep=True)
    scoped.graph_scope = GraphScope(lookback_days=90, max_degree=5)
    scoped.models = [m for m in scoped.models if m.kind == "gbdt"]
    df = branches.training_table(built, A, scoped)
    assert df["party__khop__neighbor_cnt__peer_h1__all"].max() <= 5
    assert scoped.graph_scope.scope_id != GraphScope().scope_id


def test_invalid_experiment_configs_are_rejected(built):
    raw = branches.load_contract(built, A, "credit_score").model_dump(mode="json")
    for mutate in (
        lambda r: r["split"]["oot"].update(start="2024-01-01"),  # overlaps train
        lambda r: r["label"].update(indeterminate_from=120),  # above bad threshold
        lambda r: r.update(baseline="nope"),
        lambda r: r.pop("label"),
        lambda r: r.update(Name="x"),  # unknown key
        lambda r: r["models"][0].update(kind="xgboost"),
    ):
        bad = json.loads(json.dumps(raw))
        mutate(bad)
        with pytest.raises(ValidationError):
            BranchContract.model_validate(bad)


def test_fraud_ring_recovers_planted_rings(built):
    metrics, out = branches.run_branch(built, A, "fraud_ring", evaluate=True)
    gt = metrics["ground_truth"]
    assert gt["ring_recall"] >= 0.8 and gt["r_precision"] >= 0.8, gt
    table = pl.read_parquet(out / "output.parquet")
    assert {"cluster_id", "party_id", "rank", "score"} <= set(table.columns)


def test_circular_flow_finds_layering(built):
    metrics, _ = branches.run_branch(built, A, "aml_circular_flow", evaluate=True)
    assert metrics["cluster_cnt"] > 0 and metrics["ground_truth"]["r_precision"] >= 0.6, metrics


def test_recommendation_beats_popularity(built):
    metrics, out = branches.run_branch(built, A, "movie_recommendation")
    assert metrics["item_cf"]["hit_rate_at_10"] > metrics["popularity"]["hit_rate_at_10"]
    recs = pl.read_parquet(out / "output.parquet")
    assert recs.group_by("party_id").len()["len"].max() == 10


def test_ota_recommendation_reuses_the_same_plugin(built):
    metrics, _ = branches.run_branch(built, A, "ota_trip_recommendation")
    assert metrics["users_evaluated"] > 0 and 250 < metrics["items"] <= 300 and "item_cf_peer" in metrics


def test_quasi_cash_merchants_rank_first(built):
    metrics, out = branches.run_branch(built, A, "merchant_quasi_cash", evaluate=True)
    assert metrics["ground_truth"]["r_precision"] >= 0.8, metrics
    table = pl.read_parquet(out / "output.parquet")
    assert table["score"].is_sorted(descending=True) and {"round_amt_ratio", "top_payer_ratio", "repeat_ratio"} <= set(table.columns)


def test_customer_360_has_one_profile_per_party(built):
    metrics, out = branches.run_branch(built, A, "customer_360")
    table = pl.read_parquet(out / "output.parquet")
    parties = pl.read_parquet(built.data_dir / A / "shared/graph/nodes/Party.parquet")
    assert table["party_id"].n_unique() == table.height == parties.height
    assert metrics["multi_domain_ratio"] > 0.5
    rich = table.filter((pl.col("loan_cnt") > 0) & (pl.col("movie_cnt") > 0) & (pl.col("txn_out_cnt") > 0))
    assert rich.height > 50  # the same person seen through credit, movies and payment
    assert "full_name" not in table.columns


def test_branches_do_not_reach_into_the_trunk(built):
    """Plugins see the graph projection and the feature store, never landing/canonical or trunk internals."""
    from pathlib import Path

    import graph_platform.branches as pkg

    for f in Path(pkg.__file__).parent.rglob("*.py"):
        text = f.read_text()
        assert "graph_platform.trunk" not in text and "graph_platform.simulator" not in text, f
        assert '"canonical"' not in text and '"landing"' not in text, f


def test_sdk_training_table_in_five_lines(built, monkeypatch):
    monkeypatch.setenv("GP_ROOT", str(built.root))
    monkeypatch.setenv("GP_DATA_DIR", str(built.data_dir))
    df = sdk.training_table(A, "credit_score")
    assert df.height > 0 and "label" in df.columns
    cfg = sdk.experiment(A, "credit_score")
    cfg.label.bad_threshold = 60
    assert sdk.training_table(A, "credit_score", cfg)["label"].mean() >= df["label"].mean()
    ft = sdk.feature_table(A, date(2024, 6, 1), ["profile", "degree"])
    assert ft["party_id"].n_unique() == ft.height
    assert "description" in sdk.feature_registry(A).columns


def test_experiment_params_cannot_inject_sql_or_paths(built):
    c = branches.load_contract(built, A, "movie_recommendation")
    from graph_platform.branches.plugins import link_prediction
    from graph_platform.branches.runner import Context

    for bad in ({"edge": "../../beta_group/x"}, {"edge": "PURCHASES; DROP"}, {"item_subtype": "movie' OR '1'='1"}):
        hostile = c.model_copy(update={"params": c.params | bad})
        with pytest.raises((KeyError, ValueError)):
            link_prediction.run(Context(built, A, hostile))


def test_mlflow_artifacts_stay_inside_the_tenant(built, credit, monkeypatch, tmp_path):
    _, out = credit
    base = out.parent.parent
    assert any((base / "mlflow_artifacts" / "credit_score").rglob("report.md"))


def test_early_warning_scores_good_borrowers_out_of_time(built):
    metrics, out = branches.run_branch(built, A, "credit_early_warning")
    assert metrics["rows"]["train"] > 0 and metrics["rows"]["test"] > 0
    assert 0 <= metrics["with_network"]["auc"] <= 1 and metrics["uplift"]["ci_low"] <= metrics["uplift"]["delta"]
    table = pl.read_parquet(out / "output.parquet")
    assert table["as_of"].n_unique() == 1 and table["score"].is_sorted(descending=True)


def test_party_knowledge_documents_are_json_without_pii(built):
    import json

    metrics, out = branches.run_branch(built, A, "party_knowledge")
    docs = pl.read_parquet(out / "output.parquet")
    assert metrics["document_cnt"] == docs.height > 0
    doc = json.loads(docs["context"][0])
    assert set(doc) == {"as_of", "features", "peers"} and doc["peers"] and "party__khop__neighbor_cnt__peer_h1__all" in doc["features"]
    users = pl.read_parquet(built.data_dir / A / "landing/payment/wallet_pay/users.parquet")
    text = "".join(docs["context"].head(50))
    assert not [v for v in users["phone_number"].head(300) if v in text]


def test_answer_uses_only_the_context_document():
    from graph_platform.agents.roles import answer

    class Echo:
        def complete(self, agent, system, user):
            self.seen = (agent, system, user)
            return "ok"

    llm = Echo()
    assert answer(llm, "Khách này có rủi ro không?", '{"features": {}}') == "ok"
    assert llm.seen[0] == "knowledge" and "Khách này có rủi ro không?" in llm.seen[2] and '{"features": {}}' in llm.seen[2]


@pytest.mark.parametrize("name", ["insurance_cross_sell", "merchant_recommendation"])
def test_more_link_prediction_experiments_run_from_config(built, name):
    metrics, _ = branches.run_branch(built, A, name)
    assert metrics["users_evaluated"] > 0 and "item_cf_peer" in metrics


def test_promo_abuse_reuses_the_community_plugin(built):
    metrics, _ = branches.run_branch(built, A, "promo_abuse", evaluate=True)
    assert metrics["scored_cluster_cnt"] > 0 and metrics["ground_truth"]["ring_recall"] >= 0.8


@pytest.mark.parametrize("name", ["savings_churn", "insurance_claim_risk", "mule_account", "merchant_chargeback_risk"])
def test_party_outcome_experiments_run_from_config(built, name):
    metrics, out = branches.run_branch(built, A, name)
    assert metrics["rows"]["train"] > 0 and metrics["rows"]["test"] > 0 and 0 <= metrics["with_network"]["auc"] <= 1
    table = pl.read_parquet(out / "output.parquet")
    assert table["as_of"].n_unique() == 1 and table["score"].is_sorted(descending=True)


def test_flow_centrality_ranks_hubs(built):
    metrics, out = branches.run_branch(built, A, "supply_chain_hubs")
    table = pl.read_parquet(out / "output.parquet")
    assert metrics["party_cnt"] == table.height > 0 and table["score"].is_sorted(descending=True)
    assert table["party_id"].n_unique() == table.height
    metrics, out = branches.run_branch(built, A, "referral_influence")
    top = pl.read_parquet(out / "output.parquet").head(1)
    assert top["inflow_sum"][0] >= 0 and metrics["edge_cnt"] > 0


def test_purge_overlap_removes_training_rows_that_mature_inside_oot(built):
    base = branches.load_contract(built, A, "credit_score")
    purged = base.model_copy(deep=True)
    purged.split.purge_overlap = True
    before, after = population(built, A, base), population(built, A, purged)
    assert after.filter(pl.col("split") == "oot").height == before.filter(pl.col("split") == "oot").height
    assert after.filter(pl.col("split") != "oot").height < before.filter(pl.col("split") != "oot").height


def test_transaction_fraud_classifies_edges_after_as_of(built):
    metrics, out = branches.run_branch(built, A, "transaction_fraud")
    c = branches.load_contract(built, A, "transaction_fraud")
    table = pl.read_parquet(out / "output.parquet")
    assert metrics["rows"]["train"] > 0 and table.height == metrics["rows"]["test"]
    assert table["event_time"].dt.date().min() > c.as_of and table["score"].is_sorted(descending=True)


def test_customer_ltv_predicts_future_spend(built):
    metrics, out = branches.run_branch(built, A, "customer_ltv")
    assert metrics["with_network"]["spearman"] > 0.2 and 0 < metrics["top_decile_value_ratio"] <= 1
    assert pl.read_parquet(out / "output.parquet")["predicted"].min() >= -1e-6


def test_collections_only_scores_borrowers_already_late(built):
    metrics, out = branches.run_branch(built, A, "collections_priority")
    full, _ = branches.run_branch(built, A, "credit_early_warning")
    assert 0 < metrics["rows"]["train"] < full["rows"]["train"]


def test_co_booking_groups_and_scalping(built):
    metrics, out = branches.run_branch(built, A, "ota_co_booking_groups")
    groups = pl.read_parquet(out / "output.parquet")
    assert metrics["group_cnt"] > 0 and groups.group_by("cluster_id").len()["len"].min() >= 2
    metrics, _ = branches.run_branch(built, A, "ticket_scalping", evaluate=True)
    assert metrics["ground_truth"]["recall"] >= 0.5 and metrics["burst_purchase_max"] >= 5


def test_every_experiment_has_plain_language_text(built):
    for f in built.tenant(A).branches.glob("*.yaml"):
        c = branches.load_contract(built, A, f.stem)
        assert c.title and c.question.endswith("?") and c.reading, f.stem
        assert not any(w in (c.title + c.question + c.reading).lower() for w in ("auc", "oot", "node", "edge", "graph", "feature"))


def test_merchants_have_their_own_peer_graph(built):
    from datetime import date

    from graph_platform import features
    from graph_platform.contracts import GraphScope
    from graph_platform.graph import graph_dir

    g = graph_dir(built, A)
    khop = features.compute(g, date(2025, 6, 1), built.ontology, GraphScope(), ["khop"])["khop"]
    merchants = pl.read_parquet(g / "nodes/Party.parquet").filter(pl.col("subtype") == "merchant").select(party_id="node_id")
    assert khop.join(merchants, on="party_id")["party__khop__neighbor_cnt__peer_h1__all"].max() > 0


def test_corridor_anomaly_puts_the_planted_corridor_first(built):
    metrics, out = branches.run_branch(built, A, "corridor_anomaly", evaluate=True)
    assert metrics["ground_truth"]["planted_rank"] == 1 and metrics["growth_max"] > 5
    assert pl.read_parquet(out / "output.parquet")["usual_txn_cnt"].min() >= 0


def test_limit_review_combines_risk_and_value(built):
    for name in ("credit_early_warning", "customer_ltv"):
        branches.run_branch(built, A, name)
    metrics, out = branches.run_branch(built, A, "credit_limit_review")
    table = pl.read_parquet(out / "output.parquet")
    assert set(table["action"].unique()) <= {"Tăng hạn mức", "Giữ nguyên", "Xem xét giảm"} and metrics["customer_cnt"] == table.height > 0
    worst = table.filter(pl.col("action") == "Xem xét giảm")["risk_score"].min()
    assert worst >= table.filter(pl.col("action") == "Tăng hạn mức")["risk_score"].max()


def test_merchant_lending_population_is_merchant_loans(built):
    c = branches.load_contract(built, A, "merchant_lending")
    pop = population(built, A, c)
    merchants = pl.read_parquet(built.data_dir / A / "shared/graph/nodes/Party.parquet").filter(pl.col("subtype") == "merchant")
    assert pop.height > 0 and pop["party_id"].is_in(merchants["node_id"].implode()).all()
    assert pop["event_id"].str.contains(":Event:ML").all() and set(pop["label"].unique()) == {0, 1}

"""Model families available to branches."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import torch
from scipy.sparse import diags
from sklearn.ensemble import HistGradientBoostingClassifier
from torch import nn

from graph_platform import features
from graph_platform.config import Settings
from graph_platform.contracts import GraphScope
from graph_platform.contracts.branch import ModelSpec
from graph_platform.features.snapshot import open_snapshot
from graph_platform.graph import graph_dir


def informative(df: pl.DataFrame, cols: list[str], train: np.ndarray) -> list[str]:
    """Columns with at least one known value in the training rows; an all-missing column cannot be binned."""
    known = df.filter(pl.Series(train)).select(pl.col(cols).cast(pl.Float64).is_not_nan().fill_null(False).any()).row(0)
    return [c for c, ok in zip(cols, known, strict=True) if ok]


def fit_gbdt(x: np.ndarray, y: np.ndarray, params: dict) -> HistGradientBoostingClassifier:
    defaults = {"max_iter": 400, "learning_rate": 0.05, "max_leaf_nodes": 31, "l2_regularization": 1.0}
    return HistGradientBoostingClassifier(
        **(defaults | params), early_stopping=True, validation_fraction=0.15, n_iter_no_change=30, random_state=0
    ).fit(x, y)


def _squash(x: np.ndarray) -> np.ndarray:
    x = np.nan_to_num(x)
    return np.sign(x) * np.log1p(np.abs(x))


class GraphSage(nn.Module):
    """Two mean-aggregation layers over the peer graph, then an event-level head."""

    def __init__(self, d_node: int, d_event: int, d_hidden: int = 32, dropout: float = 0.3) -> None:
        super().__init__()
        self.drop = nn.Dropout(dropout)
        self.l1 = nn.Linear(2 * d_node, d_hidden)
        self.l2 = nn.Linear(2 * d_hidden, d_hidden)
        self.head = nn.Sequential(nn.Linear(d_hidden + d_event, d_hidden), nn.ReLU(), nn.Linear(d_hidden, 1))

    def forward(self, x: torch.Tensor, adj: torch.Tensor, rows: torch.Tensor, event: torch.Tensor) -> torch.Tensor:
        h = self.drop(torch.relu(self.l1(torch.cat([x, torch.sparse.mm(adj, x)], 1))))
        h = self.drop(torch.relu(self.l2(torch.cat([h, torch.sparse.mm(adj, h)], 1))))
        return self.head(torch.cat([h[rows], event], 1)).squeeze(1)


def graphsage_scores(
    settings: Settings, tenant: str, df: pl.DataFrame, spec: ModelSpec, scope: GraphScope, event_cols: list[str]
) -> tuple[np.ndarray, GraphSage]:
    """Trains on rows with split == 'train', one graph per as_of; returns a score for every row of df."""
    torch.manual_seed(0)
    graph = graph_dir(settings, tenant, principal="branches")
    df = df.with_row_index("_row")
    batches: dict[date, dict] = {}
    for d in df["as_of"].unique().sort().to_list():
        snap = open_snapshot(graph, d, settings.ontology_of(tenant), scope)
        ids = pl.DataFrame({"party_id": snap.party_ids}).with_row_index("_node")
        # a party unseen before as_of (a brand-new applicant) maps to one extra isolated node with no features
        part = df.filter(pl.col("as_of") == d).join(ids, on="party_id", how="left").with_columns(pl.col("_node").fill_null(len(ids)))
        n = len(ids) + 1
        a = (snap.peers > 0).astype(np.float32)
        a.resize((n, n))
        ids = pl.concat([ids, pl.DataFrame({"_node": [len(ids)], "party_id": [None]}, schema=ids.schema)])
        # population treatment: only the model_hops neighborhood of the scored parties is materialized
        reach = np.zeros(a.shape[0], bool)
        reach[part["_node"].to_numpy()] = True
        for _ in range(scope.model_hops):
            reach |= (a @ reach.astype(np.float32)) > 0
        keep = np.flatnonzero(reach)
        local = np.full(a.shape[0], -1)
        local[keep] = np.arange(keep.size)
        a = a[keep][:, keep]
        deg = np.asarray(a.sum(axis=1)).ravel()
        norm = (diags(np.divide(1.0, deg, out=np.zeros_like(deg), where=deg > 0)) @ a).tocoo()
        feats = ids[keep].join(features.load(settings, tenant, d, spec.families, scope), on="party_id", how="left")
        batches[d] = {
            "x": _squash(feats.drop("party_id", "_node").to_numpy().astype(np.float64)),
            "adj": norm,
            "rows": local[part["_node"].to_numpy()].astype(np.int64),
            "event": _squash(part.select(event_cols).to_numpy().astype(np.float64)),
            "y": part["label"].to_numpy().astype(np.float32),
            "train": (part["split"] == "train").to_numpy(),
            "row": part["_row"].to_numpy(),
        }

    # tensors are created only after every snapshot is read: torch's thread pool starves DuckDB otherwise
    train = [b for b in batches.values() if b["train"].any()]
    pooled = np.vstack([b["x"] for b in train])
    mu, sd = pooled.mean(0), pooled.std(0) + 1e-6
    ev = np.vstack([b["event"] for b in train])
    emu, esd = ev.mean(0), ev.std(0) + 1e-6
    for b in batches.values():
        n = b["adj"]
        idx = torch.as_tensor(np.vstack([n.row, n.col]).astype(np.int64))
        b["adj"] = torch.sparse_coo_tensor(idx, torch.as_tensor(n.data.astype(np.float32)), n.shape, check_invariants=False).coalesce()
        b["x"] = torch.as_tensor(((b["x"] - mu) / sd).astype(np.float32))
        b["event"] = torch.as_tensor(((b["event"] - emu) / esd).astype(np.float32))
        b["rows"], b["y"], b["train"] = torch.as_tensor(b["rows"]), torch.as_tensor(b["y"]), torch.as_tensor(b["train"])

    y_all = torch.cat([b["y"][b["train"]] for b in train])
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=(1 - y_all.mean()) / y_all.mean())
    par = {"hidden": 32, "dropout": 0.3, "lr": 5e-3, "weight_decay": 1e-3, "epochs": 30} | spec.params
    model = GraphSage(train[0]["x"].shape[1], len(event_cols), par["hidden"], par["dropout"])
    opt = torch.optim.Adam(model.parameters(), lr=par["lr"], weight_decay=par["weight_decay"])
    for _ in range(par["epochs"]):
        for b in train:
            opt.zero_grad()
            logit = model(b["x"], b["adj"], b["rows"], b["event"])
            loss_fn(logit[b["train"]], b["y"][b["train"]]).backward()
            opt.step()

    model.eval()
    scores = np.zeros(df.height)
    with torch.no_grad():
        for b in batches.values():
            scores[b["row"]] = torch.sigmoid(model(b["x"], b["adj"], b["rows"], b["event"])).numpy()
    return scores, model

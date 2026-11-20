"""Model evaluation metrics."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import roc_auc_score, roc_curve


def auc(y: np.ndarray, score: np.ndarray) -> float:
    return float(roc_auc_score(y, score))


def ks(y: np.ndarray, score: np.ndarray) -> float:
    fpr, tpr, _ = roc_curve(y, score)
    return float(np.max(tpr - fpr))


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    e = np.clip(np.histogram(expected, edges)[0] / expected.size, 1e-6, None)
    a = np.clip(np.histogram(actual, edges)[0] / actual.size, 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def summary(y: np.ndarray, score: np.ndarray) -> dict[str, float]:
    a = auc(y, score)
    return {"auc": a, "gini": 2 * a - 1, "ks": ks(y, score)}


def bootstrap_auc_delta(y: np.ndarray, base: np.ndarray, other: np.ndarray, n: int = 300, seed: int = 0) -> dict[str, float]:
    """Paired bootstrap of AUC(other) - AUC(base) on the same rows."""
    rng = np.random.default_rng(seed)
    deltas = []
    while len(deltas) < n:
        i = rng.integers(0, y.size, y.size)
        if 0 < y[i].sum() < i.size:
            deltas.append(auc(y[i], other[i]) - auc(y[i], base[i]))
    lo, hi = np.quantile(deltas, [0.025, 0.975])
    return {"delta": auc(y, other) - auc(y, base), "ci_low": float(lo), "ci_high": float(hi)}


def precision_at_k(hit: np.ndarray, k: int) -> float:
    return float(np.mean(hit[:k])) if k else 0.0

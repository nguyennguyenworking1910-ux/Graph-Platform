"""Deterministic health checks: feature drift between snapshots."""

from __future__ import annotations

from datetime import date

import numpy as np

from graph_platform import features
from graph_platform.branches.metrics import psi
from graph_platform.config import Settings

DRIFT_THRESHOLD = 0.25


def feature_drift(settings: Settings, tenant: str, family: str, reference: date, current: date) -> dict[str, float]:
    """PSI per feature between two snapshots; values above DRIFT_THRESHOLD need attention."""
    scope = features.load_scope(settings, tenant)
    a = features.load(settings, tenant, reference, [family], scope)
    b = features.load(settings, tenant, current, [family], scope)
    out = {}
    for col in a.columns[1:]:
        x, y = a[col].drop_nulls().to_numpy().astype(float), b[col].drop_nulls().to_numpy().astype(float)
        x, y = x[~np.isnan(x)], y[~np.isnan(y)]
        if x.size and y.size and np.unique(x).size > 1:
            out[col] = psi(x, y)
    return out

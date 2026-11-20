"""Branch contract: the experiment config a business unit owns. The only place a problem diverges from the trunk.

Every file is complete on its own: no inheritance between experiments.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from graph_platform.contracts.graph_scope import GraphScope
from graph_platform.naming import Snake

TaskFamily = Literal["node_level", "edge_level", "community", "entity_resolution", "path_flow", "temporal", "knowledge"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Period:
        if self.end < self.start:
            raise ValueError("end before start")
        return self


class Population(Strict):
    """Who is scored: one row per event of this subtype, attached to its party."""

    event: Snake
    min_amount: float | None = None
    max_amount: float | None = None
    exclude_prior_bad: bool = False  # drop parties whose own bad outcome was already observed at as_of


class Label(Strict):
    """Bad = worst `attribute` of the outcome events inside the performance window >= bad_threshold."""

    outcome_event: Snake
    attribute: Snake
    bad_threshold: float
    indeterminate_from: float | None = None  # worst value in [indeterminate_from, bad_threshold) -> row dropped
    performance_window_months: int = Field(gt=0)

    @model_validator(mode="after")
    def _band(self) -> Label:
        if self.indeterminate_from is not None and self.indeterminate_from >= self.bad_threshold:
            raise ValueError("indeterminate_from must be below bad_threshold")
        return self


class Split(Strict):
    """train period, an in-time test holdout by party, and an out-of-time period after it."""

    train: Period
    test_fraction: float = Field(0.2, gt=0, lt=1)
    oot: Period
    seed: int = 7
    purge_overlap: bool = False  # drop training rows whose performance window ends after oot starts

    @model_validator(mode="after")
    def _no_overlap(self) -> Split:
        if self.oot.start <= self.train.end:
            raise ValueError("oot must start after train ends")
        return self


class ModelSpec(Strict):
    name: Snake
    kind: Literal["gbdt", "graphsage"]
    families: list[Snake]  # feature families the model may see
    params: dict[str, Any] = {}


class BranchContract(Strict):
    name: Snake
    owner_domain: Snake  # business unit allowed to change this experiment
    # shown to business users: no technical terms
    title: str
    question: str  # the business question this answers
    reading: str  # how to read the result
    family: TaskFamily
    plugin: Snake  # module under graph_platform.branches.plugins
    target_entity: str
    as_of: date | None = None  # single snapshot date, for branches without a population
    population: Population | None = None
    label: Label | None = None
    split: Split | None = None
    graph_scope: GraphScope | None = None  # absent = the tenant's standard scope
    models: list[ModelSpec] = []
    baseline: Snake | None = None
    metrics: list[Snake]
    params: dict[str, Any] = {}

    @model_validator(mode="after")
    def _complete(self) -> BranchContract:
        if self.models:
            missing = [f for f in ("population", "label", "split", "baseline") if getattr(self, f) is None]
            if missing:
                raise ValueError(f"experiments that train models need population, label, split, baseline; missing {missing}")
            names = [m.name for m in self.models]
            if len(set(names)) != len(names) or self.baseline not in names:
                raise ValueError("model names must be unique and include the baseline")
        elif self.as_of is None:
            raise ValueError("experiments without models need as_of")
        return self

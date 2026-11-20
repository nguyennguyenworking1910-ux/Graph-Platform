"""Platform naming standard. Every name is validated against these rules; nothing is free-form.

Feature name: <entity>__<family>__<metric>__<qualifier>__<window>
  party__degree__cnt__transacts_out__90d
  party__khop__default_rate__refers_any_h1__all
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import StringConstraints

SNAKE = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
PASCAL = r"^[A-Z][a-z0-9]+([A-Z][a-z0-9]+)*$"
UPPER_SNAKE = r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$"

Snake = Annotated[str, StringConstraints(pattern=SNAKE)]
Pascal = Annotated[str, StringConstraints(pattern=PASCAL)]
UpperSnake = Annotated[str, StringConstraints(pattern=UPPER_SNAKE)]

FEATURE_FAMILIES = ("profile", "degree", "khop", "shared", "centrality", "community", "embedding")
FEATURE_WINDOWS = ("30d", "90d", "180d", "365d", "all")
# Last token of a metric must be one of these, so the statistic is always explicit.
METRIC_STATS = ("val", "code", "flag", "cnt", "nuniq", "sum", "avg", "max", "min", "std", "rate", "ratio", "score", "dim")

_PART = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_FEATURE = re.compile(rf"^({_PART})__({_PART})__({_PART})__({_PART})__([a-z0-9]+)$")


@dataclass(frozen=True)
class FeatureName:
    entity: str
    family: str
    metric: str
    qualifier: str
    window: str

    def __str__(self) -> str:
        return "__".join((self.entity, self.family, self.metric, self.qualifier, self.window))

    @classmethod
    def parse(cls, name: str) -> FeatureName:
        m = _FEATURE.match(name)
        if not m:
            raise ValueError(f"feature name '{name}' is not <entity>__<family>__<metric>__<qualifier>__<window>")
        return cls(*m.groups()).validated()

    def validated(self) -> FeatureName:
        problems = []
        for part in (self.entity, self.family, self.metric, self.qualifier):
            if not re.match(SNAKE, part):
                problems.append(f"'{part}' is not snake_case")
        if self.family not in FEATURE_FAMILIES:
            problems.append(f"family '{self.family}' not in {FEATURE_FAMILIES}")
        if self.window not in FEATURE_WINDOWS:
            problems.append(f"window '{self.window}' not in {FEATURE_WINDOWS}")
        stat = re.sub(r"\d+$", "", self.metric.rsplit("_", 1)[-1])
        if stat not in METRIC_STATS:
            problems.append(f"metric '{self.metric}' must end with one of {METRIC_STATS}")
        if problems:
            raise ValueError(f"invalid feature name '{self}': " + "; ".join(problems))
        return self


def feature_name(entity: str, family: str, metric: str, qualifier: str = "all", window: str = "all") -> str:
    return str(FeatureName(entity, family, metric, qualifier, window).validated())

"""Entity resolution: links the same real-world party across domains of one tenant."""

from graph_platform.resolution.resolve import ErRules, evaluate, resolve_parties

__all__ = ["ErRules", "evaluate", "resolve_parties"]

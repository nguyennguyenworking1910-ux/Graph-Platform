"""Tenant- and domain-scoped data access."""

from graph_platform.storage.access import AccessDenied, Principal, canonical_files, resolve

__all__ = ["AccessDenied", "Principal", "canonical_files", "resolve"]

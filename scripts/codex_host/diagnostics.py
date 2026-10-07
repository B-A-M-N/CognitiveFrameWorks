"""Compatibility exports for Codex feature-closure diagnostics."""
from cognitive_runtime_core.integrations.codex.diagnostics import (
    _native_feature_catalog,
    _native_feature_categories,
)

__all__ = ["_native_feature_catalog", "_native_feature_categories"]

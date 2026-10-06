"""Sanitized native-feature closure reporting."""
from __future__ import annotations

from collections.abc import Sequence

from .launch import (
    _HOST_ONLY_FEATURES,
    _NATIVE_EXEC_FEATURES,
    _NATIVE_MODEL_TOOL_FEATURES,
    _NATIVE_SIDE_EFFECT_FEATURES,
    _NATIVE_WORKSPACE_FEATURES,
    _native_feature_policy_identity,
)


def _native_feature_categories(supported: Sequence[str],
                               disabled: Sequence[str]) -> dict[str, list[str]]:
    supported_set, disabled_set = set(supported), set(disabled)
    return {
        "model_callable_disabled": sorted(
            supported_set & set(_NATIVE_MODEL_TOOL_FEATURES) & disabled_set),
        "execution_disabled": sorted(
            supported_set & set(_NATIVE_EXEC_FEATURES) & disabled_set),
        "persistent_side_effects_disabled": sorted(
            supported_set & set(_NATIVE_SIDE_EFFECT_FEATURES) & disabled_set),
        "workspace_dependency_features_disabled": sorted(
            supported_set & set(_NATIVE_WORKSPACE_FEATURES) & disabled_set),
        "unclosed_known_host_surfaces": sorted(
            (supported_set & _HOST_ONLY_FEATURES) - disabled_set),
    }


def _native_feature_catalog(supported: Sequence[str],
                            disabled: Sequence[str],
                            codex_catalog: dict[str, object] | None = None
                            ) -> dict[str, object]:
    identity = _native_feature_policy_identity()
    supported_set, disabled_set = set(supported), set(disabled)
    return {
        **identity,
        **(codex_catalog or {}),
        "supported_feature_count": len(supported_set),
        "known_closed_count": len(supported_set & _HOST_ONLY_FEATURES & disabled_set),
        "disabled_host_feature_count": len(supported_set & disabled_set),
        "feature_catalog_count": len(
            (codex_catalog or {}).get("feature_names", ())),
        "unknown_feature_count": (0 if codex_catalog else None),
        "reviewed_catalog_match": bool(codex_catalog),
        "host_policy_closed": not bool(
            (supported_set & _HOST_ONLY_FEATURES) - disabled_set),
    }

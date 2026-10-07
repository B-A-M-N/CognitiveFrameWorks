"""Validate immutable runtime contracts before constructing a CFW session."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime_contract.actions import (
    ActionDescriptor, action_contract_hash, default_action_registry,
    effective_action_registry,
)
from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.standalone_route_policy import PolicyEpochRecord

RUNTIME_MODES = frozenset({
    "standalone_advisory", "standalone_enforcing", "combined_enforcing",
})
DEFAULT_ACTION_REGISTRY: dict[str, ActionDescriptor] = default_action_registry()


@dataclass(frozen=True)
class ValidatedStart:
    runtime_mode: str
    pinned_contracts: Mapping[str, Any]
    action_registry: Mapping[str, ActionDescriptor]


def validate_start_contracts(
        bundle: Any, *, runtime_mode: str,
        behavioral_judge: Any | None,
        policy_epoch_record: PolicyEpochRecord | None,
        action_registry: Mapping[str, ActionDescriptor] | None,
        ) -> ValidatedStart:
    """Check pinned code, policy, and tool contracts before startup."""
    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unknown runtime mode {runtime_mode!r}")
    if behavioral_judge is not None:
        raise ValueError(
            "CFW no longer accepts a System-1 provider; semantic assessment "
            "and policy selection belong in DP")
    if "system_one_policy" in bundle.pinned:
        raise ValueError(
            "CFW task bundles no longer carry semantic provider policy; configure it in DP")
    pinned_contracts = bundle.pinned.get("contracts", {})
    if pinned_contracts.get("runtime_behavior_hash") != resolve_runtime.runtime_behavior_hash():
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime enforcement code changed after policy composition")

    if policy_epoch_record is not None and (
            not isinstance(policy_epoch_record, PolicyEpochRecord)
            or runtime_mode == "combined_enforcing"
            or policy_epoch_record.epoch_index < 1
            or policy_epoch_record.parent_policy_hash == policy_epoch_record.child_policy_hash
            or policy_epoch_record.child_policy_hash != bundle.pinned.get("policy_hash")
            or policy_epoch_record.route_type != "flow"
            or policy_epoch_record.reason != "dynamic_replan"
            or policy_epoch_record.decision_source not in {"metacognition", "operator"}
            or not policy_epoch_record.decision_ref
            or policy_epoch_record.state_revision < 0):
        raise resolve_runtime.SnapshotIntegrityError(
            "child policy epoch record does not match the immutable runtime bundle")

    resolved_action_registry = effective_action_registry(
        bundle.action_registry or DEFAULT_ACTION_REGISTRY)
    current_manifest = resolve_runtime.runtime_build_manifest(resolved_action_registry)
    pinned_manifest = pinned_contracts.get("runtime_build_manifest") or {}
    if (pinned_contracts.get("runtime_build_manifest_hash")
            != current_manifest.get("manifest_hash")
            or pinned_manifest != current_manifest):
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime build manifest changed after policy composition")
    if action_registry is not None:
        supplied_registry = effective_action_registry(action_registry)
        if action_contract_hash(supplied_registry) != action_contract_hash(
                resolved_action_registry):
            raise ValueError(
                "start-time action registry differs from the registry pinned at resolve()")
    if action_contract_hash(resolved_action_registry) != bundle.policy.action_contract_hash:
        raise resolve_runtime.SnapshotIntegrityError(
            "host action registry changed after policy composition")

    return ValidatedStart(
        runtime_mode=runtime_mode,
        pinned_contracts=pinned_contracts,
        action_registry=resolved_action_registry,
    )

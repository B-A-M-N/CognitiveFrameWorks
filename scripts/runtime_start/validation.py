"""Validate immutable runtime contracts before constructing a CFW session."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime_contract.actions import (
    ActionDescriptor, action_contract_hash, default_action_registry,
    effective_action_registry,
)
from runtime_contract.system_one import BehavioralJudge, SystemOneCapability, SystemOnePolicy
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
    system_one_policy: SystemOnePolicy
    action_registry: Mapping[str, ActionDescriptor]


def validate_start_contracts(
        bundle: Any, *, runtime_mode: str,
        behavioral_judge: BehavioralJudge | None,
        policy_epoch_record: PolicyEpochRecord | None,
        action_registry: Mapping[str, ActionDescriptor] | None,
        ) -> ValidatedStart:
    """Check pinned code, policy, provider, and tool contracts before startup."""
    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unknown runtime mode {runtime_mode!r}")
    pinned_contracts = bundle.pinned.get("contracts", {})
    if pinned_contracts.get("runtime_behavior_hash") != resolve_runtime.runtime_behavior_hash():
        raise resolve_runtime.SnapshotIntegrityError(
            "runtime enforcement code changed after policy composition")

    system_one_policy = SystemOnePolicy(
        **dict(bundle.pinned.get("system_one_policy") or {}))
    if dict(bundle.policy.system_one_policy) != system_one_policy.to_dict():
        raise resolve_runtime.SnapshotIntegrityError(
            "System-1 policy differs from the task's pinned policy")

    if policy_epoch_record is not None and (
            not isinstance(policy_epoch_record, PolicyEpochRecord)
            or runtime_mode == "combined_enforcing"
            or policy_epoch_record.epoch_index < 1
            or policy_epoch_record.parent_policy_hash == policy_epoch_record.child_policy_hash
            or policy_epoch_record.child_policy_hash != bundle.pinned.get("policy_hash")
            or policy_epoch_record.route_type != "flow"
            or policy_epoch_record.reason != "dynamic_replan"
            or policy_epoch_record.decision_source not in {
                "metacognition", "operator", "learned_profile", "semantic_advisory"}
            or not policy_epoch_record.decision_ref
            or (policy_epoch_record.decision_source == "semantic_advisory"
                and not policy_epoch_record.semantic_decision_ref)
            or policy_epoch_record.state_revision < 0):
        raise resolve_runtime.SnapshotIntegrityError(
            "child policy epoch record does not match the immutable runtime bundle")

    if behavioral_judge is not None:
        _validate_behavioral_judge(behavioral_judge, system_one_policy)

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
        system_one_policy=system_one_policy,
        action_registry=resolved_action_registry,
    )


def _validate_behavioral_judge(judge: BehavioralJudge,
                               policy: SystemOnePolicy) -> None:
    capability_method = getattr(judge, "capability", None)
    if not callable(capability_method):
        raise ValueError("System-1 provider must expose a capability handshake")
    capability = capability_method()
    if isinstance(capability, Mapping):
        capability = SystemOneCapability(
            **{**dict(capability),
               "supported_question_families": tuple(
                   capability.get("supported_question_families") or ())})
    if not isinstance(capability, SystemOneCapability):
        raise ValueError("System-1 provider returned an invalid capability handshake")
    pinned_capabilities = {
        "backend_id": policy.backend_id,
        "model_id": policy.model_id,
        "model_revision": policy.model_revision,
        "model_content_hash": policy.model_content_hash,
        "provider_protocol_hash": policy.provider_protocol_hash,
        "protocol_version": policy.protocol_version,
        "protocol_hash": policy.protocol_hash,
        "question_registry_hash": policy.question_registry_hash,
        "state_projection_schema_hash": policy.state_projection_schema_hash,
    }
    for field_name, expected in pinned_capabilities.items():
        if getattr(capability, field_name) != expected:
            raise ValueError(
                f"System-1 capability {field_name} differs from pinned policy")
    if capability.max_state_bytes < 32768:
        raise ValueError("System-1 provider cannot accept the maximum protocol frame")
    if not set(policy.enabled_question_families).issubset(
            capability.supported_question_families):
        raise ValueError(
            "System-1 provider does not support every enabled question family")
    if capability.stateful_sessions and (
            not callable(getattr(judge, "reset_identity", None))
            or not callable(getattr(judge, "resync", None))):
        raise ValueError("stateful System-1 provider must implement reset and resync")
    provider_cache_size = getattr(judge, "cache_size", None)
    if provider_cache_size is not None and int(provider_cache_size) != policy.cache_size:
        raise ValueError("injected System-1 cache size differs from pinned policy")
    provider_latency = getattr(judge, "max_latency_ms", None)
    if (provider_latency is not None
            and int(provider_latency) > policy.max_latency_ms):
        raise ValueError("injected System-1 provider exceeds pinned latency cap")

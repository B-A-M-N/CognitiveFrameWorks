"""Authoritative runtime/task manifest construction."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Dict


def build_task_manifest(bundle: Any, *, session_id: str = "prestart",
                        attempt_id: str = "attempt-1",
                        interaction_id: str | None = None,
                        parent_session_id: str | None = None,
                        delegator_agent_id: str | None = None,
                        delegate_agent_id: str | None = None,
                        delegation_id: str | None = None,
                        role: str | None = None) -> Dict[str, Any]:
    guard_pack = bundle.guard_pack
    policy = _thaw(bundle.pinned)
    # A task manifest is the authoritative compiler/result join used by DP;
    # wall-clock freeze time belongs to the separate task-instance snapshot
    # and must not make an otherwise identical trial manifest conflict.
    policy.pop("frozen_at", None)
    return {
        "task_id": bundle.task_id,
        "agent_id": bundle.pinned.get("agent_id", "agent"),
        "agent_instance_id": bundle.pinned.get("agent_instance_id", bundle.pinned.get("agent_id", "agent")),
        "session_id": session_id,
        "attempt_id": attempt_id,
        "interaction_id": interaction_id,
        "parent_session_id": parent_session_id,
        "delegator_agent_id": delegator_agent_id,
        "delegate_agent_id": delegate_agent_id,
        "delegation_id": delegation_id,
        "role": role,
        "policy_hash": bundle.pinned.get("policy_hash"),
        "active_guard_keys": [guard.get("key") or guard["id"]
                              for guard in guard_pack.get("guards", [])],
        "eligible_guard_keys": list(guard_pack.get("eligible_guard_keys", [])),
        "assigned_guard_keys": list(guard_pack.get("assigned_guard_keys", [])),
        "guard_assignments": dict(guard_pack.get("guard_assignments", {})),
        "canary_assignments": {
            bundle.task_id: [guard.get("key") or guard["id"]
                             for guard in guard_pack.get("guards", [])
                             if guard.get("status") == "canary"]},
        "kernel": list(bundle.kernel),
        "framework_kernel": list(bundle.kernel),
        "stateworks": [item.get("id") for item in bundle.stateworks],
        "statework_versions": {item.get("id"): item.get("version") for item in bundle.stateworks},
        "model": bundle.pinned.get("model"),
        "harness": bundle.pinned.get("harness"),
        "toolset": bundle.pinned.get("toolset"),
        "guard_pack_hash": bundle.pinned.get("guard_pack_hash"),
        "routing_profile_hash": bundle.policy.routing_profile_hash,
        "routing_adjustments": _thaw(bundle.guard_pack.get("routing_adjustments", [])),
        "suppressed_guard_keys": list(bundle.guard_pack.get("suppressed_guard_keys", [])),
        "task_family": bundle.profile,
        "task_shape": bundle.pinned.get("task_shape", bundle.profile),
        "phase": bundle.pinned.get("phase"),
        "domain_tags": list(bundle.pinned.get("domain_tags", [])),
        "operation": bundle.pinned.get("operation"),
        "trigger": bundle.pinned.get("trigger"),
        "environment": os.environ.get("CFW_ENVIRONMENT", "runtime"),
        "subject_ref": bundle.policy.subject_ref,
        "policy": policy,
    }


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value

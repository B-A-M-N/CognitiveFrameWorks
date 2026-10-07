#!/usr/bin/env python3
"""Runtime resolver: compose the FrameWorks kernel, CSW-owned workflow plan,
and guard pack for a task, freeze an immutable policy snapshot, and emit bundle.

Composition contract (no imports of DigitalPsychology code):

    TASK START
    ├─ read activation profile from shared/pipeline.yaml (single source)
    ├─ expand aliases (anchor/dox -> concrete stages)
    ├─ request eligible workflows and a legal dependency plan from CSW provider
    │   (CSW owns matching, packet prerequisites, composition, and flow choice)
    ├─ kernel = union(profile kernel, host integration requirements)
    ├─ order kernel by manifest order_after edges + requires closure
    │   (canonical manifest order for ties; no append-order influence)
    ├─ guard pack = always-on shared/runtime-kernel.yaml
    │               + DigitalPsychology compiled-guard-pack.json artifact
    │               (schema-validated; semantic content hash; fail-closed
    │                scope matching; never overwrites a snapshot)
    ├─ PolicySnapshot: immutable pin (framework/stateworks/guard pack/hash/
    │   model/harness/toolset/frozen_at)
    └─ EmergencyOverride: separate record, never mutates the snapshot

This module is a pure library: `resolve()` returns RuntimeBundle; the CLI is
only a debugging front end. The bundle writes to an out-of-tree per-task/agent
destination ($XDG_RUNTIME_DIR/cognitiveframeworks/<task>/<snapshot-hash>-<identity>.json),
never into the source tree.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Set, Union

from runtime_contract.paths import runtime_asset_root

ROOT = runtime_asset_root()
LOCAL_CONTRACT_ROOT = ROOT / "contracts"

from runtime_contract.guards import (enforcement_key as _contract_enforcement_key,
                                     nested_scope as _contract_nested_scope)
from runtime_contract.pipeline import load_pipeline_document
from runtime_contract.paths import runtime_state_dir
from runtime_contract.policy import freeze_contracts as _freeze_contracts
from runtime_contract.policy import stable_hash as _stable_contract_hash
from runtime_contract.stateworks import current_packet as _current_packet
from runtime_contract.task import AcceptanceCriterion, TaskGoalContract, TaskRequest
from runtime_contract.advisor import BehavioralAdvisor, bounded_adjustments, validate_advice_transaction
from runtime_contract.manifests import build_task_manifest
from runtime_contract.handlers import (enforcement_for_guard, handler_registry_hash,
                                       validate_enforcement)
from runtime_contract.validators import validator_registry_hash
from runtime_contract.actions import (action_contract_hash, default_action_registry,
                                       effective_action_registry)
from runtime_contract.compatibility import validate_compatibility
from runtime_contract.routing import (
    applicable_adjustments, assign_experiment_cohort, comparison_context_hash,
    load_routing_profiles, validate_adaptive_adjustment,
    validate_experiment_plan,
)
from runtime_contract.statework_provider import (
    DEFAULT_STATEWORK_ROOT, statework_provider,
)
from runtime_contract.workflow_port import (
    WorkflowEligibility, WorkflowPort, WorkflowResolution,
    validate_workflow_port,
)


from resolver.build_manifest import runtime_behavior_hash, runtime_build_manifest

from resolver.common import (
    _frozen_mapping, _load_json, _stable_json, _thaw, dp_trusted_guard_pack_path, load_yaml,
    sha256, sha256_bytes,
)
from resolver.guards import (
    GUARD_PACK_SCHEMA_URI, GUARD_SCHEMA_URI, GuardCompilationError,
    GuardPackIntegrityError, _guard_ref, _legacy_semantic_guard_hash,
    _load_guard_schemas, applicable_guards, canary_assigned, canonical_guard,
    compile_task_guards, guard_pack_semantic_hash, load_guard_pack,
    nested_guard_scope, semantic_guard_hash,
)

from resolver.workflows import (
    PacketDependencyCycle, PacketDependencyError, PacketValidationError,
    _consumed_inputs, _flow_selector, _load_cow_packet_store, _statework_flow_metadata,
    _statework_root, _statework_runtime_pins, _validate_statework_flow_contracts,
    enumerate_eligible_routes, load_stateworks, match_statework, match_stateworks,
    parse_packet_registry, plan_statework_chain, plan_statework_dag, read_manifest_hash,
    select_statework_runtime,
)

from resolver.policy_snapshot import (
    CapsulePin, EffectivePolicy, EmergencyOverride, GuardPin, PolicySnapshot,
    RuntimeBundle, SnapshotIntegrityError, StateWorkPin, _policy_from_bundle,
    compute_policy_hash, injected_capsules_for, load_runtime_capsules,
)

from resolver.task_resolution import resolve

from resolver.components import (canonical_stage_order, expand, load_pipeline,
                                 order_kernel, requires_closure)


STAGE_FIELDS = ["id", "owner", "requires", "order_after", "execution_mode", "frequency"]
BANNED_SISPIS_DIMENSIONS = [
    "option_multiplicity", "tradeoff_density", "ambiguity_of_framing",
    "comparative_intent", "downstream_impact",
]


















def _load_document(path: Path, loader: Callable[[Path], Any]) -> Any:
    if not path.exists():
        raise SnapshotIntegrityError(f"required policy contract is missing: {path}")
    return loader(path)



# -------------------------------------------------------- StateWork registry



















# ------------------------------------------------- packet-prerequisite chain











# ---------------------------------------------------------------- guard pack


























# ------------------------------------------------------- immutable snapshot













# ------------------------------------------------------------------ runtime
























def write_snapshot(bundle: RuntimeBundle, task_id: Optional[str] = None) -> Path:
    """Write separate semantic-policy and task-instance artifacts.

    ``policy_hash`` identifies stable policy meaning; ``frozen_at`` and the
    task/session envelope belong to the instance artifact and are therefore
    not allowed to make a semantic policy conflict.
    """
    task = task_id or bundle.task_id
    policy_hash = bundle.pinned.get("policy_hash", "unknown")
    policy_dir = runtime_state_dir() / "policies"
    policy_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(policy_dir, 0o700)
    policy_path = policy_dir / f"{policy_hash}.json"
    stable_contracts = {key: value for key, value in _thaw(bundle.pinned.get("contracts", {})).items()
                        if key not in {"routing_experiment", "policy_snapshot_metadata"}}
    policy_artifact = {
        "artifact_version": "policy-1",
        "policy_hash": policy_hash,
        "policy": {key: value for key, value in bundle.policy.canonical().items()
                   if key not in {"adopted_profile_hashes", "shared_workflow_membership",
                                  "policy_revocation_revision", "revocation_mode"}},
        "contracts": stable_contracts,
    }
    policy_lock_path = policy_dir / f".{policy_hash}.lock"
    policy_lock_path.touch(mode=0o600, exist_ok=True)
    with policy_lock_path.open("r+", encoding="utf-8") as policy_lock:
        fcntl.flock(policy_lock.fileno(), fcntl.LOCK_EX)
        if policy_path.exists():
            try:
                existing_policy = json.loads(policy_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SnapshotIntegrityError(f"existing policy artifact is unreadable: {exc}") from exc
            if existing_policy != policy_artifact:
                raise SnapshotIntegrityError(
                    f"existing semantic policy differs from requested policy: {policy_path}")
        else:
            fd, temporary = tempfile.mkstemp(dir=str(policy_dir), prefix=".policy-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(policy_artifact, indent=2, sort_keys=True) + "\n")
                    fh.flush(); os.fsync(fh.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, policy_path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        fcntl.flock(policy_lock.fileno(), fcntl.LOCK_UN)
    task_dir = runtime_state_dir() / _safe_task_id(task)
    task_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(task_dir, 0o700)
    _policy_from_bundle(bundle)
    snapshot_identity = hashlib.sha256("|".join(
        str(bundle.pinned.get(key) or "") for key in (
            "namespace_id", "application_instance_id", "agent_instance_id",
            "attempt_id", "interaction_id")).encode("utf-8")).hexdigest()[:16]
    out_path = task_dir / f"{policy_hash}-{snapshot_identity}.json"
    task_projection = bundle.to_dict()
    task_projection.setdefault("pinned", {}).pop("frozen_at", None)
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SnapshotIntegrityError(f"existing snapshot is unreadable: {exc}") from exc
        existing_hash = existing.get("pinned", {}).get("policy_hash")
        if existing_hash != policy_hash:
            raise SnapshotIntegrityError(
                f"existing snapshot policy mismatch: path={out_path} "
                f"existing={existing_hash!r} requested={policy_hash!r}")
        existing_projection = dict(existing)
        existing_projection.setdefault("pinned", {}).pop("frozen_at", None)
        if existing_projection != task_projection:
            raise SnapshotIntegrityError(
                f"existing snapshot content differs from requested policy: {out_path}")
        return out_path
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(task_dir), prefix=".snapshot-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(bundle.to_dict(), indent=2) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, out_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return out_path


def write_task_manifest(bundle: RuntimeBundle, *, session_id: str = "prestart",
                        attempt_id: str = "attempt-1", interaction_id: Optional[str] = None,
                        parent_session_id: Optional[str] = None,
                        delegator_agent_id: Optional[str] = None,
                        delegate_agent_id: Optional[str] = None,
                        delegation_id: Optional[str] = None,
                        role: Optional[str] = None) -> Path:
    """Write the immutable policy/telemetry join manifest beside telemetry."""
    root = runtime_state_dir() / "telemetry"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    digest = hashlib.sha256(
        f"{bundle.task_id}:{session_id}".encode("utf-8")).hexdigest()[:24]
    path = root / f"{digest}.manifest.json"
    payload = build_task_manifest(
        bundle, session_id=session_id, attempt_id=attempt_id,
        interaction_id=interaction_id, parent_session_id=parent_session_id,
        delegator_agent_id=delegator_agent_id, delegate_agent_id=delegate_agent_id,
        delegation_id=delegation_id, role=role)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise SnapshotIntegrityError(f"task manifest already exists with different policy: {path}")
        return path
    fd, temporary = tempfile.mkstemp(dir=str(root), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _safe_task_id(task_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")
    return cleaned or "task"




# ------------------------------------------------------------------- main

def main(argv: List[str] | None = None) -> int:
    argv = argv or sys.argv
    task_id = argv[1] if len(argv) > 1 else "task-default"
    shape = argv[2] if len(argv) > 2 else "implement"
    raw_domains = argv[3] if len(argv) > 3 else ""
    subject_ref = argv[4] if len(argv) > 4 else None
    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]

    try:
        bundle = resolve(task_id, shape, domains, subject_ref=subject_ref)
    except (PacketDependencyCycle, PacketDependencyError, ValueError) as exc:
        print(f"resolution error: {exc}")
        return 1
    except RecursionError:
        print("resolution error: recursion exceeded (likely cyclic packet dependency)")
        return 1

    out_path = write_snapshot(bundle)
    kernel_ordered = bundle.kernel
    chain = bundle.stateworks
    unique_guards = bundle.guard_pack.get("guards", [])
    snapshot = bundle.pinned
    print(f"guards:     {', '.join(g['id'] for g in unique_guards) or '(none)'} "
          f"(pack {bundle.guard_pack.get('version')}, "
          f"status={bundle.guard_pack.get('status')})")
    print(f"task:       {task_id}")
    print(f"profile:    {shape}")
    print(f"stateworks: {', '.join(s['id'] for s in chain) or '(none)'}")
    print(f"kernel:     {', '.join(kernel_ordered)}")
    print(f"bundle:     {out_path} ({bundle.instruction_words} instruction words)")
    sw_pins = ",".join(f"{s['id']}@{s['version']}" for s in snapshot.get("stateworks", [])) or "-"
    print(f"pinned:     framework={snapshot.get('framework_version')} "
          f"stateworks={sw_pins} guards={snapshot.get('guard_pack_version')}")
    print(f"policy:     {snapshot.get('policy_hash', 'n/a')}")
    print(f"frozen_at:  {snapshot.get('frozen_at')}")
    print(f"telemetry:  out-of-band (DP event plane; snapshot immutable)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

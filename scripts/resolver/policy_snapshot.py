"""Immutable CFW task policy snapshots and runtime bundle projection."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from runtime_contract.policy import freeze_contracts as _freeze_contracts
from runtime_contract.policy import stable_hash as _stable_contract_hash
from runtime_contract.task import TaskRequest, SystemOnePolicy
from runtime_contract.workflow_port import WorkflowResolution
from resolver.common import _frozen_mapping, _thaw, sha256_bytes
from resolver.guards import load_guard_pack, semantic_guard_hash

ROOT = Path(__file__).resolve().parents[2]

class SnapshotIntegrityError(ValueError):
    pass

@dataclass(frozen=True)
class PolicySnapshot:
    """Immutable pin of everything a task runs under. Normal task policy
    cannot mutate. A safety containment creates an EmergencyOverride record;
    the next task gets a new snapshot."""
    task_id: str
    kernel: Tuple[str, ...]
    stateworks: Tuple[StateWorkPin, ...]
    guard_pack_version: str
    guard_pack_hash: str
    framework_version: str
    framework_hash: str
    model: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    model_revision: Optional[str]
    model_capability_hash: Optional[str]
    harness: Optional[str]
    harness_version: Optional[str]
    toolset: Optional[str]
    namespace_id: str
    application_id: str
    application_version: Optional[str]
    application_instance_id: str
    frozen_at: str
    policy_hash: str = ""
    subject_ref: Optional[str] = None
    contracts: Mapping[str, Any] = field(default_factory=dict)
    goal_contract: Mapping[str, Any] = field(default_factory=dict)
    goal_contract_hash: str = ""
    system_one_policy: Mapping[str, Any] = field(default_factory=dict)
    phase: Optional[str] = None
    domain_tags: Tuple[str, ...] = ()
    task_shape: str = "implement"
    operation: Optional[str] = None
    trigger: Optional[str] = None
    execution_plan: Tuple[Dict[str, Any], ...] = ()
    agent_id: str = "agent"
    agent_instance_id: str = "agent"
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None
    parent_decision_ref: Optional[str] = None
    shared_evidence_refs: Tuple[str, ...] = ()
    delegation_topology: Optional[str] = None
    premise_digest: Optional[str] = None
    failure_attribution: str = "none"
    adopted_profile_hashes: Tuple[str, ...] = ()
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    shared_workflow_memberships: Tuple[Mapping[str, Any], ...] = ()
    statework_subject_refs: Mapping[str, str] = field(default_factory=dict)
    policy_revocation_revision: int = 0
    revocation_mode: str = "deferred"

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", _freeze_contracts(self.contracts))
        object.__setattr__(self, "goal_contract", _freeze_contracts(self.goal_contract))
        object.__setattr__(self, "system_one_policy", _freeze_contracts(self.system_one_policy))
        object.__setattr__(self, "shared_evidence_refs", tuple(self.shared_evidence_refs))
        object.__setattr__(self, "adopted_profile_hashes", tuple(self.adopted_profile_hashes))
        object.__setattr__(self, "shared_workflow_memberships", tuple(
            _freeze_contracts(item) for item in self.shared_workflow_memberships))
        object.__setattr__(self, "statework_subject_refs",
                           _freeze_contracts(dict(self.statework_subject_refs)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kernel": list(self.kernel),
            "stateworks": [pin.to_dict() for pin in self.stateworks],
            "guard_pack_version": self.guard_pack_version,
            "guard_pack_hash": self.guard_pack_hash,
            "framework_version": self.framework_version,
            "framework_hash": self.framework_hash,
            "model": self.model,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_revision": self.model_revision,
            "model_capability_hash": self.model_capability_hash,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "toolset": self.toolset,
            "namespace_id": self.namespace_id,
            "application_id": self.application_id,
            "application_version": self.application_version,
            "application_instance_id": self.application_instance_id,
            "frozen_at": self.frozen_at,
            "policy_hash": self.policy_hash,
            "subject_ref": self.subject_ref,
            "goal_contract": _thaw(self.goal_contract),
            "goal_contract_hash": self.goal_contract_hash,
            "system_one_policy": _thaw(self.system_one_policy),
            "agent_id": self.agent_id,
            "agent_instance_id": self.agent_instance_id,
            "attempt_id": self.attempt_id,
            "interaction_id": self.interaction_id,
            "parent_session_id": self.parent_session_id,
            "delegator_agent_id": self.delegator_agent_id,
            "delegate_agent_id": self.delegate_agent_id,
            "delegation_id": self.delegation_id,
            "role": self.role,
            "parent_decision_ref": self.parent_decision_ref,
            "shared_evidence_refs": list(self.shared_evidence_refs),
            "delegation_topology": self.delegation_topology,
            "premise_digest": self.premise_digest,
            "failure_attribution": self.failure_attribution,
            "adopted_profile_hashes": list(self.adopted_profile_hashes),
            "shared_workflow_membership": dict(self.shared_workflow_membership or {}),
            "shared_workflow_memberships": [_thaw(item)
                                            for item in self.shared_workflow_memberships],
            "statework_subject_refs": _thaw(self.statework_subject_refs),
            "policy_revocation_revision": self.policy_revocation_revision,
            "revocation_mode": self.revocation_mode,
            "phase": self.phase,
            "domain_tags": list(self.domain_tags),
            "task_shape": self.task_shape,
            "operation": self.operation,
            "trigger": self.trigger,
            "execution_plan": [_thaw(item) for item in self.execution_plan],
            "contracts": _thaw(self.contracts),
        }

@dataclass(frozen=True)
class EmergencyOverride:
    """Separate event: a safety containment changes the effective pack for
    the remainder of a task. It never mutates the PolicySnapshot; future
    tasks compile a fresh snapshot."""
    task_id: str
    override_reason: str
    rolled_back_to_guard_pack: str
    recorded_at: str

@dataclass(frozen=True)
class CapsulePin:
    owner: str
    stage: str
    version: str
    content_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner, "stage": self.stage,
                "version": self.version, "content_hash": self.content_hash}

@dataclass(frozen=True)
class StateWorkPin:
    id: str
    version: str
    domain: str
    purpose: str
    manifest_hash: str
    runtime_hash: str
    selected_flow: Optional[Tuple[str, str]]
    selected_specialists: Tuple[Tuple[str, str], ...]
    workflow_engine_kind: str = "state_machine"
    workflow_contract_version: str = ""
    workflow_contract_hash: str = ""
    flow_selection: Mapping[str, Any] = field(default_factory=dict)
    subject_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "domain": self.domain,
            "purpose": self.purpose,
            "manifest_hash": self.manifest_hash,
            "runtime_hash": self.runtime_hash,
            "selected_flow": (
                {"path": self.selected_flow[0], "content_hash": self.selected_flow[1]}
                if self.selected_flow else None
            ),
            "selected_specialists": [
                {"path": path, "content_hash": content_hash}
                for path, content_hash in self.selected_specialists
            ],
            "workflow_engine_kind": self.workflow_engine_kind,
            "workflow_contract_version": self.workflow_contract_version,
            "workflow_contract_hash": self.workflow_contract_hash,
            "flow_selection": _thaw(self.flow_selection),
            "subject_ref": self.subject_ref,
        }

@dataclass(frozen=True)
class GuardPin:
    guard_key: str
    rule_hash: str
    status_at_resolution: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guard_key": self.guard_key,
            "rule_hash": self.rule_hash,
            "status_at_resolution": self.status_at_resolution,
        }

@dataclass(frozen=True)
class EffectivePolicy:
    profile: str
    kernel: Tuple[str, ...]
    capsules: Tuple[CapsulePin, ...]
    stateworks: Tuple[StateWorkPin, ...]
    guards: Tuple[GuardPin, ...]
    model: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    model_revision: Optional[str]
    model_capability_hash: Optional[str]
    harness: Optional[str]
    harness_version: Optional[str]
    toolset: Optional[str]
    namespace_id: str
    application_id: str
    application_version: Optional[str]
    application_instance_id: str
    trigger: Optional[str]
    pipeline_hash: str
    runtime_kernel_hash: str
    calibration_hash: str
    system_one_policy: Mapping[str, Any] = field(default_factory=dict)
    subject_ref: Optional[str] = None
    goal_contract_hash: str = ""
    signal_schema_hash: str = ""
    signal_registry_hash: str = ""
    behavior_event_schema_hash: str = ""
    behavior_event_schema_version: str = ""
    runtime_behavior_hash: str = ""
    validator_registry_hash: str = ""
    handler_registry_hash: str = ""
    action_contract_hash: str = ""
    runtime_build_manifest_hash: str = ""
    routing_profile_hash: str = ""
    parent_decision_ref: Optional[str] = None
    shared_evidence_refs: Tuple[str, ...] = ()
    delegation_topology: Optional[str] = None
    premise_digest: Optional[str] = None
    failure_attribution: str = "none"
    adopted_profile_hashes: Tuple[str, ...] = ()
    shared_workflow_membership: Optional[Mapping[str, Any]] = None
    shared_workflow_memberships: Tuple[Mapping[str, Any], ...] = ()
    statework_subject_refs: Mapping[str, str] = field(default_factory=dict)
    policy_revocation_revision: int = 0
    revocation_mode: str = "deferred"
    policy_contract_version: str = "2.3.0"

    def canonical(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "kernel": list(self.kernel),
            "capsules": [pin.to_dict() for pin in self.capsules],
            "stateworks": [pin.to_dict() for pin in self.stateworks],
            "guards": [pin.to_dict() for pin in self.guards],
            "model": self.model,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_revision": self.model_revision,
            "model_capability_hash": self.model_capability_hash,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "toolset": self.toolset,
            "namespace_id": self.namespace_id,
            "application_id": self.application_id,
            "application_version": self.application_version,
            "application_instance_id": self.application_instance_id,
            "trigger": self.trigger,
            "pipeline_hash": self.pipeline_hash,
            "runtime_kernel_hash": self.runtime_kernel_hash,
            "calibration_hash": self.calibration_hash,
            "system_one_policy": _thaw(self.system_one_policy),
            "subject_ref": self.subject_ref,
            "goal_contract_hash": self.goal_contract_hash,
            "signal_schema_hash": self.signal_schema_hash,
            "signal_registry_hash": self.signal_registry_hash,
            "behavior_event_schema_hash": self.behavior_event_schema_hash,
            "behavior_event_schema_version": self.behavior_event_schema_version,
            "runtime_behavior_hash": self.runtime_behavior_hash,
            "validator_registry_hash": self.validator_registry_hash,
            "handler_registry_hash": self.handler_registry_hash,
            "action_contract_hash": self.action_contract_hash,
            "runtime_build_manifest_hash": self.runtime_build_manifest_hash,
            "routing_profile_hash": self.routing_profile_hash,
            "parent_decision_ref": self.parent_decision_ref,
            "shared_evidence_refs": list(self.shared_evidence_refs),
            "delegation_topology": self.delegation_topology,
            "premise_digest": self.premise_digest,
            "failure_attribution": self.failure_attribution,
            "adopted_profile_hashes": list(self.adopted_profile_hashes),
            "shared_workflow_membership": dict(self.shared_workflow_membership or {}),
            "shared_workflow_memberships": [_thaw(item)
                                            for item in self.shared_workflow_memberships],
            "statework_subject_refs": _thaw(self.statework_subject_refs),
            "policy_revocation_revision": self.policy_revocation_revision,
            "revocation_mode": self.revocation_mode,
            "policy_contract_version": self.policy_contract_version,
        }

def load_runtime_capsules() -> Dict[str, Dict[str, Any]]:
    """Load compact runtime capsules for core protocols (runtime.md), falling
    back to SKILL.md frontmatter description only when the capsule is
    absent. Used to build the effective injected instruction surface."""
    out: Dict[str, Dict[str, Any]] = {}
    for name in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS", "cogframe"]:
        skill_dir = ROOT / name
        capsule_path = skill_dir / "runtime.md"
        if (skill_dir / "SKILL.md").exists():
            out[name] = {"skill_words": len((skill_dir / "SKILL.md").read_text(encoding="utf-8").split())}
            if capsule_path.exists():
                out[name]["capsule_words"] = len(capsule_path.read_text(encoding="utf-8").split())
                out[name]["capsule"] = capsule_path.read_text(encoding="utf-8")
    return out

def compute_policy_hash(policy: EffectivePolicy) -> str:
    return _stable_contract_hash(policy.canonical())

def _policy_from_bundle(bundle: "RuntimeBundle") -> EffectivePolicy:
    expected = compute_policy_hash(bundle.policy)
    if bundle.pinned.get("policy_hash") != expected:
        raise SnapshotIntegrityError(
            f"runtime bundle policy hash mismatch: pinned={bundle.pinned.get('policy_hash')!r} "
            f"computed={expected!r}")

    capsules = tuple(
        CapsulePin(owner=stage.rsplit(":", 1)[-1].split("_")[0].upper(),
                   stage=stage, version=bundle.bundle_version,
                   content_hash=sha256_bytes(content.encode("utf-8")))
        for stage, (content, _trusted_hash) in sorted(bundle.injected_capsules)
    )
    guards = tuple(
        GuardPin(guard_key=guard.get("key") or guard["id"],
                 rule_hash=semantic_guard_hash(guard),
                 status_at_resolution=guard.get("status", "active"))
        for guard in bundle.guard_pack.get("guards", [])
    )
    stateworks = tuple(
        StateWorkPin(
            id=item["id"], version=item["version"],
            domain=str(item.get("domain") or ""),
            purpose=str(item.get("purpose") or ""),
            manifest_hash=item["manifest_hash"], runtime_hash=item["runtime_hash"],
            selected_flow=(tuple(item["selected_flow"])
                           if item.get("selected_flow") else None),
            selected_specialists=tuple(
                tuple(pair) for pair in item.get("selected_specialists", [])),
            workflow_engine_kind=str(item.get("workflow_engine_kind") or "state_machine"),
            workflow_contract_version=str(item.get("workflow_contract_version") or ""),
            workflow_contract_hash=str(item.get("workflow_contract_hash") or ""),
            flow_selection=item.get("flow_selection", {}),
            subject_ref=item.get("subject_ref"))
        for item in bundle.stateworks
    )
    if (capsules, guards, stateworks) != (bundle.policy.capsules,
                                          bundle.policy.guards,
                                          bundle.policy.stateworks):
        differences = []
        if capsules != bundle.policy.capsules:
            differences.append("capsules")
        if guards != bundle.policy.guards:
            differences.append("guards")
        if stateworks != bundle.policy.stateworks:
            differences.append("stateworks")
        raise SnapshotIntegrityError(
            "runtime bundle content differs from its pinned policy: " + ",".join(differences))
    return bundle.policy

@dataclass(frozen=True)
class RuntimeBundle:
    """Composed, immutable runtime policy for a single task."""
    bundle_version: str
    task_id: str
    profile: str
    kernel: Tuple[str, ...]
    kernel_instructions: Tuple[Dict[str, Any], ...]
    stateworks: Tuple[Dict[str, Any], ...]
    handoff_plan: Tuple[Dict[str, Any], ...]
    guard_pack: Dict[str, Any]
    pinned: Dict[str, Any]
    policy: EffectivePolicy
    capsule_hashes: Dict[str, str]
    injected_capsules: Tuple[Tuple[str, Tuple[str, str]], ...]
    instruction_words: int
    execution_plan: Tuple[Dict[str, Any], ...] = ()
    # Runtime-only source root.  It is intentionally not serialized into the
    # policy snapshot because absolute installation paths are not policy
    # identity; the executable COW hash is pinned in EffectivePolicy.
    statework_root: str = ""
    # Host-owned descriptors composed before resolution.  Runtime-only so
    # absolute/source-specific objects never become policy artifact fields.
    action_registry: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kernel", tuple(self.kernel))
        object.__setattr__(self, "kernel_instructions", tuple(self.kernel_instructions))
        object.__setattr__(self, "stateworks", tuple(self.stateworks))
        object.__setattr__(self, "handoff_plan", tuple(self.handoff_plan))
        object.__setattr__(self, "guard_pack", _frozen_mapping(self.guard_pack))
        object.__setattr__(self, "pinned", _frozen_mapping(self.pinned))
        object.__setattr__(self, "capsule_hashes", _frozen_mapping(self.capsule_hashes))
        object.__setattr__(self, "injected_capsules", tuple(
            (key, tuple(value)) for key, value in self.injected_capsules
        ))
        object.__setattr__(self, "execution_plan", tuple(self.execution_plan))
        object.__setattr__(self, "action_registry",
                           MappingProxyType(dict(self.action_registry)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_version": self.bundle_version,
            "task_id": self.task_id,
            "profile": self.profile,
            "kernel": list(self.kernel),
            "kernel_instructions": [_thaw(item) for item in self.kernel_instructions],
            "stateworks": [_thaw(item) for item in self.stateworks],
            "handoff_plan": [_thaw(item) for item in self.handoff_plan],
            "guard_pack": _thaw(self.guard_pack),
            "pinned": _thaw(self.pinned),
            "capsule_hashes": _thaw(self.capsule_hashes),
            "injected_capsules": [[stage, [text, content_hash]]
                                  for stage, (text, content_hash) in self.injected_capsules],
            "instruction_words": self.instruction_words,
            "execution_plan": [_thaw(item) for item in self.execution_plan],
        }

def injected_capsules_for(kernel: Iterable[str]) -> Dict[str, str]:
    """The compact runtime capsules actually injected for the active kernel
    stages. This is the effective instruction surface the model sees — not
    the authoring SKILL.md and not activation metadata."""
    out: Dict[str, str] = {}
    for stage_id in kernel:
        name = stage_id.split("_")[0].upper()
        if name == "SISPIS":
            path = ROOT / "SISPIS" / "runtime.md"
        else:
            path = ROOT / name / "runtime.md"
        if path.exists():
            out[stage_id] = path.read_text(encoding="utf-8")
    return out

"""CFW session lifecycle, component activation, and policy epochs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from runtime_contract.actions import ActionDescriptor
from runtime_contract.component_control import (
    COMPONENT_BASELINE_MODES, COMPONENT_IDS, ComponentOverlay,
    PreparedComponentOverlay,
)

def _canonical_system_one_status(reason: str, *, has_judgment: bool,
                                 mode: str) -> str:
    if reason in {"ok", "unavailable", "rejected", "stale", "shadow", "disabled"}:
        return reason
    if reason == "disabled":
        return "disabled"
    if reason in {"stale_request", "stale_response"}:
        return "stale"
    if reason in {"ineligible", "calibration_mismatch", "provider_contract_error"}:
        return "rejected"
    if reason in {"uncalibrated_shadow", "uncalibrated", "shadow_mode"} \
            or (has_judgment and mode == "shadow"):
        return "shadow"
    return "ok" if has_judgment else "unavailable"
@dataclass(frozen=True)
class ExecutionIdentity:
    """Immutable identity for one agent execution; never a workflow key."""
    namespace_id: str
    application_instance_id: str
    agent_instance_id: str
    session_id: str
    task_id: str
    attempt_id: str
    interaction_id: Optional[str]

    def __post_init__(self) -> None:
        for name in ("namespace_id", "application_instance_id", "agent_instance_id",
                     "session_id", "task_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError(f"execution identity field {name} must be a bounded identifier")
        if self.interaction_id is not None and (
                not isinstance(self.interaction_id, str) or not self.interaction_id
                or len(self.interaction_id) > 256):
            raise ValueError("execution identity interaction_id must be null or a bounded identifier")

    def storage_key(self) -> str:
        return "|".join((self.namespace_id, self.application_instance_id,
                         self.agent_instance_id, self.session_id, self.task_id,
                         self.attempt_id, self.interaction_id or ""))

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "namespace_id", "application_instance_id", "agent_instance_id",
            "session_id", "task_id", "attempt_id", "interaction_id")}
@dataclass(frozen=True)
class SharedWorkflowMembership:
    """Explicit authorization to use a shared CSW workflow subject."""
    namespace_id: str
    application_id: str
    application_instance_id: str
    workflow_id: str
    statework_id: str
    subject_ref: str
    authorization_revision: int

    def state_identity(self) -> dict[str, Any]:
        if self.authorization_revision < 1:
            raise ValueError("shared workflow membership requires a positive authorization revision")
        return {
            "namespace_id": self.namespace_id, "application_id": self.application_id,
            "application_instance_id": self.application_instance_id,
            "workflow_id": self.workflow_id, "statework_id": self.statework_id,
            "subject_ref": self.subject_ref,
            "authorization_revision": self.authorization_revision,
        }
class GateDecision(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REQUIRE_REINSPECTION = "require_reinspection"
    REQUIRE_RECOVERY = "require_recovery"
    REQUIRE_CHANGED_HYPOTHESIS = "require_changed_hypothesis"
    REQUIRE_GOAL_REANCHOR = "require_goal_reanchor"
@dataclass(frozen=True)
class ActionPreflightResult:
    """Side-effect-light CFW hard-gate result for a proposed host action."""

    action_digest: str
    decision: GateDecision
    authority: str = "cfw_action_gate"
    reason_codes: tuple[str, ...] = ()
@dataclass(frozen=True)
class ActionPreparation:
    """One host authorization result and its unique request correlation ID."""

    action_request_id: str
    decision: GateDecision
    descriptor: ActionDescriptor
    action_digest: str = ""
    authority: str = "cfw_action_gate"
    reason_codes: tuple[str, ...] = ()
class LifecycleController:
    """Execute pipeline activation metadata at its declared lifecycle point."""

    def __init__(self, bundle: resolve_runtime.RuntimeBundle) -> None:
        self.bundle = bundle
        self.specs = {item["stage"]: item for item in bundle.kernel_instructions}
        # Each declared frequency has a matching storage lifetime. In
        # particular, temporary wrappers never enter task_active.
        self.task_active: set[str] = set()
        self.segment_active: set[str] = set()
        self.action_active: set[str] = set()
        self.artifact_active: set[str] = set()
        self.response_active: set[str] = set()
        self.counts: Dict[str, int] = {}
        self.action_index = 0
        self._component_overlay: Optional[ComponentOverlay] = None
        self._startup_active: set[str] = set()

    @property
    def active(self) -> set[str]:
        """Everything currently visible to the model in this segment."""
        active = (self.task_active | self.segment_active | self.action_active
                  | self.artifact_active | self.response_active)
        if self._component_overlay is None:
            return active
        return {stage for stage in active if self._stage_permitted(stage)}

    def component_mode(self, component_id: str) -> Optional[str]:
        if self._component_overlay is None:
            return None
        control = self._component_overlay.by_component().get(component_id)
        if control is None:
            return None
        leased_value = self._component_overlay.effective_target_value(
            "framework:" + component_id, self.action_index)
        if leased_value is not None:
            return leased_value
        if (control.expires_at_action is not None
                and self.action_index >= control.expires_at_action):
            return COMPONENT_BASELINE_MODES[component_id]
        return control.mode

    def effective_behavior_target_values(self) -> Dict[str, str]:
        """Return effective target-scoped DP lease values at the local boundary."""
        if self._component_overlay is None:
            return {}
        targets = {item.target for item in self._component_overlay.target_leases}
        return {target: value for target in targets
                if (value := self._component_overlay.effective_target_value(
                    target, self.action_index)) is not None}

    def expire_behavior_leases(self, action_index: int) -> bool:
        """Advance target-scoped lease time without clearing unrelated controls."""
        prior = self.action_index
        self.synchronize_action_index(action_index)
        return bool(self._component_overlay is not None and any(
            prior <= lease.expires_at_action < self.action_index
            for lease in self._component_overlay.target_leases))

    def synchronize_action_index(self, action_index: int) -> None:
        """Advance to a host/DP action basis without rewinding local progress."""
        if type(action_index) is not int or action_index < 0:
            raise ValueError("CFW action index must be a non-negative integer")
        self.action_index = max(self.action_index, action_index)

    def _stage_permitted(self, stage: str) -> bool:
        overlay = self._component_overlay
        if overlay is None:
            return True
        owner = stage.split("_", 1)[0].lower()
        mode = self.component_mode(owner)
        if mode is None:
            return owner not in COMPONENT_IDS
        if owner == "ward" or self._required_by_hard_floor(stage):
            return True
        if mode == "off":
            return False
        if mode == "passive":
            return stage in self._startup_active
        return True

    def _required_by_hard_floor(self, stage: str) -> bool:
        pending = [candidate for candidate in self.specs
                   if candidate.split("_", 1)[0].lower() == "ward"]
        visited: set[str] = set()
        while pending:
            candidate = pending.pop()
            if candidate in visited:
                continue
            visited.add(candidate)
            for dependency in self.specs.get(candidate, {}).get("requires") or ():
                if dependency == stage:
                    return True
                pending.append(dependency)
        return False

    def prepare_component_overlay(self, overlay: ComponentOverlay, *,
                                  allow_rollback: bool = False
                                  ) -> PreparedComponentOverlay:
        if not isinstance(overlay, ComponentOverlay):
            raise TypeError("CFW requires a validated ComponentOverlay")
        prior = self._component_overlay
        current_revision = prior.behavior_revision if prior is not None else -1
        if not allow_rollback and overlay.behavior_revision < current_revision:
            raise ValueError("CFW rejects a stale component-overlay revision")
        if (not allow_rollback and overlay.behavior_revision == current_revision
                and prior is not None and overlay != prior):
            raise ValueError("same component-overlay revision cannot carry different controls")
        return PreparedComponentOverlay(
            overlay=overlay,
            expected_current_revision=current_revision,
            previous_overlay=prior,
            previous_lifecycle_state=self.capture_state(),
        )

    def apply_component_overlay(self, prepared: PreparedComponentOverlay, *,
                                startup_active: set[str],
                                allow_rollback: bool = False) -> set[str]:
        """Apply component gates to CFW's event-driven lifecycle.

        DP owns each component mode. CFW still owns the registered lifecycle
        events that execute a component. The overlay can suppress stages,
        request bounded component resets, and elevate FLOW postflight runs.
        """
        if not isinstance(prepared, PreparedComponentOverlay):
            raise TypeError("CFW component application requires a prepared overlay")
        prior_revision = (self._component_overlay.behavior_revision
                          if self._component_overlay is not None else -1)
        if prepared.expected_current_revision != prior_revision:
            raise RuntimeError("CFW component overlay changed after preparation")
        if prepared.previous_overlay != self._component_overlay:
            raise RuntimeError("CFW component overlay no longer matches its prepared basis")
        if (not allow_rollback
                and prepared.overlay.behavior_revision < prior_revision):
            raise ValueError("CFW rejects a stale component-overlay revision")
        if (not allow_rollback and prior_revision >= 0
                and prepared.overlay.behavior_revision == prior_revision
                and prepared.overlay != self._component_overlay):
            raise ValueError("same component-overlay revision cannot carry different controls")
        prior = self._component_overlay
        self._startup_active = set(startup_active)
        prior_modes = {key: item.mode for key, item in (prior.by_component().items()
                                                        if prior is not None else ())}
        self._component_overlay = prepared.overlay
        modes = {key: item.mode for key, item in prepared.overlay.by_component().items()}
        for component_id, mode in modes.items():
            if mode not in {"reset", "reopen", "recovery"}:
                continue
            if prior_modes.get(component_id) in {"reset", "reopen", "recovery"}:
                continue
            self._reset_component(component_id, mode)
        return self.active

    def rollback_component_overlay(self, prepared: PreparedComponentOverlay, *,
                                   expected_current_revision: int) -> set[str]:
        if not isinstance(prepared, PreparedComponentOverlay):
            raise TypeError("CFW rollback requires a prepared component overlay")
        current_revision = (self._component_overlay.behavior_revision
                            if self._component_overlay is not None else -1)
        if current_revision != expected_current_revision:
            raise RuntimeError("CFW component rollback revision changed; re-inspect required")
        if expected_current_revision != prepared.overlay.behavior_revision:
            raise ValueError("CFW rollback guard does not match the prepared revision")
        if self._component_overlay != prepared.overlay:
            raise RuntimeError("CFW active overlay differs from the prepared transition")
        self.restore_state(prepared.previous_lifecycle_state)
        self._component_overlay = prepared.previous_overlay
        return self.active

    def synchronize_component_overlay(self, overlay: ComponentOverlay, *,
                                      startup_active: set[str],
                                      prepared: Optional[PreparedComponentOverlay] = None
                                      ) -> set[str]:
        """Reconstruct the actuator state from DP's persisted behavior state."""
        prepared = prepared or self.prepare_component_overlay(overlay)
        if prepared.overlay != overlay:
            raise ValueError("CFW synchronization payload differs from its prepared overlay")
        return self.apply_component_overlay(prepared, startup_active=startup_active)

    def capture_state(self) -> dict[str, Any]:
        return {"task_active": set(self.task_active),
                "segment_active": set(self.segment_active),
                "action_active": set(self.action_active),
                "artifact_active": set(self.artifact_active),
                "response_active": set(self.response_active),
                "counts": dict(self.counts),
                "startup_active": set(self._startup_active)}

    def restore_state(self, value: Mapping[str, Any]) -> set[str]:
        expected = {"task_active", "segment_active", "action_active",
                    "artifact_active", "response_active", "counts",
                    "startup_active"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("CFW lifecycle snapshot is malformed")
        for key in ("task_active", "segment_active", "action_active",
                    "artifact_active", "response_active", "startup_active"):
            stages = value[key]
            if not isinstance(stages, set) or not stages.issubset(self.specs):
                raise ValueError("CFW lifecycle snapshot contains an unknown stage")
        counts = value["counts"]
        if (not isinstance(counts, Mapping)
                or any(stage not in self.specs or type(count) is not int or count < 0
                       for stage, count in counts.items())):
            raise ValueError("CFW lifecycle snapshot counts are malformed")
        self._component_overlay = None
        self.task_active = set(value["task_active"])
        self.segment_active = set(value["segment_active"])
        self.action_active = set(value["action_active"])
        self.artifact_active = set(value["artifact_active"])
        self.response_active = set(value["response_active"])
        self.counts = dict(counts)
        self._startup_active = set(value["startup_active"])
        return self.active

    def _reset_component(self, component_id: str, mode: str) -> None:
        stages = [stage for stage in self.specs
                  if stage.split("_", 1)[0].lower() == component_id]
        for stage in stages:
            self.task_active.discard(stage)
            self.segment_active.discard(stage)
            self.action_active.discard(stage)
            self.artifact_active.discard(stage)
            self.response_active.discard(stage)
            self.counts.pop(stage, None)
        stage = {
            "owl": "owl",
            "anchor": "anchor_open",
            "dox": "dox_load",
        }.get(component_id)
        if stage in self.specs and self._stage_permitted(stage):
            self.activate(stage, f"dp_component_{mode}")

    def activate(self, stage: str, reason: str) -> None:
        spec = self.specs.get(stage)
        if spec is None or not self._stage_permitted(stage):
            return
        for dependency in spec.get("requires") or ():
            if dependency not in self.specs or not self._stage_permitted(dependency):
                return
            if dependency not in self.active:
                self.activate(dependency, f"required_by:{stage}")
        frequency = spec.get("frequency")
        if frequency == "per_task" and self.counts.get(stage, 0):
            return
        target = {
            "per_task": self.task_active,
            "per_action": self.action_active,
            "per_artifact": self.artifact_active,
            "per_edit": self.artifact_active,
            "per_response": self.response_active,
        }.get(frequency, self.segment_active)
        target.add(stage)
        self.counts[stage] = self.counts.get(stage, 0) + 1

    @staticmethod
    def _predicate_matches(predicate: str, context: Mapping[str, Any]) -> bool:
        if predicate == "action.any":
            return True
        if predicate == "action.mutating":
            return context.get("mutability") not in {None, "read"}
        if predicate == "action.external_effect":
            return bool(context.get("network_effect"))
        if predicate == "action.secret_access":
            return bool(context.get("secret_access"))
        if predicate == "action.high_blast_radius":
            return context.get("blast_radius") in {"system", "organization", "global"}
        if predicate == "action.untrusted":
            return bool(context.get("untrusted"))
        if predicate == "task.nontrivial":
            return context.get("task_complexity") in {"standard", "complex"}
        if predicate == "artifact.flow_triggered":
            return context.get("flow_triggered") is True
        if predicate == "artifact.high_change_amplification":
            score = context.get("change_amplification_score", 0.0)
            cross_boundary = context.get("cross_boundary", False)
            shared_contract = context.get("shared_contract", False)
            responsibility_expansion = context.get("responsibility_expansion", False)
            owners = context.get("modified_owner_count", 0)
            fanout = context.get("fanout", 0)
            return (score >= 0.7
                    or (cross_boundary and owners >= 2 and fanout >= 3)
                    or (shared_contract and owners >= 2 and fanout >= 4)
                    or (responsibility_expansion and fanout >= 3))
        if predicate.endswith(".any"):
            return True
        return False

    def activate_predicates(self, stage: str, event: str,
                            context: Mapping[str, Any]) -> None:
        spec = self.specs.get(stage, {})
        predicates = spec.get("activation_predicates") or []
        if any(item.get("event") == event and self._predicate_matches(
                str(item.get("predicate")), context) for item in predicates):
            self.activate(stage, f"predicate:{event}")

    def on_task_start(self, context: Optional[Mapping[str, Any]] = None) -> None:
        """Dispatch declared activation, not an implicit execution mode.

        ``execution_mode`` describes how a stage behaves once active. Whether
        an event activates it is declared separately by ``activation_predicates``
        (or by the preflight convention). Conflating the two made
        ``anchor_open`` — declared ``state`` + ``task_start`` — never activate.
        """
        payload = dict(context or {})
        for stage, spec in self.specs.items():
            predicates = spec.get("activation_predicates") or []
            if predicates:
                self.activate_predicates(stage, "task_start", payload)
            elif spec.get("execution_mode") == "preflight":
                self.activate(stage, "task_start")

    def reset_for_segment(self) -> None:
        """Start a fresh model subcall without re-running task activation.

        Re-injecting context into a new segment is not re-executing the task.
        Task-scoped activation and counts survive segment boundaries; only
        per-action/per-response visibility is rebuilt.
        """
        # Reinjection is not re-execution: task-scoped activation persists;
        # ephemeral stages belong only to their current boundary.
        self.segment_active = {stage for stage in self.task_active
                              if self.specs.get(stage, {}).get("frequency") == "per_task"}
        self.action_active.clear()
        self.artifact_active.clear()
        self.response_active.clear()

    def on_model_call(self, *, tool_capable: bool = False,
                      action_hint: Optional[ActionDescriptor] = None,
                      task_complexity: Optional[str] = None) -> None:
        """Boundary before model generation.

        Model capability is not action intent. Explicit host dispatch through
        on_tool_planning activates action wrappers when a tool decision begins.
        """
        self.on_task_start({"task_complexity": task_complexity})
        if self.component_mode("owl") == "elevated":
            self._reset_component("owl", "elevated")

    def on_tool_planning(self, descriptor: Optional[ActionDescriptor] = None) -> None:
        """Expose temporary action guidance only at an explicit planning boundary."""
        self.action_active.clear()
        self.activate_predicates("fuse", "action", {
            "mutability": "read", "action_planning": True})
        if descriptor is not None:
            self.activate_predicates("ward", "action", descriptor.__dict__)


    def on_action(self, decision: GateDecision,
                  descriptor: Optional[ActionDescriptor] = None) -> None:
        context = descriptor.__dict__ if descriptor is not None else {}
        self.activate_predicates("fuse", "action", context)
        self.activate_predicates("ward", "action", context)
        if decision != GateDecision.ALLOW:
            self.activate("ward", "action_gate")

    def on_action_complete(self) -> None:
        self.action_index += 1
        self.action_active.clear()

    @staticmethod
    def _artifact_impact_context(impact_facts: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        if impact_facts is None:
            return {}
        allowed = {
            "cross_boundary", "shared_contract", "responsibility_expansion",
            "modified_owner_count", "fanout", "change_amplification_score",
        }
        if not isinstance(impact_facts, Mapping) or set(impact_facts) - allowed:
            raise ValueError("artifact impact facts are malformed")
        context = dict(impact_facts)
        for name in ("cross_boundary", "shared_contract", "responsibility_expansion"):
            if name in context and type(context[name]) is not bool:
                raise ValueError(f"artifact impact fact {name} must be boolean")
        for name in ("modified_owner_count", "fanout"):
            value = context.get(name, 0)
            if type(value) is not int or not 0 <= value <= 256:
                raise ValueError(f"artifact impact fact {name} is out of bounds")
            context[name] = value
        score = context.get("change_amplification_score", 0.0)
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not 0.0 <= float(score) <= 1.0):
            raise ValueError("artifact change amplification score must be between zero and one")
        context["change_amplification_score"] = float(score)
        for name in ("cross_boundary", "shared_contract", "responsibility_expansion"):
            context.setdefault(name, False)
        return context

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True,
                    impact_facts: Optional[Mapping[str, Any]] = None) -> None:
        self.artifact_active.clear()
        impact_context = self._artifact_impact_context(impact_facts)
        if self.component_mode("flow") == "elevated":
            self.activate("flow", "artifact_trigger")
        else:
            self.activate_predicates("flow", "artifact", {
                **impact_context, "flow_triggered": flow_triggered})
        if (durable_contract_changed
                or self.component_mode("dox") == "elevated"):
            self.activate("dox_closeout", "durable_contract")
        if state_changed or self.component_mode("anchor") == "elevated":
            self.activate("anchor_closeout", "artifact_state")

    def on_artifact_processed(self) -> None:
        self.artifact_active.clear()

    def on_completion(self) -> None:
        self.response_active.clear()
        if (self._component_overlay is not None
                and self._component_overlay.behavior_mode == "READY_TO_COMPLETE"):
            self.activate("anchor_closeout", "ready_to_complete")
        self.activate("sispis", "completion")

    def on_response_complete(self) -> None:
        self.response_active.clear()

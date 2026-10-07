"""Task, segment, response, and telemetry lifecycle methods for CFW."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from runtime_contract.operator_events import OperatorEvent
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_session.host_authority import HostIngress
from runtime_session.projection import context_for
from runtime_session.telemetry import HostTelemetry


class SessionLifecycleMethods:
    """Session lifecycle hooks and model context refresh operations."""
    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None,
                          behavior_context: str = "",
                          active_statework_ids: Optional[Sequence[str]] = None) -> str:
        self.require_external_behavior_projection("model boundary")
        if self.lifecycle_workflow.lifecycle is not None:
            prior = set(self.lifecycle_workflow.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            complexity = (self.bundle.pinned.get("goal_contract") or {}).get(
                "task_complexity", "standard" if self.bundle.profile != "quick" else "trivial")
            self.lifecycle_workflow.lifecycle.on_model_call(tool_capable=tool_capable,
                                         action_hint=descriptor,
                                         task_complexity=complexity)
            self.lifecycle_workflow.active_lifecycle = set(self.lifecycle_workflow.lifecycle.active)
            self.lifecycle_workflow.lifecycle_counts = dict(self.lifecycle_workflow.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self, behavior_context=behavior_context,
                           active_statework_ids=active_statework_ids)
    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Optional[Sequence[str]] = None) -> str:
        self.require_external_behavior_projection("planning boundary")
        if self.lifecycle_workflow.lifecycle is not None:
            prior = set(self.lifecycle_workflow.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            self.lifecycle_workflow.lifecycle.on_tool_planning(descriptor)
            self.lifecycle_workflow.active_lifecycle = set(self.lifecycle_workflow.lifecycle.active)
            self.lifecycle_workflow.lifecycle_counts = dict(self.lifecycle_workflow.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self, behavior_context=behavior_context,
                           active_statework_ids=active_statework_ids)
    def artifact_processed(self) -> None:
        if self.lifecycle_workflow.lifecycle is not None:
            self.lifecycle_workflow.lifecycle.on_artifact_processed()
            self.lifecycle_workflow.active_lifecycle = set(self.lifecycle_workflow.lifecycle.active)
            self.lifecycle_workflow.lifecycle_counts = dict(self.lifecycle_workflow.lifecycle.counts)
    def _emit_lifecycle_activations(self, prior: set[str]) -> None:
        for stage in sorted(self.lifecycle_workflow.active_lifecycle - prior):
            component = stage.split("_", 1)[0].lower()
            recovery_epoch_ids = sorted({
                str(item["recovery_epoch_id"])
                for item in (self.behavior.external_projection.get("target_leases") or ())
                if isinstance(item, Mapping)
                and item.get("target") == f"framework:{component}"
                and isinstance(item.get("recovery_epoch_id"), str)
            })
            payload = {"summary": stage, "route": stage,
                       "route_type": "lifecycle_stage",
                       "reason": "registered_predicate"}
            if recovery_epoch_ids:
                payload["recovery_epoch_ids"] = recovery_epoch_ids[:16]
            self._emit_runtime({
                "category": "decision", "event_type": "lifecycle_stage_activated",
                "payload": payload})
    @property
    def host(self) -> HostIngress:
        raise PermissionError(
            "host ingress is not exposed on the agent session; use the host adapter")
    def advance_segment(self, segment_index: Optional[int] = None) -> ExecutionSegment:
        """Start a new immutable subcall context; prior prompt text is not mutated."""
        target = self.lifecycle_workflow.current_segment_index + 1 if segment_index is None else segment_index
        if target < 0 or target >= len(self.lifecycle_workflow.execution_segments):
            raise ValueError(f"unknown execution segment {target}")
        if target < self.lifecycle_workflow.current_segment_index:
            raise ValueError("execution segments are forward-only")
        current_id = self.lifecycle_workflow.current_segment.segment_id
        if self.lifecycle_workflow.statuses.get(current_id) != SegmentStatus.COMPLETED:
            self.lifecycle_workflow.set_segment_status(current_id, SegmentStatus.BLOCKED)
            raise PermissionError(
                f"segment {current_id} cannot advance before its host-attested output is complete")
        next_id = self.lifecycle_workflow.execution_segments[target].segment_id
        if target != self.lifecycle_workflow.current_segment_index + 1:
            raise PermissionError("execution segments must advance sequentially")
        self.lifecycle_workflow.current_segment_index = target
        self.lifecycle_workflow.set_segment_status(next_id, SegmentStatus.ACTIVE)
        self._journal("segment_activated", segment_id=next_id,
                      segment_index=target, statework_id=self.lifecycle_workflow.current_segment.statework_id)
        statework_id = self.lifecycle_workflow.current_segment.statework_id
        self.lifecycle_workflow.active_statework_ids = {statework_id} if statework_id else set()
        if self.lifecycle_workflow.lifecycle is not None:
            self.lifecycle_workflow.lifecycle.reset_for_segment()
            self.lifecycle_workflow.active_lifecycle = set(self.lifecycle_workflow.lifecycle.active)
            self.lifecycle_workflow.lifecycle_counts = dict(self.lifecycle_workflow.lifecycle.counts)
        self._emit_runtime({"category": "observation", "event_type": "execution_segment_started",
                   "subject": self.lifecycle_workflow.current_segment.subject_ref,
                   "payload": {"summary": self.lifecycle_workflow.current_segment.segment_id,
                               "result_class": "active"}})
        return self.lifecycle_workflow.current_segment
    def emit(self, event: Dict[str, Any]) -> None:
        """Raw telemetry is not an agent capability."""
        raise PermissionError("raw telemetry emission is host-owned")
    def _emit_runtime(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("agent_id", self.bundle.pinned.get("agent_id", "agent"))
        for key in ("namespace_id", "application_id", "application_instance_id",
                    "application_version", "provider_id", "model_id", "model_revision",
                    "model_capability_hash", "harness_id", "harness_version"):
            if key in self.bundle.pinned and self.bundle.pinned.get(key) is not None:
                event.setdefault(key, self.bundle.pinned[key])
        event.setdefault("goal_contract_hash",
                         self.bundle.pinned.get("goal_contract_hash") or None)
        event.setdefault("agent_instance_id", self.identity_authority.identity.agent_instance_id)
        event.setdefault("session_id", self.identity_authority.identity.session_id)
        event.setdefault("attempt_id", self.identity_authority.identity.attempt_id)
        event.setdefault("segment_id", self.lifecycle_workflow.current_segment.segment_id)
        event.setdefault("behavioral_subject", event.get("subject") or
                         self.bundle.policy.subject_ref or f"agent:{self.identity_authority.identity.agent_instance_id}")
        for key in ("interaction_id", "parent_session_id", "delegator_agent_id",
                    "delegate_agent_id", "delegation_id", "role"):
            value = getattr(self.identity_authority.identity, key)
            if value is not None:
                event.setdefault(key, value)
        if event.get("actor_id") in {"operator", "repository_owner", "service_owner"}:
            raise PermissionError("operator identity is host-owned")
        HostTelemetry.validate_event(
            event, task_id=self.bundle.task_id,
            expected_schema_hash=self.bundle.policy.runtime_event_schema_hash,
            expected_schema_version=self.bundle.policy.runtime_event_schema_version)
        if self.identity_authority.authorities.operator_read_model is not None:
            try:
                sequence = self.telemetry_state.allocate_operator_sequence(
                    self.identity_authority.authorities.operator_read_model)
                self.identity_authority.authorities.operator_read_model.publish(OperatorEvent.create(
                    sequence=sequence,
                    namespace_id=str(event.get("namespace_id") or "default"),
                    application_instance_id=str(event.get("application_instance_id") or "unknown"),
                    agent_instance_id=event.get("agent_instance_id"),
                    session_id=event.get("session_id"), task_id=self.bundle.task_id,
                    interaction_id=event.get("interaction_id"),
                    workflow_id=(self.identity_authority.authorities.shared_workflow_membership or {}).get("workflow_id"),
                    event_type=str(event.get("event_type") or "runtime_event"),
                    severity=("warning" if "block" in str(event.get("event_type") or "") else "info"),
                    summary=str((event.get("payload") or {}).get("summary") or event.get("event_type") or "Runtime event")[:512],
                    reason_code=(event.get("payload") or {}).get("result_class"),
                    source_system="cfw", provenance="host_verified",
                    correlation_id=f"{self.identity_authority.identity.session_id}:{self.bundle.task_id}",
                    evidence_refs=tuple(event.get("evidence_refs") or ())))
            except Exception:
                pass
        if self.telemetry_state.emit:
            self.telemetry_state.emit(event)
        if self.telemetry_state.exporter is not None:
            self.telemetry_state.exporter.enqueue()
    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True,
                    impact_facts: Optional[Mapping[str, Any]] = None) -> None:
        if self.lifecycle_workflow.lifecycle is not None:
            self.lifecycle_workflow.lifecycle.on_artifact(
                flow_triggered=flow_triggered,
                durable_contract_changed=durable_contract_changed,
                state_changed=state_changed, impact_facts=impact_facts)
            self.lifecycle_workflow.active_lifecycle = set(self.lifecycle_workflow.lifecycle.active)
            self.lifecycle_workflow.lifecycle_counts = dict(self.lifecycle_workflow.lifecycle.counts)

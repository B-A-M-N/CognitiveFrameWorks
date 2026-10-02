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
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            complexity = (self.bundle.pinned.get("goal_contract") or {}).get(
                "task_complexity", "standard" if self.bundle.profile != "quick" else "trivial")
            self.lifecycle.on_model_call(tool_capable=tool_capable,
                                         action_hint=descriptor,
                                         task_complexity=complexity)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self, behavior_context=behavior_context,
                           active_statework_ids=active_statework_ids)
    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Optional[Sequence[str]] = None) -> str:
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            descriptor = self._descriptor(action_hint) if action_hint is not None else None
            self.lifecycle.on_tool_planning(descriptor)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        return context_for(self, behavior_context=behavior_context,
                           active_statework_ids=active_statework_ids)
    def artifact_processed(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.on_artifact_processed()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
    def _emit_lifecycle_activations(self, prior: set[str]) -> None:
        for stage in sorted(self.active_lifecycle - prior):
            self._emit_runtime({
                "category": "decision", "event_type": "lifecycle_stage_activated",
                "payload": {"summary": stage, "route": stage,
                            "route_type": "lifecycle_stage",
                            "reason": "registered_predicate"}})
    @property
    def host(self) -> HostIngress:
        raise PermissionError(
            "host ingress is not exposed on the agent session; use the host adapter")
    @property
    def current_segment(self) -> ExecutionSegment:
        return self.execution_segments[self.current_segment_index]
    def advance_segment(self, segment_index: Optional[int] = None) -> ExecutionSegment:
        """Start a new immutable subcall context; prior prompt text is not mutated."""
        target = self.current_segment_index + 1 if segment_index is None else segment_index
        if target < 0 or target >= len(self.execution_segments):
            raise ValueError(f"unknown execution segment {target}")
        if target < self.current_segment_index:
            raise ValueError("execution segments are forward-only")
        current_id = self.current_segment.segment_id
        if self._segment_status.get(current_id) != SegmentStatus.COMPLETED:
            self._segment_status[current_id] = SegmentStatus.BLOCKED
            raise PermissionError(
                f"segment {current_id} cannot advance before its host-attested output is complete")
        next_id = self.execution_segments[target].segment_id
        if target != self.current_segment_index + 1:
            raise PermissionError("execution segments must advance sequentially")
        self.current_segment_index = target
        self._segment_status[next_id] = SegmentStatus.ACTIVE
        self._journal("segment_activated", segment_id=next_id,
                      segment_index=target, statework_id=self.current_segment.statework_id)
        statework_id = self.current_segment.statework_id
        self.active_statework_ids = {statework_id} if statework_id else set()
        if self.lifecycle is not None:
            self.lifecycle.reset_for_segment()
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
        self._emit_runtime({"category": "observation", "event_type": "execution_segment_started",
                   "subject": self.current_segment.subject_ref,
                   "payload": {"summary": self.current_segment.segment_id,
                               "result_class": "active"}})
        return self.current_segment
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
        event.setdefault("agent_instance_id", self.agent_instance_id)
        event.setdefault("session_id", self.session_id)
        event.setdefault("attempt_id", self.attempt_id)
        event.setdefault("segment_id", self.current_segment.segment_id)
        event.setdefault("behavioral_subject", event.get("subject") or
                         self.bundle.policy.subject_ref or f"agent:{self.agent_instance_id}")
        for key in ("interaction_id", "parent_session_id", "delegator_agent_id",
                    "delegate_agent_id", "delegation_id", "role"):
            if getattr(self, key) is not None:
                event.setdefault(key, getattr(self, key))
        if event.get("actor_id") in {"operator", "repository_owner", "service_owner"}:
            raise PermissionError("operator identity is host-owned")
        HostTelemetry.validate_event(
            event, task_id=self.bundle.task_id,
            expected_schema_hash=self.bundle.policy.behavior_event_schema_hash,
            expected_schema_version=self.bundle.policy.behavior_event_schema_version)
        if self.operator_read_model is not None:
            try:
                self._operator_sequence = self.operator_read_model.allocate_sequence()
                self.operator_read_model.publish(OperatorEvent.create(
                    sequence=self._operator_sequence,
                    namespace_id=str(event.get("namespace_id") or "default"),
                    application_instance_id=str(event.get("application_instance_id") or "unknown"),
                    agent_instance_id=event.get("agent_instance_id"),
                    session_id=event.get("session_id"), task_id=self.bundle.task_id,
                    interaction_id=event.get("interaction_id"),
                    workflow_id=(self.shared_workflow_membership or {}).get("workflow_id"),
                    event_type=str(event.get("event_type") or "runtime_event"),
                    severity=("warning" if "block" in str(event.get("event_type") or "") else "info"),
                    summary=str((event.get("payload") or {}).get("summary") or event.get("event_type") or "Runtime event")[:512],
                    reason_code=(event.get("payload") or {}).get("result_class"),
                    source_system="cfw", provenance="host_verified",
                    correlation_id=f"{self.session_id}:{self.bundle.task_id}",
                    evidence_refs=tuple(event.get("evidence_refs") or ())))
            except Exception:
                pass
        if self._emit:
            self._emit(event)
        if self._telemetry_exporter is not None:
            self._telemetry_exporter.enqueue()
    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        if self.lifecycle is not None:
            self.lifecycle.on_artifact(
                flow_triggered=flow_triggered,
                durable_contract_changed=durable_contract_changed,
                state_changed=state_changed)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)

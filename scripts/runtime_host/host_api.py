"""Host-facing Cognitive Runtime API.

CFW remains authoritative for task-local action gates and evidence-bound
enforcement. DigitalPsychology can coordinate behavioral changes through the
separate ``digital_psychology_adapter`` capability. The host process and its
protected same-user runtime directory are the trusted computing base;
same-process Python is not an untrusted security boundary. Untrusted plugins
must use an external IPC/RPC gateway as described in ``SECURITY.md``.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from runtime import (CompletionClaim, GateDecision, RuntimeSession,
                     SharedWorkflowMembershipAuthority, start)
from runtime_contract.actions import (ActionDescriptor, HostExecutionContext,
                                       effective_action_registry)
from runtime_contract.advisor import BehavioralAdvisor
from runtime_contract.workflow_port import WorkflowPort, validate_workflow_port
from resolve_runtime import RuntimeBundle, TaskRequest, resolve
from runtime_host.session_registry import HOST_SESSION_REGISTRY as _HOST_SESSION_REGISTRY
from runtime_host.handles import AgentSessionHandle, HostSessionHandle, RuntimeTaskHandle


def _register_host_session(session: RuntimeSession) -> str:
    return _HOST_SESSION_REGISTRY.register(session)


def _resolve_host_session(token: str) -> RuntimeSession:
    return _HOST_SESSION_REGISTRY.resolve(token)


class CognitiveRuntime:
    def __init__(self, *, telemetry_disabled: bool = False,
                 host_context: Optional[HostExecutionContext] = None,
                 action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
                 routing_profile_path: Optional[str] = None,
                 behavioral_advisor: Optional[BehavioralAdvisor] = None,
                 sharing_verifier: Optional[Any] = None,
                 membership_authority: Optional[SharedWorkflowMembershipAuthority] = None,
                 operator_read_model: Optional[Any] = None,
                 workflow_port: Optional[WorkflowPort] = None) -> None:
        self.telemetry_disabled = telemetry_disabled
        self.host_context = host_context
        self.action_registry = effective_action_registry(action_registry)
        self.routing_profile_path = routing_profile_path
        self.behavioral_advisor = behavioral_advisor
        self.workflow_port = (validate_workflow_port(workflow_port)
                              if workflow_port is not None else None)
        # Host-only callback; never copied into RuntimeTaskHandle or AgentSessionHandle.
        self._sharing_verifier = sharing_verifier
        self._membership_authority = membership_authority
        self._operator_read_model = operator_read_model

    def begin_task(self, request: TaskRequest, **start_options: Any) -> RuntimeTaskHandle:
        from pathlib import Path
        bundle = resolve(
            request, action_registry=self.action_registry,
            routing_profile_path=Path(self.routing_profile_path)
            if self.routing_profile_path else None,
            behavioral_advisor=self.behavioral_advisor,
            sharing_verifier=self._sharing_verifier,
            workflow_port=self.workflow_port)
        if "telemetry_disabled" in start_options:
            if bool(start_options["telemetry_disabled"]) != self.telemetry_disabled:
                raise ValueError("task start cannot override the runtime telemetry contract")
            start_options = dict(start_options)
            start_options.pop("telemetry_disabled")
        try:
            session = start(bundle, telemetry_disabled=self.telemetry_disabled,
                            host_context=self.host_context,
                            membership_authority=self._membership_authority,
                            operator_read_model=self._operator_read_model,
                            **start_options)
        except Exception:
            if self._operator_read_model is not None:
                self._operator_read_model.close()
            raise
        return RuntimeTaskHandle(_register_host_session(session))

    def before_tool(self, session: RuntimeSession, action: Mapping[str, Any]) -> GateDecision:
        return session.before_action(dict(action))

    def preflight_tool(self, session: RuntimeSession, action: Mapping[str, Any]):
        """Check CFW hard action authority before external semantic control."""
        return session.preflight_action(dict(action))

    def prepare_tool(self, session: RuntimeSession, action: Mapping[str, Any]):
        """Apply CFW's final local action gate and return its result correlation."""
        return session.prepare_action(dict(action))

    def before_retry(self, session: RuntimeSession,
                     request: Mapping[str, Any]) -> GateDecision:
        return session.before_retry(dict(request))

    def agent_handle(self, session: RuntimeSession | RuntimeTaskHandle) -> AgentSessionHandle:
        if isinstance(session, RuntimeTaskHandle):
            return session.agent
        return AgentSessionHandle(_register_host_session(session))

    def host_handle(self, session: RuntimeSession | RuntimeTaskHandle) -> HostSessionHandle:
        if isinstance(session, RuntimeTaskHandle):
            return HostSessionHandle(session._session_token, host_api=self)
        return HostSessionHandle(_register_host_session(session), host_api=self)

    def before_model_call(self, session: RuntimeSession, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return session.before_model_call(tool_capable=tool_capable,
                                         action_hint=action_hint)

    def before_tool_planning(self, session: RuntimeSession,
                            action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return session.before_tool_planning(action_hint)

    def after_tool(self, session: RuntimeSession, *, tool_type: str,
                   result_class: str, subject_ref: Optional[str] = None,
                   invocation_id: Optional[str] = None,
                   action_request_id: Optional[str] = None,
                   result_digest: Optional[str] = None,
                   actual_result: Optional[Mapping[str, Any]] = None,
                   covered_boundary_ids: Iterable[str] = ()) -> str:
        return session._host_ingress().record_tool_result(
            tool_type, result_class, subject_ref,
            invocation_id=invocation_id, result_digest=result_digest,
            action_request_id=action_request_id,
            actual_result=actual_result,
            covered_boundary_ids=covered_boundary_ids)

    def on_artifact(self, session: RuntimeSession, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True,
                    impact_facts: Optional[Mapping[str, Any]] = None) -> None:
        """Host lifecycle hook for a produced artifact boundary."""
        session.on_artifact(flow_triggered=flow_triggered,
                            durable_contract_changed=durable_contract_changed,
                            state_changed=state_changed,
                            impact_facts=impact_facts)

    def artifact_processed(self, session: RuntimeSession) -> None:
        session.artifact_processed()

    def observation(self, session: RuntimeSession, observation_type: str,
                   subject_ref: Optional[str] = None,
                   *, details: Optional[Mapping[str, Any]] = None) -> str:
        return session._host_ingress().record_observation(
            observation_type, subject_ref, details=details)

    def after_validator(self, session: RuntimeSession, *, validator_id: str,
                        invocation_id: str, result: Mapping[str, Any],
                        subject_ref: Optional[str] = None,
                        claim_digest: Optional[str] = None,
                        covered_boundary_ids: Iterable[str] = ()) -> str:
        return session._host_ingress().record_validator_result(
            validator_id, invocation_id, result, subject_ref,
            claim_digest=claim_digest,
            covered_boundary_ids=covered_boundary_ids)

    def create_completion_claim(self, session: RuntimeSession, *,
                                subject_ref: str, invocation_id: str,
                                claim_id: Optional[str] = None,
                                boundary_type: str = "completion",
                                boundary_target: str = "task",
                                expected_validator: str = "completion-boundary-v1",
                                criterion_ids: Optional[Iterable[str]] = None,
                                criterion_evidence_refs: Optional[Mapping[str, Iterable[str]]] = None
                                ) -> CompletionClaim:
        goal = session.bundle.pinned.get("goal_contract") or {}
        required_ids = [str(item.get("criterion_id"))
                        for item in goal.get("acceptance_criteria", ())
                        if item.get("required", True)]
        return CompletionClaim.create(
            claim_id=claim_id or f"claim-{session.bundle.task_id}-{invocation_id}",
            task_id=session.bundle.task_id, subject_ref=subject_ref,
            boundary_type=boundary_type, boundary_target=boundary_target,
            expected_validator=expected_validator, invocation_id=invocation_id,
            goal_contract_hash=str(session.bundle.pinned.get("goal_contract_hash") or ""),
            criterion_ids=tuple(criterion_ids) if criterion_ids is not None else tuple(required_ids),
            criterion_evidence_refs=criterion_evidence_refs)

    def operator_observation(self, session: RuntimeSession, *, subject_ref: str,
                             invalidates: str) -> str:
        return session._host_ingress().record_operator_observation(subject_ref, invalidates)

    def request_transition(self, session: RuntimeSession, *, statework_id: str,
                           subject_ref: str, output_state: str,
                           trigger: Optional[str] = None,
                           evidence_refs: Optional[list[str]] = None,
                           input_state: Optional[str] = None) -> GateDecision:
        return session.record_state_transition(
            statework_id, subject_ref, output_state, trigger=trigger,
            evidence_refs=evidence_refs, input_state=input_state)

    def request_completion(self, session: RuntimeSession, *, subject_ref: Optional[str],
                           evidence_refs: list[str], claim: Any = None) -> GateDecision:
        return session.record_completion_claim(subject_ref, evidence_refs, claim=claim)

    def validate_completion_boundary(self, session: RuntimeSession, *,
                                     subject_ref: str, validator_id: str,
                                     invocation_id: str,
                                     result: Mapping[str, Any],
                                     claim_id: Optional[str] = None,
                                     boundary_type: str = "completion",
                                     boundary_target: str = "task",
                                     criterion_ids: Optional[Iterable[str]] = None,
                                     criterion_evidence_refs: Optional[Mapping[str, Iterable[str]]] = None
                                     ) -> GateDecision:
        claim = self.create_completion_claim(
            session, subject_ref=subject_ref, invocation_id=invocation_id,
            claim_id=claim_id, boundary_type=boundary_type,
            boundary_target=boundary_target, expected_validator=validator_id,
            criterion_ids=criterion_ids,
            criterion_evidence_refs=criterion_evidence_refs)
        evidence_ref = self.after_validator(
            session, validator_id=validator_id, invocation_id=invocation_id,
            result=result, subject_ref=subject_ref,
            claim_digest=claim.claim_digest)
        return self.request_completion(session, subject_ref=subject_ref,
                                       evidence_refs=[evidence_ref], claim=claim)

    def publish_handoff(self, session: RuntimeSession, packet: dict[str, Any], *,
                        observation_context: Optional[Mapping[str, Any]] = None) -> str:
        return session._host_ingress().publish_handoff(
            packet, observation_context=observation_context)

    def route_outcome(self, session: RuntimeSession, *, route: str,
                      route_type: str, outcome: str,
                      subject_ref: Optional[str] = None) -> str:
        raise PermissionError(
            "route outcomes must reference a host route decision and registered evaluator")

    def route_decision(self, session: RuntimeSession, *, route: str,
                       route_type: str, selection_reason: str = "host_selected") -> str:
        return session._host_ingress().record_route_decision(
            route=route, route_type=route_type, selection_reason=selection_reason)

    def evaluate_route_outcome(self, session: RuntimeSession, *, route_decision_id: str,
                               evaluator_id: str, evidence_refs: list[str]) -> str:
        return session._host_ingress().evaluate_route_outcome(
            route_decision_id=route_decision_id, evaluator_id=evaluator_id,
            evidence_refs=evidence_refs)

    def route_outcome_for_decision(self, session: RuntimeSession, *, route_decision_id: str,
                                   evaluator_id: str, evidence_refs: list[str]) -> str:
        return session._host_ingress().record_route_outcome(
            route_decision_id=route_decision_id, evaluator_id=evaluator_id,
            evidence_refs=evidence_refs)

    def consume_handoff(self, session: RuntimeSession, packet_id: str, *,
                        expected_packet_type: str, expected_subject_ref: str,
                        observation_context: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return session.consume_handoff(
            packet_id, expected_packet_type=expected_packet_type,
            expected_subject_ref=expected_subject_ref,
            observation_context=observation_context)

    def advance_segment(self, session: RuntimeSession,
                        segment_index: Optional[int] = None):
        return session.advance_segment(segment_index)

    def resolve_contradiction(self, session: RuntimeSession, contradiction_id: str,
                              fresh_evidence_refs: list[str]) -> GateDecision:
        return session.resolve_contradiction(contradiction_id, fresh_evidence_refs)

    def finish_task(self, session: RuntimeSession, output: Optional[dict[str, Any]] = None,
                    **kwargs: Any) -> dict[str, Any]:
        return session.finish_task(output, **kwargs)

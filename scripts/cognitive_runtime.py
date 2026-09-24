"""Host-facing Cognitive Runtime interceptor API.

This is the integration boundary for an agent host.  DigitalPsychology can
observe the resulting telemetry and receipts, but it is not required to run
the enforcement path.  The host process and its protected same-user runtime
directory are the trusted computing base; same-process Python is not an
untrusted security boundary.  Untrusted plugins must use an external
IPC/RPC gateway as described in ``SECURITY.md``.
"""
from __future__ import annotations

import uuid
from types import MappingProxyType
from typing import Any, Mapping, Optional

from runtime import (CompletionClaim, GateDecision, RuntimeSession, start)
from runtime_contract.actions import (ActionDescriptor, HostExecutionContext,
                                       effective_action_registry)
from runtime_contract.advisor import BehavioralAdvisor
from resolve_runtime import RuntimeBundle, TaskRequest, resolve


_HOST_SESSION_REGISTRY: dict[str, RuntimeSession] = {}


def _register_host_session(session: RuntimeSession) -> str:
    token = f"host-session-{uuid.uuid4().hex}"
    _HOST_SESSION_REGISTRY[token] = session
    return token


def _resolve_host_session(token: str) -> RuntimeSession:
    session = _HOST_SESSION_REGISTRY.get(token)
    if session is None:
        raise RuntimeError("unknown or expired host session handle")
    return session


class AgentSessionHandle:
    """Capability-limited view handed to an agent adapter.

    It deliberately contains no evidence ledger, host ingress, operator
    observation, or validator methods.  The underlying session remains an
    implementation detail of the host runtime.
    """

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token

    @property
    def session_id(self) -> str:
        return self._session_token

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return _resolve_host_session(self._session_token).before_model_call(
            tool_capable=tool_capable, action_hint=action_hint)

    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return _resolve_host_session(self._session_token).before_tool_planning(action_hint)

    def request_action(self, action: Mapping[str, Any]):
        return _resolve_host_session(self._session_token).before_action(dict(action))

    def preview_output(self, output: Optional[dict[str, Any]] = None, **kwargs: Any):
        return _resolve_host_session(self._session_token).preview_output(output, **kwargs)


class RuntimeTaskHandle:
    """Ordinary task-start result with no authoritative session reference.

    The token is only a lookup capability owned by the host registry.  Bundle
    and metadata projections are safe integration data; mutable authority
    state remains behind ``HostSessionHandle``.
    """

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token

    @property
    def session_id(self) -> str:
        return _resolve_host_session(self._session_token).session_id

    @property
    def attempt_id(self) -> str:
        return _resolve_host_session(self._session_token).attempt_id

    @property
    def agent_instance_id(self) -> str:
        return _resolve_host_session(self._session_token).agent_instance_id

    @property
    def bundle(self) -> RuntimeBundle:
        return _resolve_host_session(self._session_token).bundle

    @property
    def route_decisions(self) -> Mapping[str, Mapping[str, Any]]:
        session = _resolve_host_session(self._session_token)
        return MappingProxyType({key: dict(value) for key, value in session.route_decisions.items()})

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return self.agent.before_model_call(tool_capable=tool_capable, action_hint=action_hint)

    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None) -> str:
        return self.agent.before_tool_planning(action_hint)

    @property
    def agent(self) -> AgentSessionHandle:
        return AgentSessionHandle(self._session_token)

    @property
    def host(self) -> "HostSessionHandle":
        return HostSessionHandle(self._session_token)


class HostSessionHandle:
    """Host-only adapter view for evidence, transitions, and completion."""

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token

    @property
    def bundle(self) -> RuntimeBundle:
        return _resolve_host_session(self._session_token).bundle

    @property
    def route_decisions(self) -> Mapping[str, Mapping[str, Any]]:
        session = _resolve_host_session(self._session_token)
        return MappingProxyType({key: dict(value) for key, value in session.route_decisions.items()})

    @property
    def session_id(self) -> str:
        return _resolve_host_session(self._session_token).session_id

    def _session(self) -> RuntimeSession:
        return _resolve_host_session(self._session_token)

    def after_tool(self, **kwargs: Any) -> str:
        return CognitiveRuntime().after_tool(self._session(), **kwargs)

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        """Advance host-owned lifecycle activation after a produced artifact."""
        return CognitiveRuntime().on_artifact(
            self._session(), flow_triggered=flow_triggered,
            durable_contract_changed=durable_contract_changed,
            state_changed=state_changed)

    def observation(self, observation_type: str, subject_ref: Optional[str] = None,
                    *, details: Optional[Mapping[str, Any]] = None) -> str:
        return CognitiveRuntime().observation(
            self._session(), observation_type, subject_ref, details=details)

    def authorization(self, authorization: Mapping[str, Any],
                      subject_ref: Optional[str] = None) -> str:
        return self._session()._host_ingress().record_authorization(
            authorization, subject_ref=subject_ref)

    def repository_truth(self, packet: Mapping[str, Any],
                         observation_context: Mapping[str, Any],
                         subject_ref: Optional[str] = None) -> str:
        return self._session()._host_ingress().record_repository_truth(
            packet, observation_context, subject_ref=subject_ref)

    def repository_evidence(self, packet: Mapping[str, Any],
                            validation: Mapping[str, Any],
                            subject_ref: Optional[str] = None) -> str:
        return self._session()._host_ingress().record_repository_evidence(
            packet, validation, subject_ref=subject_ref)

    def rollback_plan(self, plan: Mapping[str, Any],
                      subject_ref: Optional[str] = None) -> str:
        return self._session()._host_ingress().record_rollback_plan(
            plan, subject_ref=subject_ref)

    def rollback(self, result: Mapping[str, Any],
                 subject_ref: Optional[str] = None) -> str:
        return self._session()._host_ingress().record_rollback(
            result, subject_ref=subject_ref)

    def after_validator(self, **kwargs: Any) -> str:
        return CognitiveRuntime().after_validator(self._session(), **kwargs)

    def validate_completion_boundary(self, **kwargs: Any) -> GateDecision:
        return CognitiveRuntime().validate_completion_boundary(self._session(), **kwargs)

    def operator_observation(self, **kwargs: Any) -> str:
        return CognitiveRuntime().operator_observation(self._session(), **kwargs)

    def request_transition(self, **kwargs: Any) -> GateDecision:
        return CognitiveRuntime().request_transition(self._session(), **kwargs)

    def request_completion(self, **kwargs: Any) -> GateDecision:
        return CognitiveRuntime().request_completion(self._session(), **kwargs)

    def publish_handoff(self, packet: dict[str, Any], **kwargs: Any) -> str:
        return CognitiveRuntime().publish_handoff(self._session(), packet, **kwargs)

    def route_outcome(self, **kwargs: Any) -> str:
        return CognitiveRuntime().route_outcome(self._session(), **kwargs)

    def route_decision(self, **kwargs: Any) -> str:
        return CognitiveRuntime().route_decision(self._session(), **kwargs)

    def evaluate_route_outcome(self, **kwargs: Any) -> str:
        return CognitiveRuntime().evaluate_route_outcome(self._session(), **kwargs)

    def consume_handoff(self, packet_id: str, **kwargs: Any) -> dict[str, Any]:
        return CognitiveRuntime().consume_handoff(self._session(), packet_id, **kwargs)

    def advance_segment(self, segment_index: Optional[int] = None):
        return self._session().advance_segment(segment_index)

    def resolve_contradiction(self, contradiction_id: str,
                              fresh_evidence_refs: list[str]) -> GateDecision:
        return self._session().resolve_contradiction(contradiction_id, fresh_evidence_refs)

    def finish_task(self, output: Optional[dict[str, Any]] = None, **kwargs: Any):
        return CognitiveRuntime().finish_task(self._session(), output, **kwargs)


class CognitiveRuntime:
    def __init__(self, *, telemetry_disabled: bool = False,
                 host_context: Optional[HostExecutionContext] = None,
                 action_registry: Optional[Mapping[str, ActionDescriptor]] = None,
                 routing_profile_path: Optional[str] = None,
                 behavioral_advisor: Optional[BehavioralAdvisor] = None) -> None:
        self.telemetry_disabled = telemetry_disabled
        self.host_context = host_context
        self.action_registry = effective_action_registry(action_registry)
        self.routing_profile_path = routing_profile_path
        self.behavioral_advisor = behavioral_advisor

    def begin_task(self, request: TaskRequest, **start_options: Any) -> RuntimeTaskHandle:
        from pathlib import Path
        bundle = resolve(
            request, action_registry=self.action_registry,
            routing_profile_path=Path(self.routing_profile_path)
            if self.routing_profile_path else None,
            behavioral_advisor=self.behavioral_advisor)
        if "telemetry_disabled" in start_options:
            if bool(start_options["telemetry_disabled"]) != self.telemetry_disabled:
                raise ValueError("task start cannot override the runtime telemetry contract")
            start_options = dict(start_options)
            start_options.pop("telemetry_disabled")
        session = start(bundle, telemetry_disabled=self.telemetry_disabled,
                        host_context=self.host_context, **start_options)
        return RuntimeTaskHandle(_register_host_session(session))

    def before_tool(self, session: RuntimeSession, action: Mapping[str, Any]) -> GateDecision:
        return session.before_action(dict(action))

    def before_retry(self, session: RuntimeSession,
                     request: Mapping[str, Any]) -> GateDecision:
        return session.before_retry(dict(request))

    def agent_handle(self, session: RuntimeSession | RuntimeTaskHandle) -> AgentSessionHandle:
        if isinstance(session, RuntimeTaskHandle):
            return session.agent
        return AgentSessionHandle(_register_host_session(session))

    def host_handle(self, session: RuntimeSession | RuntimeTaskHandle) -> HostSessionHandle:
        if isinstance(session, RuntimeTaskHandle):
            return session.host
        return HostSessionHandle(_register_host_session(session))

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
                   result_digest: Optional[str] = None,
                   actual_result: Optional[Mapping[str, Any]] = None) -> str:
        return session._host_ingress().record_tool_result(
            tool_type, result_class, subject_ref,
            invocation_id=invocation_id, result_digest=result_digest,
            actual_result=actual_result)

    def on_artifact(self, session: RuntimeSession, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        """Host lifecycle hook for a produced artifact boundary."""
        session.on_artifact(flow_triggered=flow_triggered,
                            durable_contract_changed=durable_contract_changed,
                            state_changed=state_changed)

    def observation(self, session: RuntimeSession, observation_type: str,
                   subject_ref: Optional[str] = None,
                   *, details: Optional[Mapping[str, Any]] = None) -> str:
        return session._host_ingress().record_observation(
            observation_type, subject_ref, details=details)

    def after_validator(self, session: RuntimeSession, *, validator_id: str,
                        invocation_id: str, result: Mapping[str, Any],
                        subject_ref: Optional[str] = None,
                        claim_digest: Optional[str] = None) -> str:
        return session._host_ingress().record_validator_result(
            validator_id, invocation_id, result, subject_ref,
            claim_digest=claim_digest)

    def create_completion_claim(self, session: RuntimeSession, *,
                                subject_ref: str, invocation_id: str,
                                claim_id: Optional[str] = None,
                                boundary_type: str = "completion",
                                boundary_target: str = "task",
                                expected_validator: str = "completion-boundary-v1") -> CompletionClaim:
        return CompletionClaim.create(
            claim_id=claim_id or f"claim-{session.bundle.task_id}-{invocation_id}",
            task_id=session.bundle.task_id, subject_ref=subject_ref,
            boundary_type=boundary_type, boundary_target=boundary_target,
            expected_validator=expected_validator, invocation_id=invocation_id)

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
                                     boundary_target: str = "task") -> GateDecision:
        claim = self.create_completion_claim(
            session, subject_ref=subject_ref, invocation_id=invocation_id,
            claim_id=claim_id, boundary_type=boundary_type,
            boundary_target=boundary_target, expected_validator=validator_id)
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


def install_host_interceptor(registry: Any, interceptor: CognitiveRuntime) -> None:
    """Register the adapter with hosts that expose an interceptor registry."""
    register = getattr(registry, "register_host_interceptor", None)
    if not callable(register):
        raise TypeError("host registry must provide register_host_interceptor")
    register(interceptor)

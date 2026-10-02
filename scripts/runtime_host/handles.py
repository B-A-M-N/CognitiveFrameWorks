"""Capability-limited handles for CFW host and agent integrations."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from runtime import GateDecision, RuntimeSession
from resolve_runtime import RuntimeBundle
from runtime_host.session_registry import HOST_SESSION_REGISTRY


class AgentSessionHandle:
    """Capability-limited view handed to an agent adapter."""

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token

    @property
    def session_id(self) -> str:
        return self._session_token

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None,
                          behavior_context: str = "",
                          active_statework_ids: Optional[Iterable[str]] = None) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_model_call(
            tool_capable=tool_capable, action_hint=action_hint,
            behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Optional[Iterable[str]] = None) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_tool_planning(
            action_hint, behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def request_action(self, action: Mapping[str, Any]):
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_action(dict(action))

    def preview_output(self, output: Optional[dict[str, Any]] = None, **kwargs: Any):
        return HOST_SESSION_REGISTRY.resolve(self._session_token).preview_output(output, **kwargs)


class RuntimeTaskHandle:
    """Host task result with public metadata and no authoritative session reference."""

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token
        self._digital_psychology_adapter = None

    @property
    def session_id(self) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).session_id

    @property
    def attempt_id(self) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).attempt_id

    @property
    def agent_instance_id(self) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).agent_instance_id

    @property
    def bundle(self) -> RuntimeBundle:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).bundle

    @property
    def route_decisions(self) -> Mapping[str, Mapping[str, Any]]:
        session = HOST_SESSION_REGISTRY.resolve(self._session_token)
        return MappingProxyType({key: dict(value)
                                 for key, value in session.route_decisions.items()})

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Optional[Mapping[str, Any]] = None,
                          behavior_context: str = "",
                          active_statework_ids: Optional[Iterable[str]] = None) -> str:
        return self.agent.before_model_call(
            tool_capable=tool_capable, action_hint=action_hint,
            behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def before_tool_planning(self, action_hint: Optional[Mapping[str, Any]] = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Optional[Iterable[str]] = None) -> str:
        return self.agent.before_tool_planning(
            action_hint, behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def digital_psychology_adapter(self):
        """Return the explicit DP component port for this host-owned task."""
        if self._digital_psychology_adapter is not None:
            return self._digital_psychology_adapter
        from digital_psychology_adapter import CFWBehaviorAdapter
        self._digital_psychology_adapter = CFWBehaviorAdapter(
            HOST_SESSION_REGISTRY.resolve(self._session_token), self.agent)
        return self._digital_psychology_adapter

    @property
    def agent(self) -> AgentSessionHandle:
        return AgentSessionHandle(self._session_token)

    def close(self) -> None:
        HOST_SESSION_REGISTRY.close(self._session_token)


class HostSessionHandle:
    """Host-only adapter view for evidence, transitions, and completion."""

    def __init__(self, session_token: str, *, host_api: Any = None) -> None:
        self._session_token = session_token
        self._host_api_instance = host_api

    @property
    def bundle(self) -> RuntimeBundle:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).bundle

    @property
    def route_decisions(self) -> Mapping[str, Mapping[str, Any]]:
        session = HOST_SESSION_REGISTRY.resolve(self._session_token)
        return MappingProxyType({key: dict(value)
                                 for key, value in session.route_decisions.items()})

    @property
    def session_id(self) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).session_id

    def _session(self) -> RuntimeSession:
        return HOST_SESSION_REGISTRY.resolve(self._session_token)

    def _host_api(self):
        if self._host_api_instance is None:
            from cognitive_runtime import CognitiveRuntime
            self._host_api_instance = CognitiveRuntime()
        return self._host_api_instance

    def operator_trace(self) -> dict[str, Any]:
        return self._session()._operator_trace()

    def close(self) -> None:
        HOST_SESSION_REGISTRY.close(self._session_token)

    def after_tool(self, **kwargs: Any) -> str:
        return self._host_api().after_tool(self._session(), **kwargs)

    def complete_action(self) -> None:
        self._session()._host_ingress().complete_action()

    def begin_attempt(self, **kwargs: Any):
        return self._session()._host_ingress().begin_attempt(**kwargs)

    def record_attempt_failure(self, **kwargs: Any) -> None:
        self._session()._host_ingress().record_attempt_failure(**kwargs)

    def record_reinspection(self, **kwargs: Any) -> None:
        self._session()._host_ingress().record_reinspection(**kwargs)

    def record_goal_progress(self, **kwargs: Any) -> None:
        self._session()._host_ingress().record_goal_progress(**kwargs)

    def record_recovery_epoch(self, **kwargs: Any) -> None:
        self._session()._host_ingress().record_recovery_epoch(**kwargs)

    def reject_investigative_path(self, **kwargs: Any) -> None:
        self._session()._host_ingress().reject_investigative_path(**kwargs)

    def before_retry(self, request: Mapping[str, Any]) -> GateDecision:
        return self._session().before_retry(request)

    def on_artifact(self, *, flow_triggered: bool = False,
                    durable_contract_changed: bool = False,
                    state_changed: bool = True) -> None:
        """Advance host-owned lifecycle activation after a produced artifact."""
        self._host_api().on_artifact(
            self._session(), flow_triggered=flow_triggered,
            durable_contract_changed=durable_contract_changed,
            state_changed=state_changed)

    def artifact_processed(self) -> None:
        self._host_api().artifact_processed(self._session())

    def observation(self, observation_type: str, subject_ref: Optional[str] = None,
                    *, details: Optional[Mapping[str, Any]] = None) -> str:
        return self._host_api().observation(
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
        return self._host_api().after_validator(self._session(), **kwargs)

    def validate_completion_boundary(self, **kwargs: Any) -> GateDecision:
        return self._host_api().validate_completion_boundary(self._session(), **kwargs)

    def operator_observation(self, **kwargs: Any) -> str:
        return self._host_api().operator_observation(self._session(), **kwargs)

    def request_transition(self, **kwargs: Any) -> GateDecision:
        return self._host_api().request_transition(self._session(), **kwargs)

    def request_completion(self, **kwargs: Any) -> GateDecision:
        return self._host_api().request_completion(self._session(), **kwargs)

    def publish_handoff(self, packet: dict[str, Any], **kwargs: Any) -> str:
        return self._host_api().publish_handoff(self._session(), packet, **kwargs)

    def route_outcome(self, **kwargs: Any) -> str:
        return self._host_api().route_outcome(self._session(), **kwargs)

    def route_decision(self, **kwargs: Any) -> str:
        return self._host_api().route_decision(self._session(), **kwargs)

    def evaluate_route_outcome(self, **kwargs: Any) -> str:
        return self._host_api().evaluate_route_outcome(self._session(), **kwargs)

    def consume_handoff(self, packet_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._host_api().consume_handoff(
            self._session(), packet_id, **kwargs)

    def advance_segment(self, segment_index: Optional[int] = None):
        return self._session().advance_segment(segment_index)

    def resolve_contradiction(self, contradiction_id: str,
                              fresh_evidence_refs: list[str]) -> GateDecision:
        return self._session().resolve_contradiction(
            contradiction_id, fresh_evidence_refs)

    def finish_task(self, output: Optional[dict[str, Any]] = None, **kwargs: Any):
        return self._host_api().finish_task(self._session(), output, **kwargs)


__all__ = ["AgentSessionHandle", "RuntimeTaskHandle", "HostSessionHandle"]

"""Capability-limited handles for CFW host and agent integrations."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from resolve_runtime import RuntimeBundle
from runtime import GateDecision, RuntimeSession
from runtime_session.projection import context_for

from runtime_host.session_registry import HOST_SESSION_REGISTRY


class AgentSessionHandle:
    """Capability-limited view handed to an agent adapter."""

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token

    @property
    def session_id(self) -> str:
        return self._session_token

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Mapping[str, Any] | None = None,
                          behavior_context: str = "",
                          active_statework_ids: Iterable[str] | None = None) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_model_call(
            tool_capable=tool_capable, action_hint=action_hint,
            behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def before_tool_planning(self, action_hint: Mapping[str, Any] | None = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Iterable[str] | None = None) -> str:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_tool_planning(
            action_hint, behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def request_action(self, action: Mapping[str, Any]):
        return HOST_SESSION_REGISTRY.resolve(self._session_token).before_action(dict(action))

    def preview_output(self, output: dict[str, Any] | None = None, **kwargs: Any):
        return HOST_SESSION_REGISTRY.resolve(self._session_token).preview_output(output, **kwargs)


class RuntimeTaskHandle:
    """Host task result with public metadata and no authoritative session reference."""

    def __init__(self, session_token: str) -> None:
        self._session_token = session_token
        self._digital_psychology_adapter = None

    @property
    def session_id(self) -> str:
        return (HOST_SESSION_REGISTRY.resolve(self._session_token)
                .identity_authority.identity.session_id)

    @property
    def attempt_id(self) -> str:
        return (HOST_SESSION_REGISTRY.resolve(self._session_token)
                .identity_authority.identity.attempt_id)

    @property
    def cfw_behavior_revision(self) -> int:
        """Return the current CFW-owned behavior revision for gate receipts."""
        revision = HOST_SESSION_REGISTRY.resolve(
            self._session_token).external_behavior_revision
        return revision if type(revision) is int and revision >= 0 else 0

    @property
    def agent_instance_id(self) -> str:
        return (HOST_SESSION_REGISTRY.resolve(self._session_token)
                .identity_authority.identity.agent_instance_id)

    @property
    def bundle(self) -> RuntimeBundle:
        return HOST_SESSION_REGISTRY.resolve(self._session_token).bundle

    @property
    def route_decisions(self) -> Mapping[str, Mapping[str, Any]]:
        session = HOST_SESSION_REGISTRY.resolve(self._session_token)
        return MappingProxyType({key: dict(value)
                                 for key, value in session.actions.route_decisions.items()})

    def before_model_call(self, *, tool_capable: bool = False,
                          action_hint: Mapping[str, Any] | None = None,
                          behavior_context: str = "",
                          active_statework_ids: Iterable[str] | None = None) -> str:
        return self.agent.before_model_call(
            tool_capable=tool_capable, action_hint=action_hint,
            behavior_context=behavior_context,
            active_statework_ids=active_statework_ids)

    def base_model_context(self, *,
                           active_statework_ids: Iterable[str] | None = None) -> str:
        """Read the current CFW context without recording a model-call boundary.

        Composite hosts use this to let an external control bridge add its
        projection before the single CFW model-call boundary is recorded.
        """
        return context_for(
            HOST_SESSION_REGISTRY.resolve(self._session_token),
            active_statework_ids=active_statework_ids)

    def behavioral_trace_snapshot(self) -> dict[str, Any]:
        """Return bounded host diagnostics for an evaluation turn; no model text."""
        session = HOST_SESSION_REGISTRY.resolve(self._session_token)
        decision = session._evaluate_metacognition()
        pinned_hash = str(session.bundle.pinned.get("goal_contract_hash") or "")
        policy_hash = str(session.bundle.policy.goal_contract_hash or "")
        if not pinned_hash:
            anchor_state = "missing_host_contract"
        elif pinned_hash != policy_hash:
            anchor_state = "pinned_contract_mismatch"
        else:
            anchor_state = "pinned_host_contract"
        reason_codes = {
            "unresolved operator contradiction": "evidence.unresolved_contradiction",
            "current recovery budget exhausted; yield to runnable criteria": (
                "attention.local_yield_to_runnable_criteria"),
            "support-action budget reached": "convergence.support_action_limit",
            "committed-action budget reached without goal evidence": (
                "convergence.total_action_limit"),
            "required criterion remains unfinished": "goal.required_criterion_pending",
            "host completion prerequisites are ready": "completion.prerequisites_ready",
            "completion readiness still requires host evidence": "completion.evidence_pending",
            "behavior policy is owned by DigitalPsychology": "behavior.external_owner",
        }
        reason = reason_codes.get(decision.reason, "metacognition.other")
        convergence = (session.attempts.controller.state("primary-objective")
                       if session.attempts.controller is not None else None)
        goal = session.bundle.pinned.get("goal_contract") or {}
        return {
            "attempt_id": session.identity_authority.identity.attempt_id,
            "behavioral_mode": decision.mode,
            "active_optional_components": sorted(
                session.lifecycle_workflow.active_lifecycle)[:32],
            "focus_criterion_ids": list(decision.active_criterion_ids[:8]),
            "reason_codes": [reason],
            "local_yield_event": (
                reason == "attention.local_yield_to_runnable_criteria"),
            "global_blocked_event": decision.mode == "BLOCKED",
            "goal_anchor_state": anchor_state,
            "goal_contract_hash": pinned_hash[:64] if pinned_hash else "",
            "goal_reanchor_required": decision.goal_reanchor_required,
            "retry_recovery_mode": (convergence.disposition
                                     if convergence is not None else "none"),
            "retry_recovery_counters": ({
                "total_actions_since_goal_progress": int(
                    convergence.total_actions_since_goal_progress),
                "support_actions_since_goal_progress": int(
                    convergence.support_actions_since_goal_progress),
                "recovery_epochs_since_goal_progress": int(
                    convergence.recovery_epochs_since_goal_progress),
                "same_approach_retries": int(convergence.same_approach_retries),
                "equivalent_retry_count": int(
                    session.metacognitive.repeated_branch_count),
            } if convergence is not None else {}),
            "context_strategy": ("external_behavior_projection"
                                 if session.behavior.is_external
                                 else "standalone_contract_guidance"),
            "response_strategy": decision.mode.lower(),
            "work_mode": str(goal.get("work_mode") or "")[:32],
        }

    def before_tool_planning(self, action_hint: Mapping[str, Any] | None = None,
                             *, behavior_context: str = "",
                             active_statework_ids: Iterable[str] | None = None) -> str:
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
                                 for key, value in session.actions.route_decisions.items()})

    @property
    def session_id(self) -> str:
        return (HOST_SESSION_REGISTRY.resolve(self._session_token)
                .identity_authority.identity.session_id)

    def _session(self) -> RuntimeSession:
        return HOST_SESSION_REGISTRY.resolve(self._session_token)

    def _host_api(self):
        if self._host_api_instance is None:
            from runtime_host.host_api import CognitiveRuntime
            self._host_api_instance = CognitiveRuntime()
        return self._host_api_instance

    def operator_trace(self) -> dict[str, Any]:
        return self._session()._operator_trace()

    def close(self) -> None:
        HOST_SESSION_REGISTRY.close(self._session_token)

    def after_tool(self, **kwargs: Any) -> str:
        return self._host_api().after_tool(self._session(), **kwargs)

    def preflight_tool(self, action: Mapping[str, Any]):
        """Check the CFW hard gate before an external DP pre-action tick."""
        return self._host_api().preflight_tool(self._session(), action)

    def prepare_tool(self, action: Mapping[str, Any]):
        """Run the final CFW gate and return the action result correlation ID."""
        return self._host_api().prepare_tool(self._session(), action)

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
                    state_changed: bool = True,
                    impact_facts: Mapping[str, Any] | None = None) -> None:
        """Advance host-owned lifecycle activation after a produced artifact."""
        self._host_api().on_artifact(
            self._session(), flow_triggered=flow_triggered,
            durable_contract_changed=durable_contract_changed,
            state_changed=state_changed, impact_facts=impact_facts)

    def artifact_processed(self) -> None:
        self._host_api().artifact_processed(self._session())

    def observation(self, observation_type: str, subject_ref: str | None = None,
                    *, details: Mapping[str, Any] | None = None) -> str:
        return self._host_api().observation(
            self._session(), observation_type, subject_ref, details=details)

    def authorization(self, authorization: Mapping[str, Any],
                      subject_ref: str | None = None) -> str:
        return self._session()._host_ingress().record_authorization(
            authorization, subject_ref=subject_ref)

    def repository_truth(self, packet: Mapping[str, Any],
                         observation_context: Mapping[str, Any],
                         subject_ref: str | None = None) -> str:
        return self._session()._host_ingress().record_repository_truth(
            packet, observation_context, subject_ref=subject_ref)

    def repository_evidence(self, packet: Mapping[str, Any],
                            validation: Mapping[str, Any],
                            subject_ref: str | None = None) -> str:
        return self._session()._host_ingress().record_repository_evidence(
            packet, validation, subject_ref=subject_ref)

    def rollback_plan(self, plan: Mapping[str, Any],
                      subject_ref: str | None = None) -> str:
        return self._session()._host_ingress().record_rollback_plan(
            plan, subject_ref=subject_ref)

    def rollback(self, result: Mapping[str, Any],
                 subject_ref: str | None = None) -> str:
        return self._session()._host_ingress().record_rollback(
            result, subject_ref=subject_ref)

    def after_validator(self, **kwargs: Any) -> str:
        return self._host_api().after_validator(self._session(), **kwargs)

    def evidence_is_current(self, evidence_ref: str) -> bool:
        """Return whether host-attested evidence remains valid in this session."""
        if not isinstance(evidence_ref, str) or not evidence_ref or len(evidence_ref) > 256:
            return False
        record = self._session().evidence.ledger.get(evidence_ref)
        return record is not None and not record.invalidated

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

    def advance_segment(self, segment_index: int | None = None):
        return self._session().advance_segment(segment_index)

    def resolve_contradiction(self, contradiction_id: str,
                              fresh_evidence_refs: list[str]) -> GateDecision:
        return self._session().resolve_contradiction(
            contradiction_id, fresh_evidence_refs)

    def finish_task(self, output: dict[str, Any] | None = None, **kwargs: Any):
        return self._host_api().finish_task(self._session(), output, **kwargs)


__all__ = ["AgentSessionHandle", "HostSessionHandle", "RuntimeTaskHandle"]

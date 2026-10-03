"""CFW action authorization and preflight methods for runtime sessions."""
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Dict, Mapping, Optional

from runtime_contract.actions import (
    ActionObjectiveBinding, ActionStrategy, AgentActionRequest,
    ProtectedResourcePolicy,
)
from runtime_contract.behavioral_fusion import fuse_retry_relation
from runtime_contract.convergence import TERMINAL_DISPOSITIONS
from runtime_contract.handlers import handler_for_enforcement
from runtime_contract.system_one import ActivityFact
from runtime_session.host_authority import _HostCapability
from runtime_session.lifecycle import ActionPreflightResult, ActionPreparation, GateDecision


class ActionGateMethods:
    """Authorization methods operating on RuntimeSession's canonical state."""
    def _action_strategy(self) -> Dict[str, Any]:
        """Return a bounded, advisory action plan inside host-owned policy.

        A learned preference may order already-authorized bindings, but this
        object grants no permission; ``before_action`` remains authoritative.
        """
        preferences: list[str] = []
        constrained = (self.lifecycle is not None
                       and self.lifecycle.component_mode("fuse") == "constrained")
        effective_state = self.effective_external_behavior_projection()
        focus = set(effective_state.get("focus_criteria") or ())
        if constrained:
            objective_routes = []
            for route, descriptor in self.action_registry.items():
                if descriptor.goal_relation not in {
                        "advances_primary_goal", "removes_concrete_blocker",
                        "required_verification"}:
                    continue
                affected = set(descriptor.affected_criterion_ids)
                if focus and affected and not focus.intersection(affected):
                    continue
                objective_routes.append(route)
            preferences.extend(sorted(objective_routes))
        if not self.external_behavior_authority:
            for adjustment in self.bundle.guard_pack.get("routing_adjustments", ()):
                if (adjustment.get("route_type") == "tool_policy"
                        and adjustment.get("disposition") == "prefer"):
                    route = str(adjustment.get("route") or "")
                    if route in self.action_registry and route not in preferences:
                        preferences.append(route)
        eligible = [route for route in self.action_registry if route not in preferences]
        return ActionStrategy(
            eligible_actions=tuple(preferences + eligible),
            priorities={route: index for index, route in enumerate(preferences)},
            reason=("FUSE-constrained objective-first ordering over host-authorized actions"
                    if constrained else
                    "learned preference within host-authorized action set"),
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
        ).to_dict()
    def _action_strategy_hint(self) -> str:
        strategy = self._action_strategy()
        if not strategy["eligible_actions"]:
            return ""
        preferred = [action for action, _priority in sorted(
            strategy["priorities"].items(), key=lambda pair: pair[1])]
        constrained = (self.lifecycle is not None
                       and self.lifecycle.component_mode("fuse") == "constrained")
        if not preferred and not constrained:
            return ""
        other = [action for action in strategy["eligible_actions"] if action not in preferred]
        lines = ["\n===== ACTION STRATEGY ====="]
        if preferred:
            lines.append(f"Preferred when relevant: {', '.join(preferred[:12])}.")
        if constrained:
            lines.append(
                "FUSE is constrained: choose an objective or focused-criterion route "
                "when relevant, then reassess host-evidenced progress.")
        if other:
            label = "Other authorized actions" if preferred else "Authorized actions"
            lines.append(f"{label}: {', '.join(other[:12])}.")
        lines.append("Preferences do not grant permission; every action still passes the host gate.")
        return "\n".join(lines)
    def _decision(self, action: Mapping[str, Any] | AgentActionRequest) -> GateDecision:
        request = self._request(action)
        descriptor = self._descriptor(request)
        for adjustment in self.bundle.guard_pack.get("routing_adjustments", ()):
            if (adjustment.get("route_type") == "tool_policy"
                    and adjustment.get("disposition") == "suppress"
                    and adjustment.get("route") == request.binding_id):
                return GateDecision.BLOCK
        for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
            if enforcement.get("mode") not in {"structural", "tool_gate"}:
                continue
            handler = handler_for_enforcement(enforcement)
            result = (handler.gate(self, request, enforcement)
                      if handler and "before_action" in getattr(handler, "hooks", ())
                      else None)
            if result == "block":
                return GateDecision.BLOCK
            if result == "require_reinspection":
                return GateDecision.REQUIRE_REINSPECTION
        confirmation = self.host_context.confirmation_for(request, self._consumed_grant_ids)
        if descriptor.destructive and confirmation is None:
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.outside_scope or descriptor.untrusted:
            return GateDecision.BLOCK
        authorities = set(descriptor.allowed_authorities or ())
        if descriptor.required_authority and not authorities:
            authorities = {descriptor.required_authority}
        if authorities and self.host_context.principal not in authorities:
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.required_capabilities and not set(descriptor.required_capabilities).issubset(
                set(self.host_context.capabilities)):
            return GateDecision.REQUIRE_CONFIRMATION
        if descriptor.mutability not in {"read", "inspect"}:
            policy = self.host_context.protected_resources
            if tuple(self.host_context.filesystem_scope) != policy.authorized_write_roots:
                policy = ProtectedResourcePolicy(policy.protected_roots,
                                                 tuple(self.host_context.filesystem_scope))
            classification = policy.classify_write(request.arguments)
            if classification != "authorized":
                return GateDecision.BLOCK
        if any(not record["resolved"] and
               (record.get("subject") in {None, request.subject_ref})
               for record in self.contradictions.values()):
            return GateDecision.REQUIRE_REINSPECTION
        return GateDecision.ALLOW
    @staticmethod
    def _more_restrictive(left: GateDecision, right: GateDecision) -> GateDecision:
        rank = {
            GateDecision.ALLOW: 0,
            GateDecision.REQUIRE_CONFIRMATION: 1,
            GateDecision.REQUIRE_REINSPECTION: 2,
            GateDecision.REQUIRE_CHANGED_HYPOTHESIS: 3,
            GateDecision.REQUIRE_GOAL_REANCHOR: 4,
            GateDecision.REQUIRE_RECOVERY: 5,
            GateDecision.BLOCK: 6,
        }
        return left if rank[left] >= rank[right] else right
    def _operator_state_snapshot(self) -> list[dict[str, Any]]:
        """Return bounded StateWork state references without evidence content."""
        subject = self.bundle.policy.subject_ref
        snapshots: list[dict[str, Any]] = []
        for statework in self.bundle.stateworks:
            statework_id = str(statework.get("id") or "")
            bound_subject = str(statework.get("subject_ref") or subject or "")
            state = self.statework_states.get((statework_id, bound_subject))
            revision = None
            if self.state_authority is not None and bound_subject:
                try:
                    contract_hash = str(statework.get("workflow_contract_hash") or "")
                    if not contract_hash:
                        raise ValueError("StateWork has no pinned workflow contract identity")
                    authoritative = self.state_authority.inspect_workflow(
                        statework_id=statework_id, subject_ref=bound_subject,
                        contract_hash=contract_hash)
                    state = authoritative.current_state
                    revision = authoritative.revision
                except Exception as exc:
                    revision = None
                    state = f"unavailable:{type(exc).__name__}"
            snapshots.append({
                "statework_id": statework_id,
                "subject_ref": bound_subject,
                "state": state,
                "engine_kind": engine_kind,
                "revision": revision,
            })
        return snapshots
    def _revocation_current(self) -> bool:
        if self.policy_revocation_authority is None or not self.adopted_profile_hashes:
            return True
        try:
            snapshot = self.policy_revocation_authority.inspect(tuple(self.adopted_profile_hashes))
            revision = int(snapshot.get("revision", 0) or 0)
            revoked = set(snapshot.get("revoked_profile_hashes") or ())
            immediate = bool(snapshot.get("immediate"))
            if not revoked.intersection(self.adopted_profile_hashes):
                self.policy_revocation_revision = max(self.policy_revocation_revision, revision)
                return True
            if self.revocation_mode == "immediate" or immediate:
                return False
            # Deferred: the pinned policy remains intact until the next task.
            self.policy_revocation_revision = max(self.policy_revocation_revision, revision)
            return True
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "policy_revocation_unavailable",
                                "payload": {"summary": type(exc).__name__,
                                            "result_class": "reinspection required"}})
            return False
    def _operator_trace(self) -> dict[str, Any]:
        """Correlate bounded authority references; never expose prompt or raw evidence."""
        advice = self.bundle.guard_pack.get("advice_exchange") or {}
        advice_context = advice.get("context") if isinstance(advice, Mapping) else {}
        advice_sources = advice.get("adjustments") if isinstance(advice, Mapping) else []
        return {
            "trace_version": "1.0.0",
            "task": {
                "task_id": self.bundle.task_id,
                "session_id": self.session_id,
                "attempt_id": self.attempt_id,
                "agent_instance_id": self.agent_instance_id,
            },
            "policy": {
                "policy_hash": self.bundle.pinned.get("policy_hash"),
                "static_policy_hash": (advice_context or {}).get("static_policy_hash"),
                "eligible_choice_hash": (advice_context or {}).get("eligible_choice_hash"),
                "context_identity": (advice_context or {}).get("context_identity"),
            },
            "statework": self._operator_state_snapshot(),
            "advice": {
                "advice_id": (advice_context or {}).get("advice_version"),
                "source_profile_hash": (advice_context or {}).get("source_profile_hash"),
                "expires_at": (advice_context or {}).get("expires_at"),
                "adjustment_count": len(advice_sources or []),
            },
            "route_decisions": [dict(value) for value in self.route_decisions.values()],
            "route_outcomes": [dict(value) for value in self.route_outcomes.values()],
        }
    def _membership_for(self, statework_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        if statework_id:
            for membership in self.shared_workflow_memberships:
                if membership.get("statework_id") == statework_id:
                    return membership
            membership = self.shared_workflow_membership
            if membership and membership.get("statework_id") == statework_id:
                return membership
            return None
        return self.shared_workflow_membership
    def _membership_current(self, statework_id: Optional[str] = None,
                            subject_ref: Optional[str] = None) -> bool:
        membership = self._membership_for(
            statework_id or self.current_segment.statework_id)
        if not membership:
            return True
        if self.membership_authority is None:
            return False
        if subject_ref is not None and membership.get("subject_ref") != subject_ref:
            return False
        return self.membership_authority.validate(
            membership, session=self, operation="transition")
    def _authoritative_state_ready(self) -> bool:
        if self.runtime_mode != "combined_enforcing":
            return True
        if self.state_authority is None or not self.bundle.stateworks:
            return False
        try:
            for statework in self.bundle.stateworks:
                statework_id = str(statework.get("id") or "")
                subject = str(statework.get("subject_ref")
                              or self.bundle.policy.subject_ref or "")
                if not subject:
                    raise ValueError(f"StateWork {statework_id!r} has no bound subject")
                contract_hash = str(statework.get("workflow_contract_hash") or "")
                if not contract_hash:
                    raise ValueError(f"StateWork {statework_id!r} has no pinned workflow contract")
                key = (statework_id, subject)
                inspect_workflow = getattr(self.state_authority, "inspect_workflow", None)
                if callable(inspect_workflow):
                    snapshot = inspect_workflow(
                        statework_id=statework_id, subject_ref=subject,
                        contract_hash=contract_hash)
                    current, revision = snapshot.current_state, snapshot.revision
                else:
                    legacy = self.state_authority.inspect(
                        statework_id=statework_id, subject_ref=subject,
                        contract_hash=contract_hash)
                    current, revision = legacy.get("state"), legacy.get("revision", 0)
                if current is not None:
                    self.statework_states[key] = str(current)
                object.__setattr__(
                    self, "authority_revision",
                    max(self.authority_revision, int(revision or 0)))
            return True
        except Exception as exc:
            self._emit_runtime({"category": "blocker", "event_type": "state_authority_unavailable",
                                "subject": self.bundle.policy.subject_ref,
                                "payload": {"summary": str(exc)[:512], "result_class": "blocked"}})
            return False
    def before_action(self, action: Dict[str, Any] | AgentActionRequest) -> GateDecision:
        """Compatibility gate for adapters that do not need result correlation."""
        return self.prepare_action(action).decision
    def preflight_action(self, action: Dict[str, Any] | AgentActionRequest
                         ) -> ActionPreflightResult:
        """Run CFW's local authority gate before DP evaluates action semantics.

        This does not create an action request, consume a confirmation grant,
        invoke tool hooks, or record the action as executed. The normal
        ``prepare_action`` call remains the final gate after DP actuation.
        """
        request = self._request(action)
        if (request.subject_ref == self.bundle.policy.subject_ref
                and not self._authoritative_state_ready()):
            decision = GateDecision.BLOCK
        elif not self._revocation_current() or not self._membership_current():
            decision = GateDecision.BLOCK
        else:
            decision = self._decision(request)
        return ActionPreflightResult(request.digest, decision)
    def prepare_action(self, action: Dict[str, Any] | AgentActionRequest) -> ActionPreparation:
        """Authorize an action and return its unique request ID for its eventual result."""
        return self._prepare_action(action)
    def _prepare_action(self, action: Dict[str, Any] | AgentActionRequest, *,
                        capability: Optional[_HostCapability] = None,
                        objective_binding: Optional[ActionObjectiveBinding] = None
                        ) -> ActionPreparation:
        request = self._request(action)
        binding: Optional[ActionObjectiveBinding] = None
        if objective_binding is not None:
            self._require_host(capability)
            binding = self._action_objective_bindings.pop(
                objective_binding.binding_digest, None)
            if binding != objective_binding or binding.action_digest != request.digest:
                raise PermissionError("objective binding is unissued, replayed, or for another action")
            if (binding.artifact_revision is not None
                    and binding.artifact_revision != self.metacognitive_state.artifact_revision):
                raise PermissionError("objective binding expired after an artifact revision change")
        action_request_id = f"action-request-{uuid.uuid4()}"
        if request.subject_ref == self.bundle.policy.subject_ref and not self._authoritative_state_ready():
            return ActionPreparation(action_request_id, GateDecision.BLOCK,
                                     self._descriptor(request), request.digest)
        if not self._revocation_current() or not self._membership_current():
            return ActionPreparation(action_request_id, GateDecision.BLOCK,
                                     self._descriptor(request), request.digest)
        descriptor = self._descriptor(request)
        decision = self._decision(request)
        strategy = self._action_strategy()
        candidate: Optional[ActivityFact] = None
        for hook in self.tool_hooks:
            hook_decision = hook({"tool_binding_id": request.binding_id,
                                  "arguments": dict(request.arguments),
                                  "subject_ref": request.subject_ref})
            if hook_decision is not None:
                if not isinstance(hook_decision, GateDecision):
                    raise ValueError("tool hooks must return GateDecision or None")
                decision = self._more_restrictive(decision, hook_decision)

        # System-1 sees this action only after every hard host check. Objective
        # accounting comes from an exact host-issued request binding; tool
        # descriptors continue to describe authority and capability only.
        if decision == GateDecision.ALLOW:
            target_ref = next((request.arguments.get(key) for key in ("target", "path", "resource")
                               if isinstance(request.arguments.get(key), str)), None)
            host_relation = binding.goal_relation if binding else "ambiguous"
            criterion_ids = binding.criterion_ids if binding else ()
            candidate = self.metacognitive_state.make_candidate(
                "action", source="host_observed",
                action_class=descriptor.action_class or descriptor.tool_type,
                host_declared_relation=host_relation,
                target_kind=descriptor.target_kind,
                target_scope=descriptor.target_scope,
                target_ref=target_ref,
                proposed_purpose=(binding.purpose_class if binding else None),
                proposed_purpose_source="host_observed",
                affected_criterion_ids=criterion_ids)
            goal = self.bundle.pinned.get("goal_contract") or {}
            semantic_relation = None
            semantic_influenced = False
            if goal:
                semantic_descriptor = replace(descriptor, goal_relation="ambiguous")
                self._system_one_query(
                    questions=("action_relation", "goal_drift"), candidate=candidate,
                    subject_ref=request.subject_ref, action_request_id=action_request_id,
                    descriptor=semantic_descriptor)
                semantic_decision = next((item for item in reversed(
                    tuple(self.system_one_decisions.values()))
                    if item.get("action_request_id") == action_request_id), None)
                if semantic_decision is not None:
                    semantic_relation = semantic_decision.get("fused_action_relation")
                    semantic_influenced = bool(semantic_decision.get("influenced"))
            if binding is None:
                if (semantic_influenced and semantic_relation in {
                        "required_verification", "diagnostic_support", "unrelated"}):
                    action_lane = "SUPPORT"
                else:
                    action_lane = "UNCLASSIFIED"
            else:
                action_lane = {
                    "DIRECT_GOAL": "DIRECT",
                    "BLOCKER_REMOVAL": "DIRECT",
                    "REQUIRED_VERIFICATION": "VERIFICATION",
                    "DIAGNOSTIC": "SUPPORT",
                    "INCIDENTAL": "SUPPORT",
                }[binding.purpose_class]
                if (semantic_influenced and semantic_relation
                        and semantic_relation != binding.goal_relation):
                    self._emit_runtime({
                        "category": "decision", "event_type": "objective_binding_disagreement",
                        "subject": request.subject_ref,
                        "payload": {
                            "summary": "semantic action meaning differs from host objective binding",
                            "result_class": "scrutiny_increased",
                            "binding_digest": binding.binding_digest,
                            "host_relation": binding.goal_relation,
                            "semantic_relation": str(semantic_relation),
                        }})
            verification = (goal.get("verification_policy") or {})
            if host_relation == "required_verification":
                violation = self.metacognitive_state.verification_limit_exceeded(
                    candidate, relation=host_relation,
                    max_actions=int(verification.get(
                        "max_verification_actions_without_artifact_change", 2) or 2),
                    max_repeats=int(verification.get(
                        "max_repeated_equivalent_verifications", 1) or 1))
                if violation:
                    decision = GateDecision.BLOCK
                    self._emit_runtime({
                        "category": "blocker", "event_type": "verification_action_limited",
                        "subject": request.subject_ref,
                        "payload": {"summary": violation, "result_class": "blocked"}})
            if (goal.get("work_mode") == "delivery" and self.convergence is not None
                    and host_relation not in {
                        "advances_primary_goal", "removes_concrete_blocker",
                        "required_verification"}
                    and self.convergence.needs_goal_reanchor(
                        cause_id="primary-objective",
                        support_limit=int(verification.get(
                            "support_actions_before_goal_reanchor", 5) or 5),
                        total_action_limit=int(verification.get(
                            "max_committed_actions_without_goal_progress", 15) or 15))):
                decision = GateDecision.REQUIRE_GOAL_REANCHOR

        if decision == GateDecision.ALLOW:
            self._pending_action_requests[action_request_id] = {
                "descriptor": descriptor.__dict__.copy(),
                "subject_ref": request.subject_ref,
                "sequence": self.system_one_sequence,
                "relation": host_relation,
                "accounting_lane": action_lane,
                "objective_binding": binding.to_dict() if binding else None,
                "candidate": candidate,
            }
            attempt_ref = str(request.arguments.get("attempt_ref") or "")
            if attempt_ref and attempt_ref in self._pending_retry_candidates:
                self._pending_action_requests[action_request_id]["retry_candidate"] = (
                    self._pending_retry_candidates.pop(attempt_ref))
            self._journal("action_request_prepared", action_request_id=action_request_id,
                          subject_ref=request.subject_ref, descriptor=descriptor.__dict__,
                          relation=self._pending_action_requests[action_request_id]["relation"])
        self._emit_runtime({"category": "action", "event_type": "action_requested",
                   "subject": request.subject_ref,
                   "payload": {"summary": decision.value,
                               "tool_type": descriptor.tool_type,
                               "route": request.binding_id,
                               "route_type": "tool_policy",
                               "action_request_id": action_request_id,
                               "advice_priority": strategy["priorities"].get(request.binding_id),
                               "policy_hash": strategy["policy_hash"]}})
        if self.lifecycle is not None:
            prior = set(self.active_lifecycle)
            self.lifecycle.on_action(decision, descriptor=descriptor)
            self.active_lifecycle = set(self.lifecycle.active)
            self.lifecycle_counts = dict(self.lifecycle.counts)
            self._emit_lifecycle_activations(prior)
        confirmation = self.host_context.confirmation_for(request, self._consumed_grant_ids)
        if decision == GateDecision.ALLOW and confirmation is not None:
            self._consumed_grant_ids.add(confirmation.grant_id)
            self._journal("confirmation_consumed", grant_id=confirmation.grant_id)
        if decision != GateDecision.ALLOW:
            self._pending_action_requests.pop(action_request_id, None)
        return ActionPreparation(action_request_id, decision, descriptor, request.digest)
    def before_retry(self, request: Mapping[str, Any] | AgentActionRequest) -> GateDecision:
        """Dispatch retry-specific structural handlers at the host boundary."""
        action = self._request(request)
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            # A terminated task must not be revived by a retry request.
            return GateDecision.BLOCK
        attempt_ref = str(action.arguments.get("attempt_ref") or "")
        attempt = self.attempt_descriptors.get(attempt_ref)
        if attempt is None:
            return GateDecision.REQUIRE_REINSPECTION
        semantic = self.attempt_semantics.get(attempt_ref)
        candidate = self.metacognitive_state.make_candidate(
            "retry", source="host_observed",
            action_class=(semantic.planned_action_class if semantic else "retry"),
            host_declared_relation="ambiguous",
            target_kind=(semantic.planned_target_kind if semantic else "attempt"),
            proposed_purpose=(semantic.hypothesis_summary if semantic else None),
            proposed_purpose_source=(semantic.source if semantic else "derived"),
            affected_criterion_ids=(semantic.affected_criterion_ids if semantic else ()))
        self._semantic_retry_same.discard(attempt_ref)
        if semantic is not None and self._system_one_policy().mode != "disabled":
            state = self.convergence.state(attempt.cause_id) if self.convergence else None
            deterministic_same = bool(state and
                state.action_fingerprint == attempt.action_plan_digest
                and state.hypothesis_fingerprint == attempt.premise_digest
                and state.premise_digest == attempt.premise_digest)
            judgment = None
            decision = None
            if not deterministic_same:
                judgment = self._system_one_query(
                    questions=("retry_relation",), candidate=candidate,
                    subject_ref=action.subject_ref)
                decision = (self.system_one_decisions.get(judgment.request_id)
                            if judgment else None)
            retry_relation = fuse_retry_relation(
                deterministic_same=deterministic_same, judgment=judgment,
                policy=self._system_one_policy(),
                calibrated=tuple((decision or {}).get("calibrated_question_ids") or ()))
            if retry_relation == "equivalent_retry" and not deterministic_same:
                # Inference can collapse novelty to the already-recorded branch;
                # it cannot create a new branch or reset any host budget.
                self._semantic_retry_same.add(attempt_ref)
        try:
            for enforcement in self.bundle.guard_pack.get("structural_handlers", {}).values():
                if enforcement.get("mode") != "structural":
                    continue
                handler = handler_for_enforcement(enforcement)
                if handler and "before_retry" in getattr(handler, "hooks", ()):
                    result = handler.before_retry(self, action, enforcement)
                    if result == "block":
                        if (self.convergence is not None
                                and self.convergence.state(attempt.cause_id).disposition == "BLOCKED"):
                            state = self.convergence.state(attempt.cause_id)
                            self.request_termination(
                                self._host_capability, disposition="BLOCKED",
                                reason="Autonomous recovery limit reached without primary-goal progress.",
                                cause_id=attempt.cause_id,
                                evidence_refs=([state.last_failure_evidence_ref]
                                               if state.last_failure_evidence_ref else ()))
                        return GateDecision.BLOCK
                    if result == "require_reinspection":
                        return GateDecision.REQUIRE_REINSPECTION
                    if result == "require_changed_hypothesis":
                        return GateDecision.REQUIRE_CHANGED_HYPOTHESIS
                    if result == "require_goal_reanchor":
                        return GateDecision.REQUIRE_GOAL_REANCHOR
            retry_relation = ("equivalent_retry" if attempt_ref in self._semantic_retry_same
                              else "different_fingerprint")
            self._pending_retry_candidates[attempt_ref] = replace(
                candidate, result_class=retry_relation)
            return GateDecision.ALLOW
        finally:
            self._semantic_retry_same.discard(attempt_ref)

    def _request(self, action: Mapping[str, Any] | AgentActionRequest) -> AgentActionRequest:
        return action if isinstance(action, AgentActionRequest) else AgentActionRequest.from_mapping(action)
    def _descriptor(self, action: Mapping[str, Any] | AgentActionRequest) -> ActionDescriptor:
        request = self._request(action)
        descriptor = self.action_registry.get(request.binding_id)
        if descriptor is None:
            # Unknown actions are not allowed to self-classify. The host can
            # explicitly register a safe descriptor when it knows the tool.
            return ActionDescriptor(request.binding_id or "unknown", untrusted=True,
                                    required_authority="operator")
        return descriptor.for_request(request)

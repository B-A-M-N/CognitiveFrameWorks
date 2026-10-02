"""Legal child-policy epoch validation and route preference methods."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import yaml
from runtime_contract.actions import ActionDescriptor, AgentActionRequest
from runtime_contract.convergence import TERMINAL_DISPOSITIONS
from runtime_session.host_authority import _HostCapability
from runtime_session.lifecycle import (
    ChildPolicyEpoch, PolicyEpochRecord, RouteReplanRecommendation,
)

from runtime_session.resolver_bridge import resolve_runtime


class RouteReplanningMethods:
    """Session-facing policy epoch validation for host-selected legal routes."""
    def _validate_child_route_bundle(self, route: str, target_statework_id: str,
                                      child: resolve_runtime.RuntimeBundle) -> None:
        """Require a resolver-produced child that changes only one legal flow."""
        pieces = route.split(":", 2)
        if len(pieces) != 3 or pieces[0] != "flow" or pieces[1] != target_statework_id:
            raise ValueError("route must identify the target StateWork flow")
        flow_name = pieces[2]
        if not flow_name or len(flow_name) > 64 or any(char in flow_name for char in "/\\\0"):
            raise ValueError("route flow name is malformed")
        if not isinstance(child, resolve_runtime.RuntimeBundle):
            raise ValueError("legal route must be represented by a resolved runtime bundle")
        resolve_runtime._policy_from_bundle(self.bundle)
        resolve_runtime._policy_from_bundle(child)
        if child.task_id != self.bundle.task_id or child.statework_root != self.bundle.statework_root:
            raise ValueError("child route changed task or StateWork source")

        before, after = self.bundle.policy.canonical(), child.policy.canonical()
        mutable_policy_fields = {"kernel", "capsules", "stateworks"}
        if set(before) != set(after) or any(
                before[key] != after[key] for key in before if key not in mutable_policy_fields):
            raise PermissionError("child route changed policy outside flow-derived content")
        for field_name in (
                "namespace_id", "application_id", "application_instance_id", "agent_id",
                "agent_instance_id", "attempt_id", "interaction_id", "parent_session_id",
                "subject_ref", "goal_contract", "goal_contract_hash", "system_one_policy",
                "policy_revocation_revision", "revocation_mode", "shared_workflow_membership",
                "contracts"):
            if self.bundle.pinned.get(field_name) != child.pinned.get(field_name):
                raise PermissionError(f"child route changed pinned {field_name}")
        if (self.bundle.guard_pack.get("guards") != child.guard_pack.get("guards")
                or self.bundle.guard_pack.get("structural_handlers")
                != child.guard_pack.get("structural_handlers")
                or self.bundle.guard_pack.get("routing_adjustments")
                != child.guard_pack.get("routing_adjustments")
                or self.bundle.guard_pack.get("routing_experiment")
                != child.guard_pack.get("routing_experiment")):
            raise PermissionError("child route changed host guards or routing authority")

        parent_stateworks = list(self.bundle.stateworks)
        child_stateworks = list(child.stateworks)
        if len(parent_stateworks) != len(child_stateworks):
            raise PermissionError("child route changed the required StateWork set")
        parent_target = child_target = None
        for left, right in zip(parent_stateworks, child_stateworks):
            if left.get("id") != right.get("id"):
                raise PermissionError("child route reordered or replaced StateWorks")
            if left.get("id") == target_statework_id:
                parent_target, child_target = left, right
                flow_derived_fields = {"selected_flow", "flow_integrations",
                                       "flow_metadata"}
                for key in set(left) | set(right):
                    if (key not in flow_derived_fields
                            and left.get(key) != right.get(key)):
                        raise PermissionError("child route changed a StateWork contract")
            elif left != right:
                raise PermissionError("child route changed a different StateWork")
        if parent_target is None or child_target is None:
            raise ValueError("route target is not required by the pinned task")
        selected = child_target.get("selected_flow")
        if not isinstance(selected, (list, tuple)) or len(selected) != 2:
            raise ValueError("child bundle has no pinned selected flow")
        relative_path, content_hash = str(selected[0]), str(selected[1])
        flow_path = Path(relative_path)
        if (flow_path.is_absolute() or ".." in flow_path.parts
                or flow_path.parts[:1] != (target_statework_id,)
                or flow_path.parent.name != flow_name):
            raise ValueError("child bundle did not resolve the selected legal flow")
        root = Path(child.statework_root).resolve()
        resolved_flow = (root / flow_path).resolve()
        try:
            resolved_flow.relative_to(root)
        except ValueError as exc:
            raise PermissionError("child flow escaped its StateWork source") from exc
        if not resolved_flow.is_file() or resolve_runtime.sha256(resolved_flow) != content_hash:
            raise resolve_runtime.SnapshotIntegrityError(
                "child flow content no longer matches its resolved snapshot")
    def _record_route_replan_blocker(self, *, target_statework_id: str,
                                     reason: str,
                                     legal_routes: Sequence[str] = ()) -> None:
        self._journal("route_replan_blocked", target_statework_id=target_statework_id,
                      reason=reason, legal_routes=sorted(legal_routes))
        self._emit_runtime({
            "category": "blocker", "event_type": "route_replan_blocked",
            "subject": self.current_segment.subject_ref,
            "payload": {"summary": reason[:256], "result_class": "blocked"}})
    def _rank_legal_routes(
            self, capability: _HostCapability, *,
            legal_children: Mapping[str, resolve_runtime.RuntimeBundle],
            state_revision: int, target_statework_id: str,
            route_preference: Optional[str] = None,
            preference_source: str = "metacognition"
            ) -> Optional[RouteReplanRecommendation]:
        self._require_host(capability)
        if self.external_behavior_authority:
            return None
        if self._evaluate_metacognition().mode != "REPLAN":
            return None
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot request a route replan")
        if self._pending_action_requests:
            raise PermissionError("route ranking requires an action boundary with no pending calls")
        if not isinstance(state_revision, int) or state_revision < 0:
            raise ValueError("current StateWork revision must be a non-negative integer")
        if not target_statework_id or len(target_statework_id) > 64:
            raise ValueError("route ranking requires a bounded StateWork ID")
        if not isinstance(legal_children, Mapping) or len(legal_children) > 16:
            raise ValueError("route ranking accepts at most sixteen host-resolved legal flows")
        if preference_source not in {"metacognition", "operator", "learned_profile"}:
            raise ValueError("unsupported deterministic route preference source")
        children = dict(legal_children)
        for route, child in children.items():
            if not isinstance(route, str):
                raise ValueError("legal flow route IDs must be strings")
            self._validate_child_route_bundle(route, target_statework_id, child)
        if not children:
            self._record_route_replan_blocker(
                target_statework_id=target_statework_id,
                reason="no host-resolved legal route is available")
            return None

        selected = route_preference
        source = preference_source
        semantic_decision_ref: Optional[str] = None
        confidence = 1.0
        if selected is not None:
            if selected not in children:
                raise ValueError("preferred route must be one of the host-resolved legal flows")
            if source == "learned_profile":
                adjustments = self.bundle.guard_pack.get("routing_adjustments", ())
                if not any(item.get("route_type") == "flow"
                           and item.get("disposition") == "prefer"
                           and item.get("route") in {selected, selected.split(":", 2)[-1]}
                           for item in adjustments):
                    raise PermissionError("learned route preference is not pinned in this policy")
        elif len(children) == 1:
            selected = next(iter(children))
            source = "metacognition"
        else:
            domains = set(self.bundle.pinned.get("domain_tags") or ())
            task_shape = self.bundle.pinned.get("task_shape")
            operation = self.bundle.pinned.get("operation")
            trigger = self.bundle.pinned.get("trigger")
            current_state = self.statework_states.get(
                (target_statework_id, self.current_segment.subject_ref or ""))
            flow_contracts: Dict[str, Mapping[str, Any]] = {}
            for route, child in children.items():
                pin = next((item for item in child.stateworks
                            if item.get("id") == target_statework_id), None)
                selected_flow = pin.get("selected_flow") if pin else None
                if not selected_flow:
                    continue
                flow_path = Path(child.statework_root) / str(selected_flow[0])
                parts = flow_path.read_text(encoding="utf-8").split("---", 2)
                metadata = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
                metadata = metadata if isinstance(metadata, Mapping) else {}
                flow_contracts[route] = metadata
            target_pin = next((item for item in self.bundle.stateworks
                               if item.get("id") == target_statework_id), None)
            flow_policy = dict((target_pin or {}).get("flow_selection") or {})
            selector = resolve_runtime._flow_selector(
                Path(self.bundle.statework_root))
            selection = selector.select(
                flow_contracts,
                {"operation": operation, "trigger": trigger,
                 "task_shape": task_shape, "phase": self.bundle.pinned.get("phase"),
                 "domain_tags": sorted(domains), "current_state": current_state},
                flow_policy)
            scores = dict(selection.scores)
            if not scores:
                self._record_route_replan_blocker(
                    target_statework_id=target_statework_id,
                    reason="no legal route matches the current selector context")
                return None
            top_score = max(scores.values())
            top_routes = sorted(route for route, score in scores.items()
                                if score == top_score)
            if len(top_routes) == 1:
                selected = top_routes[0]
                source = "metacognition"
            else:
                policy = self._system_one_policy()
                if policy.mode != "advisory" or self.system_one_judge is None:
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="legal routes are tied and no calibrated semantic resolver is available",
                        legal_routes=top_routes)
                    return None
                candidate = self.metacognitive_state.make_candidate(
                    "validation", source="host_observed", action_class="route_ranking",
                    host_declared_relation="ambiguous", target_kind="statework",
                    proposed_purpose="resolve equally specific legal StateWork flows",
                    proposed_purpose_source="host_observed")
                judgment = self._system_one_query(
                    questions=("route_preference",), candidate=candidate,
                    subject_ref=self.current_segment.subject_ref,
                    route_candidates=tuple(top_routes),
                    route_state_revision=state_revision,
                    route_statework_id=target_statework_id)
                if judgment is None:
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="semantic route resolution did not produce an answer",
                        legal_routes=top_routes)
                    return None
                decision = self.system_one_decisions.get(judgment.request_id) or {}
                answer = judgment.answer("route_preference")
                calibrated_ids = set(decision.get("calibrated_question_ids") or ())
                confidence = answer.calibrated_confidence if answer else None
                if ("route_preference" not in calibrated_ids or answer is None
                        or confidence is None
                        or confidence < policy.confidence_threshold("route_preference")
                        or answer.selected not in top_routes):
                    self._record_route_replan_blocker(
                        target_statework_id=target_statework_id,
                        reason="semantic route answer is not calibrated for the tied legal candidates",
                        legal_routes=top_routes)
                    return None
                selected = answer.selected
                source = "semantic_advisory"
                semantic_decision_ref = judgment.request_id
        assert selected is not None
        reason = {
            "metacognition": "deterministic_route_specificity",
            "operator": "operator_route_preference",
            "learned_profile": "learned_profile_route_preference",
            "semantic_advisory": "semantic_route_recommendation",
        }[source]
        if semantic_decision_ref:
            decision_id = semantic_decision_ref
            semantic_decision = self.system_one_decisions[decision_id]
            state_digest = semantic_decision["state_digest"]
            request_digest = semantic_decision["request_digest"]
            sequence = int(semantic_decision["sequence"])
        else:
            decision_id = self._record_route_decision(
                capability, route=selected, route_type="flow",
                selection_reason=f"policy_epoch:{reason}",
                event_type="route_replan_recommended")
            state_digest = hashlib.sha256(json.dumps({
                "metacognitive": self.metacognitive_state.snapshot(),
                "state_revision": state_revision,
                "legal_routes": sorted(children),
            }, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            request_digest = hashlib.sha256(json.dumps({
                "decision_id": decision_id, "route": selected, "source": source,
                "state_digest": state_digest,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            sequence = self.system_one_sequence
        recommendation = RouteReplanRecommendation(
            route=selected, decision_id=decision_id, source=source,
            source_state_digest=state_digest,
            request_digest=request_digest, sequence=sequence,
            confidence=float(confidence), state_revision=state_revision,
            target_statework_id=target_statework_id,
            source_metacognitive_revision=self.metacognitive_state.logical_revision,
            legal_routes_digest=hashlib.sha256(json.dumps(
                sorted(children), separators=(",", ":")).encode()).hexdigest(),
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
            legal_children=children,
            semantic_decision_ref=semantic_decision_ref)
        self._journal("route_replan_recommended", recommendation={
            "route": recommendation.route,
            "decision_id": recommendation.decision_id,
            "decision_source": recommendation.source,
            "semantic_decision_ref": recommendation.semantic_decision_ref,
            "source_state_digest": recommendation.source_state_digest,
            "request_digest": recommendation.request_digest,
            "sequence": recommendation.sequence,
            "confidence": recommendation.confidence,
            "state_revision": state_revision,
            "target_statework_id": target_statework_id,
            "legal_routes": sorted(children),
            "source_metacognitive_revision": recommendation.source_metacognitive_revision,
            "legal_routes_digest": recommendation.legal_routes_digest,
            "policy_hash": recommendation.policy_hash,
        })
        return recommendation
    def _create_child_policy_epoch(
            self, capability: _HostCapability,
            recommendation: RouteReplanRecommendation, *, selected_route: str,
            current_state_revision: int) -> ChildPolicyEpoch:
        self._require_host(capability)
        if not isinstance(recommendation, RouteReplanRecommendation):
            raise TypeError("child policy epoch requires a host route recommendation")
        if self.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot enter a child policy epoch")
        if self._pending_action_requests:
            raise PermissionError("child policy epoch requires no pending action calls")
        if (recommendation.source == "semantic_advisory"
                and recommendation.sequence != self.system_one_sequence):
            raise PermissionError("semantic route recommendation is stale after subsequent System-1 activity")
        if current_state_revision != recommendation.state_revision:
            raise PermissionError("StateWork revision changed after route ranking")
        if recommendation.source_metacognitive_revision != self.metacognitive_state.logical_revision:
            raise PermissionError("route recommendation is stale after metacognitive state changed")
        expected_routes_digest = hashlib.sha256(json.dumps(
            sorted(recommendation.legal_children), separators=(",", ":")).encode()).hexdigest()
        if (recommendation.legal_routes_digest != expected_routes_digest
                or recommendation.policy_hash != self.bundle.pinned.get("policy_hash")):
            raise PermissionError("route recommendation no longer matches its pinned policy and candidates")
        if selected_route not in recommendation.legal_children:
            raise PermissionError("host selected a route outside the resolved legal candidates")
        if recommendation.source == "semantic_advisory":
            decision = self.system_one_decisions.get(recommendation.decision_id)
            if (not decision or decision.get("policy_hash") != self.bundle.pinned.get("policy_hash")
                    or decision.get("request_digest") != recommendation.request_digest
                    or decision.get("state_digest") != recommendation.source_state_digest
                    or decision.get("sequence") != recommendation.sequence
                    or decision.get("route_state_revision") != current_state_revision
                    or decision.get("route_statework_id") != recommendation.target_statework_id
                    or tuple(decision.get("legal_routes") or ())
                    != tuple(sorted(recommendation.legal_children))):
                raise PermissionError("route recommendation no longer matches its semantic decision")
            answers = decision.get("answers", {})
            answer = answers.get("route_preference") if isinstance(answers, Mapping) else None
            if (not isinstance(answer, Mapping)
                    or answer.get("selected") != recommendation.route):
                raise PermissionError("route recommendation has no matching semantic answer")
        else:
            reasons = {
                "metacognition": "deterministic_route_specificity",
                "operator": "operator_route_preference",
                "learned_profile": "learned_profile_route_preference",
            }
            decision = self.route_decisions.get(recommendation.decision_id)
            if (recommendation.source not in reasons or not decision
                    or decision.get("policy_hash") != self.bundle.pinned.get("policy_hash")
                    or decision.get("route_type") != "flow"
                    or decision.get("route") != recommendation.route
                    or decision.get("selection_reason")
                    != f"policy_epoch:{reasons[recommendation.source]}"):
                raise PermissionError("route recommendation no longer matches its host decision")
        if recommendation.route not in recommendation.legal_children:
            raise PermissionError("route recommendation is outside the legal candidates")
        child_bundle = recommendation.legal_children[selected_route]
        self._validate_child_route_bundle(
            selected_route, recommendation.target_statework_id, child_bundle)
        child_hash = str(child_bundle.pinned.get("policy_hash") or "")
        parent_hash = str(self.bundle.pinned.get("policy_hash") or "")
        if not child_hash or child_hash == parent_hash:
            raise ValueError("child policy epoch must have a distinct policy hash")
        epoch_index = (self.policy_epoch_record.epoch_index + 1
                       if self.policy_epoch_record else 1)
        record = PolicyEpochRecord(
            epoch_index=epoch_index, parent_policy_hash=parent_hash,
            child_policy_hash=child_hash, reason="dynamic_replan",
            route_type="flow", selected_route=selected_route,
            decision_source=recommendation.source,
            decision_ref=recommendation.decision_id,
            semantic_decision_ref=recommendation.semantic_decision_ref,
            state_revision=current_state_revision)
        route_name = selected_route.split(":", 2)[2]
        route_decision_id = self._record_route_decision(
            capability, route=route_name, route_type="flow",
            selection_reason="dynamic_policy_epoch")
        self._journal("child_policy_epoch_created", policy_epoch=record.to_dict(),
                      route_decision_id=route_decision_id,
                      legal_routes=sorted(recommendation.legal_children),
                      recommendation_confidence=recommendation.confidence)
        self._host_event({
            "event_id": f"policy-epoch-{uuid.uuid4()}", "category": "decision",
            "event_type": "policy_epoch_created", "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": "host-created child policy epoch",
                        "route": route_name, "route_type": "flow",
                        "route_decision_id": route_decision_id,
                        "policy_hash": parent_hash,
                        "reason": "dynamic_replan"}})
        return ChildPolicyEpoch(record=record, bundle=child_bundle)

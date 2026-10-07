"""Legal child-policy epoch validation and route preference methods."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping, Optional, Sequence

from runtime_contract.actions import ActionDescriptor, AgentActionRequest
from runtime_contract.convergence import TERMINAL_DISPOSITIONS
from runtime_contract.workflow_port import WorkflowRouteRanking
from runtime_session.host_authority import _HostCapability
from runtime_session.standalone_route_policy import (
    ChildPolicyEpoch, PolicyEpochRecord, RoutePolicyContext,
    RouteReplanRecommendation, RouteSelectionUnavailable,
)

from runtime_session.resolver_bridge import resolve_runtime


class RouteReplanningMethods:
    """Session-facing policy epoch validation for host-selected legal routes."""
    def _validate_child_route_bundle(self, route_id: str,
                                      child: resolve_runtime.RuntimeBundle, *,
                                      expected_route_digest: Optional[str] = None) -> None:
        """Validate a child using neutral route pins and CFW-owned policy identity."""
        if not isinstance(route_id, str) or not route_id or len(route_id) > 192:
            raise ValueError("workflow route candidate ID is malformed")
        if not isinstance(child, resolve_runtime.RuntimeBundle):
            raise ValueError("legal route must be represented by a resolved runtime bundle")
        if child.task_id != self.bundle.task_id:
            raise ValueError("child route changed task identity")
        parent_cfw = self.bundle.cfw_policy_snapshot
        child_cfw = child.cfw_policy_snapshot
        parent_composite = self.bundle.composite_snapshot
        child_composite = child.composite_snapshot
        if (parent_cfw is None or child_cfw is None
                or parent_composite is None or child_composite is None):
            raise PermissionError("route candidates require pinned CFW and composite snapshots")
        if (parent_cfw.policy_hash != child_cfw.policy_hash
                or parent_composite.cfw_policy_hash != parent_cfw.policy_hash
                or child_composite.cfw_policy_hash != child_cfw.policy_hash):
            raise PermissionError("child workflow route changed CFW-owned policy")
        for candidate in (self.bundle, child):
            cfw_snapshot = candidate.cfw_policy_snapshot
            composite_snapshot = candidate.composite_snapshot
            pinned_cfw = candidate.pinned.get("cfw_policy_snapshot")
            pinned_composite = candidate.pinned.get("composite_snapshot")
            if (candidate.pinned.get("cfw_policy_hash") != cfw_snapshot.policy_hash
                    or not isinstance(pinned_cfw, Mapping)
                    or pinned_cfw.get("cfw_policy_hash") != cfw_snapshot.policy_hash
                    or pinned_composite != composite_snapshot.to_dict()):
                raise resolve_runtime.SnapshotIntegrityError(
                    "route candidate snapshot does not match its immutable pins")
        stable_composite_fields = (
            "csw_registry_hash", "host_goal_contract_hash",
            "model_harness_toolset_hash", "dp_policy_contract_hash",
            "laya_identity_hash",
        )
        if any(getattr(parent_composite, name) != getattr(child_composite, name)
               for name in stable_composite_fields):
            raise PermissionError("child route changed unrelated composite runtime identity")
        actual_digest = child.workflow_route_contract_digests.get(route_id)
        if (actual_digest is None or (expected_route_digest is not None
                                      and actual_digest != expected_route_digest)):
            raise resolve_runtime.SnapshotIntegrityError(
                "child route differs from the workflow owner's pinned route contract")

        for field_name in (
                "namespace_id", "application_id", "application_instance_id", "agent_id",
                "agent_instance_id", "attempt_id", "interaction_id", "parent_session_id",
                "subject_ref", "goal_contract", "goal_contract_hash",
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
    def _record_route_replan_blocker(self, *, target_workflow_id: str,
                                     reason: str,
                                     legal_routes: Sequence[str] = ()) -> None:
        self._journal("route_replan_blocked", target_workflow_id=target_workflow_id,
                      reason=reason, legal_routes=sorted(legal_routes))
        self._emit_runtime({
            "category": "blocker", "event_type": "route_replan_blocked",
            "subject": self.lifecycle_workflow.current_segment.subject_ref,
            "payload": {"summary": reason[:256], "result_class": "blocked"}})
    def _rank_legal_routes(
            self, capability: _HostCapability, *,
            legal_children: Mapping[str, resolve_runtime.RuntimeBundle],
            state_revision: int, target_workflow_id: str,
            route_preference: Optional[str] = None,
            preference_source: str = "metacognition"
            ) -> Optional[RouteReplanRecommendation]:
        self._require_host(capability)
        if self.behavior.is_external:
            return None
        if self._evaluate_standalone_behavior().mode != "REPLAN":
            return None
        if self.lifecycle_workflow.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot request a route replan")
        if self.actions.pending_action_requests:
            raise PermissionError("route ranking requires an action boundary with no pending calls")
        if type(state_revision) is not int or state_revision < 0:
            raise ValueError("current workflow revision must be a non-negative integer")
        if not isinstance(target_workflow_id, str) or not target_workflow_id \
                or len(target_workflow_id) > 128:
            raise ValueError("route ranking requires a bounded workflow ID")
        if not isinstance(legal_children, Mapping) or len(legal_children) > 16:
            raise ValueError("route ranking accepts at most sixteen resolved candidates")
        children = dict(legal_children)
        for route_id, child in children.items():
            if not isinstance(route_id, str):
                raise ValueError("workflow route candidate IDs must be strings")
            self._validate_child_route_bundle(route_id, child)

        workflow_port = self.identity_authority.authorities.workflow_port
        ranker = getattr(workflow_port, "rank_legal_routes", None)
        if not callable(ranker):
            self._record_route_replan_blocker(
                target_workflow_id=target_workflow_id,
                reason="workflow authority cannot rank legal routes")
            return None
        runtime_identity = dict(self.bundle.workflow_runtime_identity)
        workflow_context = dict(self.bundle.workflow_task_context)
        if not runtime_identity or not workflow_context:
            raise PermissionError("route ranking requires the task-scoped workflow context")
        ranking = ranker(
            runtime_identity=runtime_identity, task_context=workflow_context,
            workflow_id=target_workflow_id,
            candidate_routes=tuple(sorted(children)),
            expected_workflow_revision=state_revision)
        if isinstance(ranking, Mapping):
            ranking = WorkflowRouteRanking(**dict(ranking))
        if not isinstance(ranking, WorkflowRouteRanking):
            raise TypeError("workflow authority returned an invalid route ranking")
        legal_routes = ranking.legal_routes
        route_digests = dict(ranking.route_contract_digests)
        route_scores = dict(ranking.scores)
        registry_hash = ranking.registry_hash
        if (ranking.workflow_id != target_workflow_id
                or ranking.workflow_revision != state_revision
                or not set(legal_routes).issubset(children)):
            raise PermissionError("workflow authority returned an invalid route ranking")
        for route in legal_routes:
            self._validate_child_route_bundle(
                route, children[route], expected_route_digest=route_digests[route])
        children = {route: children[route] for route in legal_routes}
        if not children:
            self._record_route_replan_blocker(
                target_workflow_id=target_workflow_id,
                reason="no workflow-authorized legal route is available")
            return None

        route_policy = self.ensure_standalone_behavior_authority().route_policy
        try:
            selection = route_policy.select(
                tuple(children), RoutePolicyContext(
                    route_preference=route_preference,
                    preference_source=preference_source,
                    scores=route_scores))
        except RouteSelectionUnavailable as error:
            self._record_route_replan_blocker(
                target_workflow_id=target_workflow_id,
                reason=error.reason, legal_routes=error.legal_routes)
            return None
        selected = selection.route
        source = selection.source
        reason = selection.reason
        confidence = selection.confidence
        attention = self._standalone_attention()
        behavior_revision = (self.behavior.task_evidence.logical_revision
                             + attention.revision)
        decision_id = self._record_route_decision(
            capability, route=selected, route_type="flow",
            selection_reason=f"policy_epoch:{reason}",
            event_type="route_replan_recommended")
        state_digest = hashlib.sha256(json.dumps({
            "metacognitive": self.behavior.task_evidence.snapshot(),
            "standalone_attention": attention.snapshot(),
            "state_revision": state_revision,
            "legal_routes": sorted(children),
            "workflow_registry_hash": registry_hash,
            "route_contract_digests": {
                route: route_digests[route] for route in sorted(children)},
        }, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        request_digest = hashlib.sha256(json.dumps({
            "decision_id": decision_id, "route": selected, "source": source,
            "state_digest": state_digest,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        sequence = behavior_revision
        recommendation = RouteReplanRecommendation(
            route=selected, decision_id=decision_id, source=source,
            source_state_digest=state_digest,
            request_digest=request_digest, sequence=sequence,
            confidence=float(confidence), state_revision=state_revision,
            workflow_ranking=ranking,
            source_metacognitive_revision=behavior_revision,
            legal_routes_digest=hashlib.sha256(json.dumps(
                sorted(children), separators=(",", ":")).encode()).hexdigest(),
            policy_hash=str(self.bundle.pinned.get("policy_hash") or ""),
            workflow_registry_hash=registry_hash,
            route_contract_digests={route: route_digests[route]
                                    for route in legal_routes},
            legal_children=children,
            semantic_decision_ref=None)
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
            "target_workflow_id": target_workflow_id,
            "legal_routes": sorted(children),
            "workflow_registry_hash": recommendation.workflow_registry_hash,
            "route_contract_digests": dict(recommendation.route_contract_digests),
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
        route_policy = self.behavior.standalone_route_policy
        if route_policy is None:
            raise PermissionError("child policy epochs require standalone CFW route policy")
        if not isinstance(recommendation, RouteReplanRecommendation):
            raise TypeError("child policy epoch requires a host route recommendation")
        if self.lifecycle_workflow.termination_disposition in TERMINAL_DISPOSITIONS:
            raise PermissionError("a terminated task cannot enter a child policy epoch")
        if self.actions.pending_action_requests:
            raise PermissionError("child policy epoch requires no pending action calls")
        if recommendation.source not in {"metacognition", "operator"}:
            raise PermissionError(
                "standalone CFW route epochs accept only deterministic or operator decisions")
        if current_state_revision != recommendation.state_revision:
            raise PermissionError("workflow revision changed after route ranking")
        revalidate = getattr(self.identity_authority.authorities.workflow_port, "validate_route_ranking", None)
        if not callable(revalidate):
            raise PermissionError("workflow authority cannot revalidate its route ranking")
        if not revalidate(
                runtime_identity=dict(self.bundle.workflow_runtime_identity),
                task_context=dict(self.bundle.workflow_task_context),
                ranking=recommendation.workflow_ranking):
            raise PermissionError("workflow route ranking changed before policy epoch creation")
        current_behavior_revision = (
            self.behavior.task_evidence.logical_revision
            + self._standalone_attention().revision)
        if recommendation.source_metacognitive_revision != current_behavior_revision:
            raise PermissionError("route recommendation is stale after metacognitive state changed")
        expected_routes_digest = hashlib.sha256(json.dumps(
            sorted(recommendation.legal_children), separators=(",", ":")).encode()).hexdigest()
        if (recommendation.legal_routes_digest != expected_routes_digest
                or recommendation.policy_hash != self.bundle.pinned.get("policy_hash")):
            raise PermissionError("route recommendation no longer matches its pinned policy and candidates")
        if selected_route not in recommendation.legal_children:
            raise PermissionError("host selected a route outside the resolved legal candidates")
        reasons = {
            "metacognition": "deterministic_route_specificity",
            "operator": "operator_route_preference",
        }
        decision = self.actions.route_decisions.get(recommendation.decision_id)
        if (not decision
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
            selected_route, child_bundle,
            expected_route_digest=recommendation.route_contract_digests.get(selected_route))
        child_hash = str(child_bundle.pinned.get("policy_hash") or "")
        parent_hash = str(self.bundle.pinned.get("policy_hash") or "")
        if not child_hash or child_hash == parent_hash:
            raise ValueError("child policy epoch must have a distinct policy hash")
        epoch_index = (route_policy.policy_epoch_record.epoch_index + 1
                       if route_policy.policy_epoch_record else 1)
        record = PolicyEpochRecord(
            epoch_index=epoch_index, parent_policy_hash=parent_hash,
            child_policy_hash=child_hash, reason="dynamic_replan",
            route_type="flow", selected_route=selected_route,
            decision_source=recommendation.source,
            decision_ref=recommendation.decision_id,
            semantic_decision_ref=recommendation.semantic_decision_ref,
            state_revision=current_state_revision)
        route_decision_id = self._record_route_decision(
            capability, route=selected_route, route_type="flow",
            selection_reason="dynamic_policy_epoch")
        self._journal("child_policy_epoch_created", policy_epoch=record.to_dict(),
                      route_decision_id=route_decision_id,
                      legal_routes=sorted(recommendation.legal_children),
                      recommendation_confidence=recommendation.confidence)
        self._host_event({
            "event_id": f"policy-epoch-{uuid.uuid4()}", "category": "decision",
            "event_type": "policy_epoch_created", "subject": self.bundle.policy.subject_ref,
            "payload": {"summary": "host-created child policy epoch",
                        "route": selected_route, "route_type": "flow",
                        "route_decision_id": route_decision_id,
                        "policy_hash": parent_hash,
                        "reason": "dynamic_replan"}})
        return ChildPolicyEpoch(record=record, bundle=child_bundle)

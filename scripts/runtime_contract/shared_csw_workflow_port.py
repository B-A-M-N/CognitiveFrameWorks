"""Host-owned adapter joining CSW planning and workflow authority APIs."""
from __future__ import annotations

from typing import Any, Mapping

from .workflow_port import ProviderWorkflowPlanner


class SharedCSWWorkflowPort:
    """Expose one task-scoped CSW authority through CFW's workflow contract."""

    def __init__(self, provider: Any, state_authority: Any,
                 authority: Any) -> None:
        self.authority = authority
        self.planner = ProviderWorkflowPlanner(
            provider, state_authority=state_authority)

    @staticmethod
    def _scope(identity: Mapping[str, Any]) -> dict[str, str]:
        return {name: str(identity[name]) for name in (
            "namespace_id", "application_id", "application_instance_id",
            "workflow_id", "subject_ref")}

    def snapshot(self, *, runtime_identity: Mapping[str, Any],
                 task_context: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.authority.behavioral_snapshot(
            workflow_scope=self._scope(runtime_identity),
            candidate_statework_ids=task_context.get("candidate_statework_ids"))

    def active_guidance(self, *, runtime_identity: Mapping[str, Any],
                        task_context: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self.snapshot(runtime_identity=runtime_identity,
                                 task_context=task_context)
        return {"revision": int(snapshot.get("revision", 0)), "guidance": []}

    def apply_step(self, *, runtime_identity: Mapping[str, Any],
                   step: Mapping[str, Any]) -> Mapping[str, Any]:
        statework_id = str(step["statework_id"])
        contract = self.authority.workflow_contract_identity(statework_id)
        identity = {**self._scope(runtime_identity), "statework_id": statework_id,
                    "contract_version": contract["contract_version"]}
        result = self.authority.apply_step(
            identity, contract_hash=contract["contract_hash"],
            step_id=str(step["step_id"]), evidence_refs=step.get("evidence_refs", ()),
            expected_revision=int(step["expected_revision"]))
        return {"accepted": bool(result.allowed), "reason": str(result.reason)}

    def eligible_routes(self, **kwargs: Any) -> Any:
        return self.planner.eligible_routes(**kwargs)

    def resolve_workflows(self, **kwargs: Any) -> Any:
        return self.planner.resolve_workflows(**kwargs)

    def rank_legal_routes(self, **kwargs: Any) -> Any:
        return self.planner.rank_legal_routes(**kwargs)

    def validate_route_ranking(self, **kwargs: Any) -> Any:
        return self.planner.validate_route_ranking(**kwargs)

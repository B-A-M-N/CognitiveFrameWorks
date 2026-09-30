"""CFW-only projection of bounded, typed execution state for System-1."""
from __future__ import annotations

from collections import deque
import hashlib
import json
from typing import Any, Mapping, Optional
import uuid

from .system_one import SystemOneFrame, SystemOneIdentity, SystemOnePolicy


class SystemOneStateProjector:
    def __init__(self, *, activity_limit: int = 12, fact_limit: int = 12) -> None:
        if not 1 <= activity_limit <= 12 or not 1 <= fact_limit <= 12:
            raise ValueError("System-1 projection limits exceed the safe bound")
        self.activity = deque(maxlen=activity_limit)
        self.csw_facts = deque(maxlen=fact_limit)

    def record_activity(self, category: str, code: str) -> None:
        if category not in {"action", "tool_result", "validation", "state_transition", "retry"}:
            raise ValueError("activity category is not in the typed projection")
        if not isinstance(code, str) or not code or len(code) > 128:
            raise ValueError("activity code must be bounded")
        self.activity.append(f"{category}:{code}")

    def record_csw_fact(self, statework_id: str, subject_ref: str, input_state: str,
                        output_state: str, progress_class: str) -> None:
        values = (statework_id, subject_ref, input_state, output_state, progress_class)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("CSW fact fields must be bounded typed values")
        subject_digest = hashlib.sha256(subject_ref.encode("utf-8")).hexdigest()[:16]
        compact = (f"{statework_id[:32]}:{subject_digest}:{input_state[:20]}:"
                   f"{output_state[:20]}:{progress_class[:24]}")
        self.csw_facts.append(compact)

    def project(self, *, identity: SystemOneIdentity, sequence: int,
                policy_hash: str, policy: SystemOnePolicy,
                questions: tuple[str, ...], goal_contract: Mapping[str, Any],
                convergence: Mapping[str, int], candidate: Mapping[str, str],
                route_candidates: tuple[str, ...] = ()) -> SystemOneFrame:
        objective = str(goal_contract.get("objective") or "")[:1024]
        criteria = tuple((str(item.get("criterion_id") or "")[:128],
                           str(item.get("claim") or "")[:256])
                         for item in list(goal_contract.get("acceptance_criteria") or ())[:8])
        safe_convergence = tuple(sorted((str(key)[:128], max(0, int(value)))
                                        for key, value in convergence.items()))[:8]
        safe_candidate = tuple(sorted(
            (str(key)[:128], str(value)[:256])
            for key, value in candidate.items() if str(value).strip()))[:12]
        activity = tuple(self.activity)
        csw_facts = tuple(self.csw_facts)
        # Sequence/request identifiers are correlation metadata, not state.
        state = {"objective": objective, "criteria": criteria,
                 "recent_activity": activity, "convergence": safe_convergence,
                 "csw_facts": csw_facts, "candidate": safe_candidate,
                 "questions": tuple(questions), "route_candidates": route_candidates}
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                            default=list).encode("utf-8")).hexdigest()
        return SystemOneFrame(
            identity=identity, request_id=f"system-one-{uuid.uuid4()}", sequence=sequence,
            state_digest=digest, policy_hash=policy_hash, backend_id=policy.backend_id,
            model_id=policy.model_id, calibration_hash=policy.calibration_hash,
            protocol_hash=policy.protocol_hash,
            questions=questions, objective=objective, criteria=criteria,
            recent_activity=activity, convergence=safe_convergence, csw_facts=csw_facts,
            candidate=safe_candidate, route_candidates=route_candidates)


__all__ = ["SystemOneStateProjector"]

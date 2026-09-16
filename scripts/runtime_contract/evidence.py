"""Task-local evidence and immutable state-segment contracts."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Dict, List, Optional


def completion_claim_digest(task_id: str, subject_ref: Optional[str],
                            boundary_type: str, boundary_target: str,
                            expected_validator: str, invocation_id: str) -> str:
    payload = {"task_id": task_id, "subject_ref": subject_ref,
               "boundary_type": boundary_type,
               "boundary_target": boundary_target,
               "expected_validator": expected_validator,
               "invocation_id": invocation_id}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompletionClaim:
    claim_id: str
    task_id: str
    subject_ref: Optional[str]
    boundary_type: str
    boundary_target: str
    expected_validator: str
    invocation_id: str
    claim_digest: str

    @classmethod
    def create(cls, *, claim_id: str, task_id: str, subject_ref: Optional[str],
               boundary_type: str, boundary_target: str,
               expected_validator: str, invocation_id: str) -> "CompletionClaim":
        return cls(claim_id=claim_id, task_id=task_id, subject_ref=subject_ref,
                   boundary_type=boundary_type, boundary_target=boundary_target,
                   expected_validator=expected_validator,
                   invocation_id=invocation_id,
                   claim_digest=completion_claim_digest(
                       task_id, subject_ref, boundary_type, boundary_target,
                       expected_validator, invocation_id))


@dataclass(frozen=True)
class CompletionPermit:
    task_id: str
    policy_hash: str
    subject_ref: Optional[str]
    claim_digest: str
    evidence_refs: tuple[str, ...]
    issued_at: str
    session_nonce: str
    authority_revision: int = 0
    boundary_type: str = "completion"
    boundary_target: str = "task"
    expected_validator: str = "completion-boundary-v1"


@dataclass(frozen=True)
class EvidenceAttestation:
    """Host-issued provenance for an observation.

    Agents may request work, but they do not construct attestations.  The
    host adapter derives ``evidence_type`` and records the source event and
    invocation that produced it.
    """

    evidence_id: str
    task_id: str
    subject_ref: Optional[str]
    evidence_type: str
    issuer: str
    source_event_id: str
    invocation_id: Optional[str]
    observed_at: str
    content_digest: str
    invalidated: bool = False
    claim_digest: Optional[str] = None


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    task_id: str
    kind: str
    subject_ref: Optional[str]
    observed_at: str
    invalidated: bool = False
    event_id: Optional[str] = None
    issuer: Optional[str] = None
    source_event_id: Optional[str] = None
    invocation_id: Optional[str] = None
    content_digest: Optional[str] = None
    claim_digest: Optional[str] = None


@dataclass(frozen=True)
class StateSegment:
    """Immutable observed state segment; it never rewrites task policy."""
    segment_id: str
    task_id: str
    subject_ref: str
    input_state: str
    output_state: str
    evidence_refs: tuple[str, ...]
    recorded_at: str
    statework_id: Optional[str] = None


class EvidenceLedger:
    """Single task-local source of truth for transition/completion evidence."""

    def __init__(self, task_id: str, *, writer_token: Any = None) -> None:
        self.task_id = task_id
        self._writer_token = writer_token if writer_token is not None else object()
        self._records: Dict[str, EvidenceRecord] = {}

    def record(self, **_: Any) -> str:
        """Reject the old caller-controlled insertion path.

        Keeping this method as an explicit failure makes accidental use in a
        host integration visible instead of silently preserving the old
        self-certification vulnerability.
        """
        raise PermissionError("evidence must be recorded as a host attestation")

    def record_attestation(self, attestation: EvidenceAttestation, *,
                           writer_token: Any = None) -> str:
        if writer_token is not self._writer_token:
            raise PermissionError("only the trusted runtime host adapter may record attestations")
        if attestation.task_id != self.task_id:
            raise ValueError("evidence attestation belongs to another task")
        if not attestation.evidence_type or not attestation.issuer:
            raise ValueError("evidence attestation requires type and issuer")
        if not (attestation.issuer.startswith("host:")
                or attestation.issuer.startswith("validator:")):
            raise PermissionError("evidence issuer is not a trusted host channel")
        if attestation.evidence_type in {"boundary_evidence", "completion_boundary"}:
            if not attestation.issuer.startswith("validator:") or not attestation.invocation_id:
                raise PermissionError("completion evidence requires a validator invocation")
        if not attestation.source_event_id:
            raise ValueError("evidence attestation requires a source event")
        if not attestation.content_digest:
            raise ValueError("evidence attestation requires a content digest")
        record = EvidenceRecord(
            evidence_id=attestation.evidence_id,
            task_id=attestation.task_id,
            kind=attestation.evidence_type,
            subject_ref=attestation.subject_ref,
            observed_at=attestation.observed_at,
            invalidated=attestation.invalidated,
            event_id=attestation.source_event_id,
            issuer=attestation.issuer,
            source_event_id=attestation.source_event_id,
            invocation_id=attestation.invocation_id,
            content_digest=attestation.content_digest,
            claim_digest=attestation.claim_digest,
        )
        prior = self._records.get(attestation.evidence_id)
        if prior is not None and prior != record:
            raise ValueError(f"conflicting evidence id {attestation.evidence_id!r}")
        self._records[attestation.evidence_id] = record
        return attestation.evidence_id

    def invalidate(self, evidence_id: str, *, writer_token: Any = None) -> None:
        if writer_token is not self._writer_token:
            raise PermissionError("only the trusted runtime host adapter may invalidate evidence")
        record = self._records.get(evidence_id)
        if record is None:
            raise ValueError(f"unknown evidence id {evidence_id!r}")
        self._records[evidence_id] = replace(record, invalidated=True)

    def resolve(self, refs: List[str], *, subject_ref: Optional[str] = None,
                fresh_after: Optional[str] = None,
                allowed_kinds: Optional[set[str]] = None) -> List[Dict[str, str | bool]]:
        if not refs or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("evidence_refs must be existing ledger IDs")
        resolved: List[Dict[str, str | bool]] = []
        for ref in refs:
            record = self._records.get(ref)
            if record is None:
                raise ValueError(f"unknown evidence id {ref!r}")
            if record.task_id != self.task_id or record.invalidated:
                raise ValueError(f"evidence id {ref!r} is not current for this task")
            if subject_ref is not None and record.subject_ref != subject_ref:
                raise ValueError(f"evidence id {ref!r} is for another subject")
            if fresh_after is not None and record.observed_at <= fresh_after:
                raise ValueError(f"evidence id {ref!r} predates the required reinspection")
            if allowed_kinds is not None and record.kind not in allowed_kinds:
                raise ValueError(f"evidence id {ref!r} has kind {record.kind!r}, expected {sorted(allowed_kinds)}")
            resolved.append({"kind": record.kind, "ref": record.evidence_id,
                             "subject_ref": record.subject_ref or subject_ref or "",
                             "observed_at": record.observed_at,
                             "invalidated": record.invalidated})
        return resolved

    def get(self, evidence_id: str) -> Optional[EvidenceRecord]:
        return self._records.get(evidence_id)

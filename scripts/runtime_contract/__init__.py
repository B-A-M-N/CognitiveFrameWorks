"""Small runtime-contract boundaries used by the host adapter and resolver.

The command-line resolver remains a thin entrypoint; policy, StateWork,
evidence, and telemetry contracts live behind these importable modules.
"""

from .actions import ActionDescriptor, AgentActionRequest, ConfirmationGrant, HostExecutionContext
from .evidence import (CompletionClaim, CompletionPermit, EvidenceAttestation,
                       EvidenceLedger, EvidenceRecord, StateSegment,
                       completion_claim_digest)
from .validators import VALIDATOR_REGISTRY, evaluate_validator, validator_registry_hash
from .handlers import SUPPORTED_HANDLERS, enforcement_for_guard, validate_enforcement
from .stateworks import ExecutionSegment, SegmentStatus
from .task import TaskRequest

__all__ = [
    "ActionDescriptor", "AgentActionRequest", "ConfirmationGrant", "HostExecutionContext",
    "EvidenceAttestation", "EvidenceLedger", "EvidenceRecord", "StateSegment",
    "CompletionClaim", "CompletionPermit", "completion_claim_digest",
    "VALIDATOR_REGISTRY", "evaluate_validator", "validator_registry_hash",
    "SUPPORTED_HANDLERS", "enforcement_for_guard", "validate_enforcement",
    "ExecutionSegment",
    "SegmentStatus",
    "TaskRequest",
]

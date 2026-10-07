"""Compatibility exports for CFW's task-local evidence ledger.

Global adaptive behavior and semantic interpretation belong to DP. Active CFW
code imports :mod:`runtime_contract.task_evidence`; this module remains for
legacy journal readers and integrations during migration.
"""
from .task_evidence import (BlockerFact, CompletionReadiness,
                            CriterionEvidenceBinding, TaskEvidenceState,
                            VerificationObligation)

# Old artifacts and offline integrations resolve this symbol to the local fact
# ledger. It carries no metacognitive controller or semantic provider state.
MetacognitiveState = TaskEvidenceState

__all__ = [
    "BlockerFact", "CompletionReadiness", "CriterionEvidenceBinding",
    "MetacognitiveState", "TaskEvidenceState", "VerificationObligation",
]

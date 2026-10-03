#!/usr/bin/env python3
"""Task-local CFW enforcement and component runtime.

The CLI resolver is a debugging front end; the actual agent/harness calls the
Python API. CFW owns its component lifecycle, action authorization, and
evidence-bound enforcement in standalone mode. When DigitalPsychology is
enabled, DP owns whole-session behavioral policy and CFW exposes its components
through the explicit adapter in ``scripts/digital_psychology_adapter.py``.

    TaskRequest -> resolve() -> RuntimeBundle -> runtime.start(bundle)

`start()` injects the selected compact runtime capsules into the model
context and registers tool/action hooks plus a final-output hook. The
bundles are immutable, per-task, content-addressed snapshots in
$XDG_RUNTIME_DIR/cognitiveframeworks/<task>/<policy-hash>.json.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import types
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONTRACT_ROOT = ROOT / "contracts"
if not LOCAL_CONTRACT_ROOT.exists():
    LOCAL_CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.actions import (
    ActionDescriptor, ActionObjectiveBinding, ActionStrategy, AgentActionRequest,
    ConfirmationGrant, HostExecutionContext,
    ProtectedResourcePolicy, action_contract_hash, action_digest,
    default_action_registry, effective_action_registry,
)
from runtime_contract.evidence import (CompletionClaim, CompletionPermit,
                                        EvidenceAttestation, EvidenceLedger,
                                        EvidenceRecord, StateSegment, TerminationPermit,
                                        completion_claim_digest)
from runtime_contract.convergence import (AttemptDescriptor, ConvergenceController,
                                           ConvergenceIdentity, SharedBranchLedger,
                                           TERMINAL_DISPOSITIONS)
from runtime_contract.component_control import (COMPONENT_BASELINE_MODES,
                                                COMPONENT_IDS,
                                                COMPONENT_MODEL_VISIBILITY,
                                                ComponentOverlay,
                                                PreparedComponentOverlay)
from runtime_contract.telemetry import DurableTelemetryExporter
from runtime_contract.handlers import enforcement_for_guard, handler_for_enforcement
from runtime_contract.operator_events import OperatorEvent
from runtime_contract.stateworks import ExecutionSegment, SegmentStatus
from runtime_contract.behavioral_fusion import (
    SemanticFusion, fuse_action_relation, fuse_progress_relation,
    fuse_retry_relation, render_metacognitive_directive,
)
from runtime_contract.metacognition import (
    CompletionReadiness, CriterionEvidenceBinding, MetaDecision, MetacognitiveController,
    MetacognitiveState, SemanticSignalStore, SemanticSignals,
)
from runtime_contract.system_one import (ActivityFact, AttemptSemanticDescriptor,
                                         BehavioralJudge, SystemOneCapability,
                                         SystemOneFrame, SystemOneIdentity,
                                         SystemOneJudgment, SystemOnePolicy,
                                         QUESTION_REGISTRY)
from runtime_contract.system_one_state import SystemOneStateProjector
from runtime_contract.validators import evaluate_validator, validator_registry_hash
from runtime_session.telemetry import HostTelemetry, OPTIONAL_EVENT_FIELDS
from runtime_session.projection import context_for
from runtime_session.external_behavior import ExternalBehaviorMethods
from runtime_session.segments import SessionLifecycleMethods
from runtime_session.route_replanning import RouteReplanningMethods
from runtime_session.completion import CompletionMethods
from runtime_session.attempts import AttemptMethods
from runtime_session.evidence import AUTHORITATIVE_EVIDENCE_KINDS, EvidenceMethods
from runtime_session.action_gate import ActionGateMethods
from runtime_session.standalone_behavior import StandaloneBehaviorMethods, _sispis_calibration
from runtime_session.host_authority import (
    HostIngress, PolicyRevocationAuthority, SharedWorkflowMembershipAuthority,
    StateAuthority, _HostCapability, create_state_authority,
)
from runtime_session.lifecycle import (
    ActionPreflightResult, ActionPreparation, ChildPolicyEpoch, ExecutionIdentity,
    GateDecision, LifecycleController, PolicyEpochRecord,
    RouteReplanRecommendation, SharedWorkflowMembership,
    _canonical_system_one_status,
)
from runtime_session.resolver_bridge import resolve_runtime

from runtime_session.session import RuntimeSession, TelemetrySink
from runtime_session.journal import _journal_identity_prefix, _journal_records, _restore_session
from runtime_session.start import DEFAULT_ACTION_REGISTRY, RUNTIME_MODES, resume_task, start

SCHEMA_VERSION = "2.7.0"
CATEGORIES = {
    "observation", "assumption", "inference", "decision", "action",
    "tool_result", "validation", "correction", "contradiction", "retry",
    "state_transition", "handoff", "completion", "blocker",
}
PAYLOAD_FIELDS = {"tool_type", "result_class", "evidence_reference", "exit_state", "summary"}



























































































































    # Compatibility names fail explicitly rather than accidentally exposing
    # host authority to an agent/plugin caller holding only RuntimeSession.






























if __name__ == "__main__":
    task_id = sys.argv[1] if len(sys.argv) > 1 else "task-default"
    shape = sys.argv[2] if len(sys.argv) > 2 else "implement"
    raw_domains = sys.argv[3] if len(sys.argv) > 3 else ""
    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]
    bundle = resolve_runtime.resolve(task_id, shape, domains)
    session = start(bundle)
    print(f"task:     {task_id}")
    print(f"profile:  {shape}")
    print(f"kernel:   {' -> '.join(bundle.kernel)}")
    print(f"capsules: {', '.join(bundle.injected_capsules) or '(none)'}")
    ctx = context_for(session)
    print(f"context:  {len(ctx.split())} words injected into model")
    print(f"snapshot: {resolve_runtime.write_snapshot(bundle)}")
    print(f"policy:   {bundle.pinned['policy_hash']}")

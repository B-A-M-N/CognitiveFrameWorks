#!/usr/bin/env python3
"""Construct each cohort configuration for real and emit DP behavioral telemetry.

The benchmark harness speaks JSON to an agent subprocess. On its own that
measures the model, not the system under test: it never builds CFW, never binds
DP, and therefore cannot observe starvation, mode behavior, or fairness. This
module supplies that missing half.

For each configuration it constructs the real objects:

- ``baseline``      : CFW only, DP unbound.
- ``cfw_csw``       : CFW session with a real CSW WorkflowAuthority.
- ``cfw_csw_dp``    : the above plus a real DP RuntimeSession bound as the
                      external behavior authority.
- ``cfw_csw_dp_laya``: identical composition, but the semantic provider must be
                      live and attested or the trial is INVALID_TREATMENT.

Nothing here is stubbed. If a configuration cannot be constructed, that is
reported rather than silently downgraded to a weaker cell.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
COW = Path(os.environ.get("COGNITIVE_STATEWORK_ROOT",
                          ROOT.parent / "CognitiveStateWork")).resolve()
DP = Path(os.environ.get("DIGITALPSYCHOLOGY_ROOT",
                         ROOT.parent / "DigitalPsychology")).resolve()

# Required provider attestation for a real semantic treatment (P1-16).
REQUIRED_PROVIDER_ATTESTATION = (
    "backend_id", "model_id", "model_revision", "model_content_hash",
    "provider_protocol_hash", "calibration_bundle_hash", "provider_mode",
    "capability_digest", "enabled_question_families",
)


class HarnessError(RuntimeError):
    """The requested configuration could not be constructed as specified."""


class InvalidTreatment(HarnessError):
    """A treatment arm ran without the capability it claims."""


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cfw_modules():
    """Load CFW's resolver and runtime from the current checkout."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    resolve = _load("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
    runtime = _load("cfw_runtime", ROOT / "scripts" / "runtime.py")
    return resolve, runtime


def _real_csw_authority(state_root: Path):
    if str(COW / "scripts") not in sys.path:
        sys.path.insert(0, str(COW / "scripts"))
    from control_plane import (EvidenceStore, SubjectStateStore,
                               WorkflowAuthority, WorkflowSelectionStore)
    return WorkflowAuthority(
        root=COW,
        state_store=SubjectStateStore(state_root / "csw-subjects.json"),
        evidence_store=EvidenceStore(),
        selection_store=WorkflowSelectionStore(state_root / "csw-selections.json"))


def build_session(*, configuration: str, task_id: str, state_root: Path,
                  statework_id: str = "tuid",
                  flow_id: str = "discover",
                  provider_manifest: Mapping[str, Any] | None = None
                  ) -> dict[str, Any]:
    """Construct one configuration and return its live session handles.

    Returns the CFW session, the DP session when the configuration binds DP, and
    the CSW authority when configured. Raising is preferred to silently
    substituting a weaker composition.
    """
    if configuration not in {"baseline", "cfw", "cfw_csw", "cfw_csw_dp",
                             "cfw_csw_dp_laya"}:
        raise HarnessError(f"unknown cohort configuration {configuration!r}")
    system_one_service = None
    if configuration == "cfw_csw_dp_laya":
        # Caller-supplied metadata is not evidence that a provider is live.
        # Build the provider from its pinned environment and derive attestation
        # from the capability handshake DP will actually use.
        if str(DP) not in sys.path:
            sys.path.insert(0, str(DP))
        from lib.control.system_one import create_system_one_frame_factory
        from lib.system_one_service import SystemOneService
        from lib.telemetry.behavioral_trace import provider_attestation
        try:
            system_one_service = SystemOneService.from_environment()
            factory = create_system_one_frame_factory(system_one_service)
            attestation = provider_attestation(factory)
            missing = [field for field in REQUIRED_PROVIDER_ATTESTATION
                       if attestation is None or not attestation.get(field)]
            if (missing
                    or attestation.get("backend_id") != "digitalpsychology-laya-native"
                    or attestation.get("provider_mode") not in {"shadow", "advisory"}):
                raise ValueError(
                    "the live provider handshake is incomplete or is not a pinned Laya treatment")
        except Exception as exc:
            if system_one_service is not None:
                system_one_service.close()
            raise InvalidTreatment(
                "the Laya arm lacks an actual enabled provider capability handshake; "
                "INVALID_TREATMENT") from exc

    resolve, runtime = _cfw_modules()
    use_csw = configuration != "baseline"
    authority = None
    kwargs: dict[str, Any] = {}
    if use_csw:
        authority = _real_csw_authority(state_root)
        kwargs["state_authority"] = runtime.create_state_authority(
            authority.root,
            state_path=state_root / "cfw-subjects.json",
            evidence_path=state_root / "cfw-evidence.json")

    request = resolve.TaskRequest(
        task_id=task_id, namespace_id="tenant-cohort",
        application_id="cohort-app", application_instance_id="cohort-instance",
        subject_ref=f"repo:{task_id}" if use_csw else None,
        shape="quick",
        domain_tags=("terminal interface",) if use_csw else ())
    bundle = resolve.resolve(
        request, cow_root=(COW if use_csw else ROOT / "statework"),
        requested_flows={statework_id: flow_id} if use_csw else None)
    cfw_session = runtime.start(bundle, telemetry_disabled=True, **kwargs)

    handles: dict[str, Any] = {
        "configuration": configuration,
        "cfw_session": cfw_session,
        "csw_authority": authority,
        "dp_session": None,
        "resolve": resolve,
        "runtime": runtime,
    }

    if configuration in {"cfw_csw_dp", "cfw_csw_dp_laya"}:
        handles["dp_session"] = _bind_dp(
            cfw_session, task_id=task_id, state_root=state_root,
            statework_id=statework_id, authority=authority,
            system_one_service=system_one_service)
    return handles


def _bind_dp(cfw_session: Any, *, task_id: str, state_root: Path,
             statework_id: str, authority: Any,
             system_one_service: Any = None):
    """Open a real DP RuntimeSession with CFW as its component port."""
    if str(DP) not in sys.path:
        sys.path.insert(0, str(DP))
    from digital_psychology_adapter import CFWBehaviorAdapter
    from lib.runtime.adapters.cfw import CFWRuntimeAdapter
    from lib.runtime.contracts import RuntimeIdentity
    from lib.runtime.host import RuntimeHost
    from lib.service import DigitalPsychologyService

    identity = RuntimeIdentity(
        namespace_id="tenant-cohort", application_id="cohort-app",
        application_instance_id="cohort-instance", agent_id="cohort-agent",
        agent_instance_id="cohort-agent-1", session_id=f"session-{task_id}",
        task_id=task_id, attempt_id="attempt-1",
        workflow_id=f"workflow-{task_id}", subject_ref=f"repo:{task_id}",
        segment_id="segment-0", behavioral_subject=f"repo:{task_id}")

    service = DigitalPsychologyService(
        event_db=state_root / "dp-events.sqlite3",
        system_one_service=system_one_service)
    host = RuntimeHost(service=service,
                       state_path=state_root / "dp-behavior.sqlite3")
    policy_context = {
        "semantic_provider_status": (
            "enabled" if system_one_service is not None else "disabled")}
    if system_one_service is not None:
        policy_context["deliverable_kind"] = "code"
    # CFWBehaviorAdapter is the production DP-facing port: it owns the signal
    # collection, action gating and context projection DP binds against.
    cfw_port = CFWBehaviorAdapter(cfw_session, cfw_session)
    session = host.open_session(
        identity=identity, objective=f"Complete cohort task {task_id}",
        objective_criteria=(f"{task_id}-criterion",),
        cfw=CFWRuntimeAdapter(cfw_port), csw=None,
        outcome_evidence_authority=None,
        policy_context=policy_context)
    session._cohort_service = service  # keep the service alive with the session
    return session


def drive_actions(handles: Mapping[str, Any], *, criterion_id: str,
                  action_count: int = 3) -> int:
    """Commit real DP actions so the trace reflects actual runtime behavior.

    A session that was merely constructed has nothing to report; telemetry must
    come from actions DP actually committed, not from a zeroed placeholder.
    Returns the number of committed actions.
    """
    dp_session = handles.get("dp_session")
    if dp_session is None:
        return 0
    from lib.runtime.contracts import RuntimeObservation
    committed = 0
    for index in range(1, action_count + 1):
        dp_session.process_action(RuntimeObservation(
            action_index=index, criterion_status={criterion_id: "unmet"}))
        committed += 1
    return committed


def behavioral_record(handles: Mapping[str, Any], *, trial_id: str,
                      task_outcome_score: float | None = None,
                      task_completed: bool | None = None
                      ) -> dict[str, Any] | None:
    """Return DP's internal trial record, or None when DP is not bound.

    A configuration without DP has no behavioral telemetry by construction. That
    is reported as absent rather than faked, so the missing-telemetry tripwire
    can distinguish "DP recorded nothing" from "DP was not configured".
    """
    dp_session = handles.get("dp_session")
    if dp_session is None:
        return None
    return dp_session.behavioral_trial_record(
        trial_id=trial_id, task_outcome_score=task_outcome_score,
        task_completed=task_completed)


__all__ = ["HarnessError", "InvalidTreatment", "REQUIRED_PROVIDER_ATTESTATION",
           "build_session", "drive_actions", "behavioral_record"]

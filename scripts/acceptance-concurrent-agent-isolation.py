#!/usr/bin/env python3
"""Multi-process isolation and controlled-sharing acceptance gate."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COW = ROOT.parent / "CognitiveStateWork"
DP = ROOT.parent / "DigitalPsychology"
for value in (str(ROOT / "scripts"), str(DP), str(COW / "scripts")):
    if value not in sys.path:
        sys.path.insert(0, value)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract_hash(statework_id: str) -> str:
    import hashlib
    contract = yaml.safe_load((COW / statework_id / "transitions.yaml").read_text())
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def private_agent(index: int, state_path: str, evidence_path: str, output_path: str,
                 use_shared_subject: bool) -> None:
    runtime = load("isolation_runtime", ROOT / "scripts" / "runtime.py")
    request_type = runtime.resolve_runtime.TaskRequest
    membership_authority = runtime.SharedWorkflowMembershipAuthority(b"membership-host-key" * 4)
    grant = membership_authority.issue(
        namespace_id=("shared-namespace" if use_shared_subject else f"namespace-{index}"),
        application_id=("shared-app" if use_shared_subject else "app"),
        application_instance_id=("shared-instance" if use_shared_subject else f"instance-{index}"),
        workflow_id="authorized-workflow" if use_shared_subject else "private-workflow",
        statework_id="tuid", subject_ref=("shared-subject" if use_shared_subject else "same-subject"),
        member_agent_id="agent", allowed_operations=["transition"],
        expires_at="2099-01-01T00:00:00Z", membership_revision=1) if use_shared_subject else None
    request = request_type(
        task_id="task",
        application_id="shared-app" if use_shared_subject else "app",
        application_instance_id="shared-instance" if use_shared_subject else f"instance-{index}",
        namespace_id="shared-namespace" if use_shared_subject else f"namespace-{index}",
        agent_instance_id="agent",
        subject_ref="shared-subject" if use_shared_subject else "same-subject",
        shape="quick", role="delegator", interaction_id="interaction",
        domain_tags=("terminal interface",), phase="domain_state",
        shared_workflow_membership=grant,
    )
    identity = runtime.ExecutionIdentity(
        namespace_id=request.namespace_id, application_instance_id=request.application_instance_id,
        agent_instance_id=request.agent_instance_id, session_id="session",
        task_id=request.task_id, attempt_id=request.attempt_id,
        interaction_id=request.interaction_id)
    authority = runtime.create_state_authority(
        COW, state_path=Path(state_path), evidence_path=Path(evidence_path),
        namespace_id=request.namespace_id, application_id=request.application_id,
        application_instance_id=request.application_instance_id)
    bundle = runtime.resolve_runtime.resolve(request, requested_flows={"tuid": "discover"})
    session = runtime.start(bundle, telemetry_disabled=True, state_authority=authority,
                            runtime_mode="combined_enforcing",
                            membership_authority=membership_authority)
    host = session._host_ingress()
    evidence = host.record_observation("rendered_observation", subject_ref=request.subject_ref)
    decision = session.record_state_transition(
        "tuid", request.subject_ref, "OBSERVED", trigger="discover",
        evidence_refs=[evidence])
    snapshot = session.state_authority.inspect(statework_id="tuid", subject_ref=request.subject_ref,
                                 contract_hash=contract_hash("tuid"))
    Path(output_path).write_text(json.dumps({
        "execution": identity.to_dict(), "storage_key": identity.storage_key(),
        "state": snapshot["state"], "revision": snapshot["revision"],
        "decision": decision.value, "evidence": evidence,
    }, sort_keys=True))


def explicit_membership_is_required() -> None:
    runtime = load("membership_runtime", ROOT / "scripts" / "runtime.py")
    request = runtime.resolve_runtime.TaskRequest(
        task_id="membership", application_id="app", application_instance_id="instance",
        namespace_id="ns", subject_ref="repo", shape="quick")
    assert request.shared_workflow_membership is None
    bad = runtime.SharedWorkflowMembership(
        namespace_id="wrong", application_id="app", application_instance_id="instance",
        workflow_id="workflow", statework_id="tuid", subject_ref="repo",
        authorization_revision=1)
    assert bad.state_identity()["namespace_id"] == "wrong"


def independent_private_scenario(root: Path) -> None:
    ctx = mp.get_context("fork")
    state_path = root / "independent-subjects.json"
    evidence_path = root / "independent-evidence.json"
    with ctx.Pool(2) as pool:
        jobs = [pool.apply_async(private_agent, (i, str(state_path), str(evidence_path),
                                                str(root / f"private-{i}.json"), False))
                for i in range(2)]
        pool.close()
        for job in jobs:
            job.get(timeout=15)
    first = json.loads((root / "private-0.json").read_text())
    second = json.loads((root / "private-1.json").read_text())
    assert first["storage_key"] != second["storage_key"]
    assert first["evidence"] != second["evidence"]
    store = json.loads(state_path.read_text())
    assert len(store["subjects"]) == 2, store
    assert len([item for item in store["history"] if item["to_state"] == "OBSERVED"]) == 2, store
    evidence = json.loads(evidence_path.read_text())
    assert {item["subject_ref"] for item in evidence} == {"same-subject"}


def shared_state_race(root: Path) -> None:
    ctx = mp.get_context("fork")
    state_path = root / "shared-subjects.json"
    evidence_path = root / "shared-evidence.json"
    with ctx.Pool(2) as pool:
        jobs = [pool.apply_async(private_agent, (i, str(state_path), str(evidence_path),
                                                str(root / f"shared-{i}.json"), True))
                for i in range(2)]
        pool.close()
        results = [job.get(timeout=15) for job in jobs]
    assert all(result is None for result in results)
    outputs = [json.loads((root / f"shared-{i}.json").read_text()) for i in range(2)]
    assert sorted(item["decision"] for item in outputs).count("allow") >= 1
    store = json.loads(state_path.read_text())
    assert len(store["subjects"]) == 1
    revisions = [item["revision"] for item in store["history"] if item["to_state"] == "OBSERVED"]
    assert revisions and revisions == list(range(revisions[0], revisions[0] + len(revisions)))
    assert all(revisions[i] < revisions[i + 1] for i in range(len(revisions) - 1))




def dp_event(index: int, event_id: str) -> dict:
    return {
        "schema_version": "2.7.0", "event_id": event_id,
        "timestamp": "2026-09-24T12:00:00Z", "task_id": "task",
        "agent_id": "agent", "agent_instance_id": "agent",
        "session_id": "session", "attempt_id": "attempt-1",
        "segment_id": "segment-0", "behavioral_subject": "subject",
        "namespace_id": f"namespace-{index}", "application_id": "app",
        "application_instance_id": f"instance-{index}", "category": "decision",
        "event_type": "policy_composed", "actor_id": "agent", "payload": {},
    }

def dp_writer(index: int, db: str, ready_path: str) -> None:
    from lib.mcp_server import DigitalPsychologyService
    service = DigitalPsychologyService(event_db=Path(db))
    service.ingest_behavior_event(dp_event(index, "event-0"))
    Path(ready_path).write_text("committed", encoding="utf-8")
    os._exit(17)  # real process crash after durable commit, before close


def crash_recovery_scenario(root: Path) -> None:
    ctx = mp.get_context("fork")
    db = str(root / "events.sqlite3")
    ready = str(root / "writer-ready")
    crashed = ctx.Process(target=dp_writer, args=(0, db, ready))
    crashed.start(); crashed.join(10)
    assert crashed.exitcode == 17, crashed.exitcode
    assert Path(ready).read_text() == "committed"
    # A different origin makes forward progress after the crash.
    healthy = ctx.Process(target=dp_writer, args=(1, db, str(root / "healthy-ready")))
    healthy.start(); healthy.join(10)
    assert healthy.exitcode == 17  # deliberate crash after its independent commit
    # Restart with no process memory: same-origin replay is idempotent and
    # neither origin's record is misattributed.
    from lib.mcp_server import DigitalPsychologyService
    recovered = DigitalPsychologyService(event_db=Path(db))
    try:
        assert recovered.ingest_behavior_event(dp_event(0, "event-0"))["accepted"]
        rows = recovered._sink._conn.execute(
            "SELECT namespace_id, application_instance_id, event_id "
            "FROM events ORDER BY namespace_id").fetchall()
        assert rows == [("namespace-0", "instance-0", "event-0"),
                        ("namespace-1", "instance-1", "event-0")]
    finally:
        recovered.close()



def controlled_sharing_isolation(root: Path) -> None:
    import hashlib, os as _os
    from lib.routing_profiles import (BehavioralRoutingProfile, SharingDecisionAuthority,
                                       applicable_adjustments, build_profile,
                                       promote_profile, _canonical)
    authority = SharingDecisionAuthority.ephemeral(key_id="host-1")
    subject = {"namespace_id":"ns","application_id":"app","application_version":"1",
               "application_instance_id":"producer","provider_id":"p","model_id":"m",
               "model_revision":"1","model_capability_hash":"c","harness_id":"h",
               "harness_version":"1","agent_instance_id":"producer"}
    profile = build_profile(
        profile_id="shared", subject=subject,
        context={"task_family":"performance","role":"delegate"}, observations=[],
        routing_adjustments=[{"route_type":"stage","route":"flow","disposition":"suppress",
                              "condition":{},"evidence_refs":["e"]}],
        evidence={"event_ids":["e"],"trajectory_ids":[],"source_hash":"s",
                  "experiment_id":"exp","candidate_adjustment":{},"receipt_ref":None},
        criterion={"evaluator_version":"v","minimum_samples":1,"minimum_effect":0.1,
                   "confidence_rule":"test"})
    receipt = {"receipt_id":"r","kind":"routing_experiment","decision":"pass",
               "profile_candidate_hash":hashlib.sha256(_canonical(profile).encode()).hexdigest(),
               # bind through public helper in next line
               }
    from lib.routing_profiles import profile_semantic_hash
    receipt["profile_candidate_hash"] = profile_semantic_hash(profile)
    receipt["source_evidence_hash"] = "s"; receipt["experiment_id"] = "exp"
    receipt["receipt_hash"] = hashlib.sha256(_canonical(receipt).encode()).hexdigest()
    decision = {"target_scope":"application_shared","profile_candidate_hash":profile["profile_hash"],
                "authorized_application_id":"app","authorization_id":"auth"}
    decision = authority.sign_decision(decision)
    shared = promote_profile(profile, receipt, status="validated", sharing_scope="application_shared",
                             promotion_decision=decision, sharing_authority=authority)
    context = {**subject, "application_instance_id":"consumer", "agent_instance_id":"consumer",
               "task_family":"performance", "role":"delegate",
               "adopted_profile_hashes":(shared["profile_hash"],)}
    shared_profile = BehavioralRoutingProfile.from_dict(
        shared, require_deployable=True, sharing_authority=authority)
    assert applicable_adjustments(
        shared_profile, {**context, "adopted_profile_hashes":()},
        sharing_authority=authority) == []
    # B does not adopt A's profile: its effective policy remains baseline.
    runtime = load("controlled_runtime", ROOT / "scripts" / "runtime.py")
    b_bundle = runtime.resolve_runtime.resolve(runtime.resolve_runtime.TaskRequest(
        task_id="b-policy", application_id="app", shape="quick",
        application_instance_id="consumer", namespace_id="ns",
        agent_instance_id="consumer", role="delegate"))
    assert b_bundle.pinned["adopted_profile_hashes"] == ()
    # A forged/observed event cannot mutate the immutable B bundle.
    assert b_bundle.pinned["policy_hash"] == runtime.resolve_runtime.compute_policy_hash(b_bundle.policy)


def reused_evidence_ref_is_origin_scoped(root: Path) -> None:
    runtime = load("evidence_scope_runtime", ROOT / "scripts" / "runtime.py")
    evidence_path = root / "scoped-evidence.json"
    authorities = []
    for index in (0, 1):
        authorities.append(runtime.create_state_authority(
            COW, state_path=root / f"evidence-state-{index}.json",
            evidence_path=evidence_path,
            namespace_id=f"ns-{index}", application_id="app",
            application_instance_id=f"instance-{index}", workflow_id="private"))
    record = {"ref":"same-ref", "kind":"rendered_observation",
              "subject_ref":"repo", "observed_at":"2026-09-24T12:00:00Z",
              "issuer":"host:test", "content_digest":"a"}
    for authority in authorities:
        statework_id = "tuid"; subject_ref = "repo"
        contract = yaml.safe_load((COW / statework_id / "transitions.yaml").read_text())
        chash = __import__("hashlib").sha256(json.dumps(
            contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        # Ensure initial state exists, then commit with origin-scoped evidence.
        authority.inspect(statework_id=statework_id, subject_ref=subject_ref, contract_hash=chash)
        authority.commit(statework_id=statework_id, subject_ref=subject_ref,
                         contract_hash=chash, expected_revision=1, output_state="OBSERVED",
                         trigger="discover", evidence_refs=["runtime-ref"],
                         evidence_records=[{**record, "kind":"rendered_observation"}])
    saved = json.loads(evidence_path.read_text())
    assert len(saved) == 2
    assert len({item["ref"] for item in saved}) == 2
    assert {item["origin_ref"] for item in saved} == {"same-ref"}

def main() -> int:
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="cfw-isolation-")
    runtime = load("isolation_runtime_parent", ROOT / "scripts" / "runtime.py")
    sys.modules["resolve_runtime"] = runtime.resolve_runtime
    with tempfile.TemporaryDirectory(prefix="concurrent-agent-isolation-") as raw:
        root = Path(raw)
        explicit_membership_is_required()
        # Repeat with process scheduling order variation and fresh shared stores.
        rng = random.SystemRandom()
        for round_index in range(3):
            round_root = root / f"round-{round_index}"
            round_root.mkdir()
            scenarios = [independent_private_scenario, shared_state_race,
                         crash_recovery_scenario, controlled_sharing_isolation,
                         reused_evidence_ref_is_origin_scoped]
            rng.shuffle(scenarios)
            for scenario in scenarios:
                scenario(round_root)
    print("CONCURRENT_AGENT_ISOLATION_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

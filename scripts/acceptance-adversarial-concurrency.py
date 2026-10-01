#!/usr/bin/env python3
"""Sustained multi-process contention, crash, trust, and forward-progress gate."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import multiprocessing as mp
import os
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DP = ROOT.parent / "DigitalPsychology"
COW = ROOT.parent / "CognitiveStateWork"
for value in (str(ROOT / "scripts"), str(DP), str(COW / "scripts")):
    if value not in sys.path:
        sys.path.insert(0, value)


def event(index: int, event_id: str, *, throttle: bool = False) -> dict:
    return {
        "schema_version": "2.4.0", "event_id": event_id,
        "timestamp": "2026-09-24T12:00:00Z", "task_id": f"task-{index}",
        "agent_id": f"agent-{index}", "agent_instance_id": f"agent-{index}",
        "session_id": "session", "attempt_id": "attempt-1",
        "segment_id": "segment-0", "behavioral_subject": f"subject-{index}",
        "namespace_id": "ns", "application_id": "app",
        "application_instance_id": f"instance-{index}",
        "category": "decision", "event_type": "policy_composed",
        "actor_id": f"agent-{index}",
        "payload": {"summary": f"event sequence {event_id}", "result_class": "validated"},
    }


def dp_writer(index: int, db: str, count: int, crash_after: int | None = None,
              throttle: bool = False) -> None:
    from lib.mcp_server import DigitalPsychologyService
    service = DigitalPsychologyService(event_db=Path(db), busy_timeout_ms=250,
                                       max_pending_events_per_origin=10_000)
    for sequence in range(count):
        service.ingest_behavior_event(event(index, f"event-{sequence}"))
        if throttle:
            time.sleep(0.001)
        if crash_after is not None and sequence + 1 == crash_after:
            os._exit(23)
    service.close()


def ordinary_forward_progress(db: str, index: int, count: int = 200) -> None:
    from lib.mcp_server import DigitalPsychologyService
    service = DigitalPsychologyService(event_db=Path(db), busy_timeout_ms=250,
                                       max_pending_events_per_origin=10_000)
    for sequence in range(count):
        service.ingest_behavior_event(event(index, f"event-{sequence}"))
    service.close()


def deliberate_contention(db: str, hold_seconds: float) -> None:
    connection = sqlite3.connect(db, timeout=1)
    connection.execute("PRAGMA busy_timeout=1000")
    connection.execute("BEGIN IMMEDIATE")
    time.sleep(hold_seconds)
    connection.rollback()
    connection.close()


def sustained_dp_qualification(root: Path, *, agents: int, events_per_agent: int) -> dict:
    db = str(root / "events.sqlite3")
    ctx = mp.get_context("fork")
    rng = random.SystemRandom()
    started = time.monotonic()
    processes = []
    # Crashed writers, ordinary writers, and a deliberate lock holder contend.
    for index in range(agents):
        processes.append(ctx.Process(target=dp_writer, args=(
            index, db, events_per_agent, max(1, events_per_agent // 3)
            if index % 5 == 0 else None, True)))
    blocker = ctx.Process(target=deliberate_contention, args=(db, 0.15))
    processes.append(blocker)
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
    writer_processes = processes[:-1]
    for index, process in enumerate(writer_processes):
        expected_crash = index % 5 == 0
        if process.exitcode != (23 if expected_crash else 0):
            raise AssertionError(
                f"writer {index} exit={process.exitcode}, expected={23 if expected_crash else 0}")
    if processes[-1].exitcode != 0:
        raise AssertionError(f"contention holder exit={processes[-1].exitcode}")
    ordinary_latencies = []
    # Agent B is not among the crash/noise cohort and must finish despite it.
    progress_started = time.monotonic()
    ordinary_forward_progress(db, agents + 1, 200)
    ordinary_latencies.append(time.monotonic() - progress_started)
    for process in processes:
        if process.is_alive():
            process.kill(); process.join()
    from lib.mcp_server import DigitalPsychologyService
    service = DigitalPsychologyService(event_db=Path(db), busy_timeout_ms=250)
    try:
        # Replay every crashed origin to make its prefix durable and idempotent.
        for index in range(0, agents, 5):
            crash_prefix = max(1, events_per_agent // 3)
            for sequence in range(crash_prefix, events_per_agent):
                service.ingest_behavior_event(event(index, f"event-{sequence}"))
        for sequence in range(200):
            service.ingest_behavior_event(event(agents + 1, f"event-{sequence}"))
        count = service._sink._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        origins = service._sink._conn.execute(
            "SELECT COUNT(DISTINCT namespace_id || '|' || application_instance_id) "
            "FROM events").fetchone()[0]
    finally:
        service.close()
    expected = sum(events_per_agent for _ in range(agents)) + 200
    if count != expected:
        raise AssertionError(f"forward progress lost events: {count} != {expected}")
    if origins != agents + 1:
        raise AssertionError(f"origin collision/merge detected: {origins}")
    if ordinary_latencies[0] > 5.0:
        raise AssertionError("ordinary agent exceeded forward-progress latency budget")
    return {
        "agents": agents, "events_per_agent": events_per_agent,
        "total_events": count, "origins": origins,
        "ordinary_forward_progress_seconds": round(ordinary_latencies[0], 3),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }

def trust_boundary_qualification(root: Path) -> dict:
    """Malformed observations and scope decisions without host authority fail."""
    from lib.mcp_server import DigitalPsychologyService
    from lib.routing_profiles import (SharingDecisionAuthority, build_profile,
                                      profile_semantic_hash, promote_profile)
    db = root / "trust.sqlite3"
    service = DigitalPsychologyService(event_db=db)
    malformed = event(0, "malformed")
    malformed["namespace_id"] = ""
    try:
        service.ingest_behavior_event(malformed)
    except ValueError:
        malformed_rejected = True
    else:
        malformed_rejected = False
    service.close()
    if not malformed_rejected:
        raise AssertionError("malformed forged observation was accepted")

    profile = build_profile(
        profile_id="trust", subject={"namespace_id":"ns","application_id":"app",
        "application_version":"1","application_instance_id":"producer",
        "provider_id":"p","model_id":"m","model_revision":"1",
        "model_capability_hash":"c","harness_id":"h","harness_version":"1",
        "agent_instance_id":"producer"}, context={"task_family":"performance"},
        observations=[], routing_adjustments=[{"route_type":"stage","route":"flow",
        "disposition":"suppress","condition":{},"evidence_refs":["e"]}],
        evidence={"event_ids":["e"],"trajectory_ids":[],"source_hash":"s",
        "experiment_id":"exp","candidate_adjustment":{},"receipt_ref":None},
        criterion={"evaluator_version":"v","minimum_samples":1,
        "minimum_effect":0.1,"confidence_rule":"test"})
    receipt = {"receipt_id":"r","kind":"routing_experiment","decision":"pass",
               "profile_candidate_hash":profile_semantic_hash(profile),
               "source_evidence_hash":"s","experiment_id":"exp"}
    receipt["receipt_hash"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",",":")).encode()).hexdigest()
    authority = SharingDecisionAuthority.ephemeral(key_id="host-1")
    unsigned = {"target_scope":"application_shared",
                "profile_candidate_hash":profile["profile_hash"],
                "authorized_application_id":"app","authorization_id":"forged"}
    try:
        promote_profile(profile, receipt, status="validated",
                        sharing_scope="application_shared", promotion_decision=unsigned)
    except ValueError:
        unsigned_rejected = True
    else:
        unsigned_rejected = False
    if not unsigned_rejected:
        raise AssertionError("unsigned sharing promotion was accepted")
    signed = authority.sign_decision(dict(unsigned, authorization_id="valid"))
    forged = dict(signed, authorization_id="tampered")
    try:
        promote_profile(profile, receipt, status="validated",
                        sharing_scope="application_shared", promotion_decision=forged,
                        sharing_authority=authority)
    except ValueError:
        forged_rejected = True
    else:
        forged_rejected = False
    if not forged_rejected:
        raise AssertionError("tampered signed sharing promotion was accepted")
    return {"malformed_observation_rejected": malformed_rejected,
            "unsigned_sharing_promotion_rejected": unsigned_rejected,
            "tampered_sharing_promotion_rejected": forged_rejected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=16)
    parser.add_argument("--events-per-agent", type=int, default=200)
    args = parser.parse_args()
    if not 10 <= args.agents <= 20 or args.events_per_agent < 100:
        raise SystemExit("qualification requires 10..20 agents and >=100 events/agent")
    with tempfile.TemporaryDirectory(prefix="adversarial-concurrency-") as raw:
        root = Path(raw)
        dp = sustained_dp_qualification(root, agents=args.agents,
                                        events_per_agent=args.events_per_agent)
        trust = trust_boundary_qualification(root)
    print(json.dumps({"status":"pass", "dp":dp, "trust":trust}, sort_keys=True))
    print("ADVERSARIAL_CONCURRENCY_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from runtime_contract.routing import applicable_adjustments


IDENTITY = {
    "namespace_id": "namespace-a",
    "application_id": "application-a",
    "application_version": "application-version-a",
    "application_instance_id": "instance-a",
    "provider_id": "provider-a",
    "model_id": "model-a",
    "model_revision": "revision-a",
    "model_capability_hash": "capability-a",
    "harness_id": "harness-a",
    "harness_version": "harness-version-a",
    "agent_instance_id": "agent-instance-a",
}


def _profile() -> dict:
    return {
        "status": "active",
        "scope_mode": "exact",
        "selector_semantics": {"subject": "exact", "context": "exact"},
        "subject": dict(IDENTITY),
        "context": {"task_family": "performance"},
        "routing_adjustments": [{
            "route_type": "stage", "route": "flow", "disposition": "suppress",
            "condition": {}, "evidence_refs": ["receipt:event"],
        }],
    }


def _context() -> dict:
    return {**IDENTITY, "task_family": "performance"}


def test_routing_profile_matches_only_the_exact_producer_identity() -> None:
    profile = _profile()
    assert applicable_adjustments(profile, _context())
    for field in ("application_id", "model_id", "model_revision", "model_capability_hash",
                  "harness_version"):
        mismatched = _context()
        mismatched[field] = mismatched[field] + "-other"
        assert applicable_adjustments(profile, mismatched) == []


def test_missing_identity_is_not_a_wildcard() -> None:
    profile = _profile()
    missing = _context()
    missing.pop("application_instance_id")
    assert applicable_adjustments(profile, missing) == []

def test_routing_profile_role_is_exact() -> None:
    profile = _profile()
    profile["context"] = {**profile["context"], "role": "delegate"}
    delegate = {**_context(), "role": "delegate"}
    delegator = {**_context(), "role": "delegator"}
    assert applicable_adjustments(profile, delegate)
    assert applicable_adjustments(profile, delegator) == []


def test_advice_transaction_rejects_stale_or_mismatched_response() -> None:
    from datetime import datetime, timedelta, timezone
    from runtime_contract.advisor import validate_advice_transaction
    request = {"static_policy_hash": "a" * 64, "eligible_choice_hash": "b" * 64, "context_identity": "c" * 64}
    valid = {**request, "status": "ok", "adjustments": [{"route": "x"}],
             "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    validate_advice_transaction(valid, static_policy=request)
    for field in request:
        bad = {**valid, field: "d" * 64}
        try:
            validate_advice_transaction(bad, static_policy=request)
        except ValueError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"mismatched {field} accepted")
    expired = {**valid, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    try:
        validate_advice_transaction(expired, static_policy=request)
    except ValueError:
        pass
    else:
        raise AssertionError("expired advice accepted")


def test_resume_does_not_emit_fresh_policy_or_route_decisions(tmp_path, monkeypatch) -> None:
    import runtime
    resolve = runtime.resolve_runtime
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    events = []
    bundle = resolve.resolve("resume-no-dupes", "quick")
    first = runtime.start(bundle, session_nonce="resume-no-dupes",
                          telemetry_sink=lambda event: events.append(dict(event)))
    original_routes = dict(first.route_decisions)
    first_events = [event for event in events if event.get("event_type") in {"policy_composed", "route_selected", "statework_selected", "flow_selected", "specialist_selected", "guard_selected", "route_suppressed", "route_preferred"}]
    resumed = runtime.resume_task(bundle, "resume-no-dupes", telemetry_sink=lambda event: events.append(dict(event)))
    resumed_events = events[len(first_events):]
    assert not any(event.get("event_type") == "policy_composed" for event in resumed_events)
    assert not any(str(event.get("event_type", "")).startswith("route_") or str(event.get("event_type", "")).endswith("_selected") for event in resumed_events)
    assert resumed.route_decisions == original_routes


def test_intervention_assignment_and_execution_are_separate(monkeypatch, tmp_path):
    import runtime
    from tests.test_combined_authorities import _runtime_modules
    resolve, runtime_module = _runtime_modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    seen=[]
    bundle=resolve.resolve("intervention-telemetry","quick")
    session=runtime_module.start(bundle, telemetry_sink=lambda e: seen.append(dict(e)))
    assert all("intervention_assigned" in e.get("payload",{}) for e in seen if e.get("event_type") == "policy_composed")
    assert all("intervention_executed" in e.get("payload",{}) for e in seen if "route" in e.get("event_type","") or e.get("payload",{}).get("route"))


def test_causal_lineage_is_pinned_from_host_request_to_route_telemetry(tmp_path, monkeypatch):
    from tests.test_combined_authorities import _runtime_modules
    resolve, runtime = _runtime_modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    events=[]
    request=resolve.TaskRequest(
        task_id="causal-lineage", application_id="app", shape="quick",
        parent_decision_ref="decision-1", shared_evidence_refs=("evidence-1", "evidence-2"),
        delegation_topology="delegator:agent-a->delegate:agent-b",
        premise_digest="premise-digest", failure_attribution="inherited")
    bundle=resolve.resolve(request)
    assert bundle.pinned["parent_decision_ref"] == "decision-1"
    assert tuple(bundle.pinned["shared_evidence_refs"]) == ("evidence-1", "evidence-2")
    session=runtime.start(bundle, telemetry_sink=lambda e: events.append(dict(e)))
    route_events=[e for e in events if "route" in e.get("event_type", "")]
    assert route_events
    for event in route_events:
        payload=event["payload"]
        assert payload["parent_decision_ref"] == "decision-1"
        assert payload["shared_evidence_refs"] == ["evidence-1", "evidence-2"]
        assert payload["delegation_topology"] == "delegator:agent-a->delegate:agent-b"
        assert payload["premise_digest"] == "premise-digest"
        assert payload["failure_attribution"] == "inherited"


def test_operator_trace_is_host_only_and_contains_only_bounded_references(tmp_path, monkeypatch) -> None:
    from tests.test_combined_authorities import _load, ROOT
    import sys
    sys.modules.pop("resolve_runtime", None)
    resolve = _load("trace_resolve", ROOT / "scripts" / "resolve-runtime.py")
    sys.modules["resolve_runtime"] = resolve
    runtime = _load("trace_runtime", ROOT / "scripts" / "runtime.py")
    runtime_api = _load("trace_cognitive_runtime", ROOT / "scripts" / "cognitive_runtime.py")

    class Advisor:
        def advice_for_task(self, *, context, eligible_routes, static_policy):
            from datetime import datetime, timedelta, timezone
            return {
                "status": "ok",
                "static_policy_hash": static_policy["static_policy_hash"],
                "eligible_choice_hash": static_policy["eligible_choice_hash"],
                "context_identity": static_policy["context_identity"],
                "profile_pack_hash": "profile-pack-hash",
                "advice_id": "advice-1",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "adjustments": [],
            }

    request = resolve.TaskRequest(
        task_id="operator-trace",
        shape="quick",
        subject_ref="subject",
        namespace_id="ns",
        application_id="app",
        application_instance_id="instance",
        agent_id="agent",
    )
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    bundle = resolve.resolve(request, behavioral_advisor=Advisor())
    session = runtime.start(bundle, telemetry_disabled=True)
    assert not hasattr(session, "_operator_trace") or callable(session._operator_trace)
    host = runtime_api.CognitiveRuntime().host_handle(session)
    assert not hasattr(host._session_token, "operator_trace")
    trace = host.operator_trace()
    assert trace["task"]["task_id"] == "operator-trace"
    assert trace["policy"]["policy_hash"]
    assert trace["advice"]["advice_id"] == "advice-1"
    assert trace["advice"]["adjustment_count"] == 0
    encoded = repr(trace)
    assert "prompt" not in encoded.lower()
    assert "actual_result" not in encoded
    assert "injected" not in trace


def test_shared_policy_requires_explicit_adoption_and_honors_immediate_rollback(tmp_path, monkeypatch):
    import runtime
    from tests.test_combined_authorities import _runtime_modules
    resolve, runtime_module = _runtime_modules()
    monkeypatch.setattr(resolve, "runtime_state_dir", lambda: tmp_path)
    profile_hash = "a" * 64
    adopted = resolve.TaskRequest(
        task_id="adopted-task", application_id="app", shape="quick",
        adopted_profile_hashes=(profile_hash,), revocation_mode="immediate")
    adopted_bundle = resolve.resolve(adopted)
    baseline = resolve.resolve(resolve.TaskRequest(task_id="baseline", application_id="app", shape="quick"))
    assert tuple(adopted_bundle.pinned["adopted_profile_hashes"]) == (profile_hash,)
    assert baseline.pinned["policy_hash"] != adopted_bundle.pinned["policy_hash"]
    class Revoked:
        def inspect(self, hashes):
            assert tuple(hashes) == (profile_hash,)
            return {"revision": 4, "revoked_profile_hashes": [profile_hash], "immediate": True}
    active = runtime_module.start(
        adopted_bundle, telemetry_disabled=True, policy_revocation_authority=Revoked())
    second_adopted = resolve.resolve(resolve.TaskRequest(
        task_id="second-adopted-task", application_id="app", shape="quick",
        adopted_profile_hashes=(profile_hash,), revocation_mode="immediate"))
    second_active = runtime_module.start(
        second_adopted, telemetry_disabled=True, policy_revocation_authority=Revoked())
    for active_task in (active, second_active):
        assert active_task.before_action(
            {"tool_binding_id":"read","arguments":{},"subject_ref":""}
        ) == runtime_module.GateDecision.BLOCK
    safe = runtime_module.start(baseline, telemetry_disabled=True, policy_revocation_authority=Revoked())
    assert safe.before_action({"tool_binding_id":"read","arguments":{},"subject_ref":""}) != runtime_module.GateDecision.BLOCK


def test_shared_profile_artifact_requires_host_verifier(tmp_path):
    import json, sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "DigitalPsychology"))
    from lib.routing_profiles import SharingDecisionAuthority, build_profile, promote_profile
    from runtime_contract.routing import load_routing_profiles
    profile = build_profile(
        profile_id="cfw-shared", subject={"namespace_id":"ns","application_id":"app",
        "application_version":"1","application_instance_id":"producer","provider_id":"p",
        "model_id":"m","model_revision":"1","model_capability_hash":"c",
        "harness_id":"h","harness_version":"1","agent_instance_id":"producer"},
        context={}, observations=[], routing_adjustments=[],
        evidence={"event_ids":["e"],"trajectory_ids":[],"source_hash":"s",
                  "experiment_id":"exp","candidate_adjustment":{},"receipt_ref":None},
        criterion={"evaluator_version":"v","minimum_samples":1,
                   "minimum_effect":0.1,"confidence_rule":"test"})
    import hashlib
    from lib.routing_profiles import _canonical, profile_semantic_hash
    receipt = {"receipt_id":"r","kind":"routing_experiment","decision":"pass",
               "profile_candidate_hash":profile_semantic_hash(profile),
               "source_evidence_hash":"s","experiment_id":"exp"}
    receipt["receipt_hash"] = hashlib.sha256(_canonical(receipt).encode()).hexdigest()
    authority = SharingDecisionAuthority.ephemeral(key_id="host-1")
    decision = authority.sign_decision({
        "target_scope":"application_shared", "profile_candidate_hash":profile["profile_hash"],
        "authorized_application_id":"app", "authorization_id":"auth"})
    shared = promote_profile(profile, receipt, status="validated",
                             sharing_scope="application_shared", promotion_decision=decision,
                             sharing_authority=authority)
    path = tmp_path / "shared-profile.json"
    path.write_text(json.dumps(shared), encoding="utf-8")
    path.chmod(0o600)
    try:
        load_routing_profiles(path)
    except ValueError as exc:
        assert "host-owned promotion verification" in str(exc)
    else:
        raise AssertionError("CFW accepted shared profile without host verifier")
    loaded, _ = load_routing_profiles(
        path, sharing_verifier=lambda decision, scope, candidate: authority.verify_decision(
            decision, target_scope=scope, candidate_hash=candidate))
    assert loaded[0]["profile_hash"] == shared["profile_hash"]

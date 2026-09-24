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

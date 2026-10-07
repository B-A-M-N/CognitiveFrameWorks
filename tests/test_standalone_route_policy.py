import pytest

from runtime_session.standalone_route_policy import (
    PolicyEpochRecord, RoutePolicyContext, RouteSelectionUnavailable,
    StandaloneRoutePolicy,
)


def test_standalone_route_policy_uses_owner_scores_and_explicit_preference():
    strategy = StandaloneRoutePolicy()

    ranked = strategy.select(
        ("route-a", "route-b"),
        RoutePolicyContext(scores={"route-a": 1.0, "route-b": 2.0}))
    assert (ranked.route, ranked.source) == ("route-b", "metacognition")

    operator = strategy.select(
        ("route-a", "route-b"),
        RoutePolicyContext(route_preference="route-a", preference_source="operator"))
    assert (operator.route, operator.source) == ("route-a", "operator")


def test_standalone_route_policy_rejects_learned_and_semantic_preferences():
    strategy = StandaloneRoutePolicy()
    for source in ("learned_profile", "semantic_advisory"):
        with pytest.raises(ValueError, match="unsupported deterministic route preference"):
            strategy.select(
                ("route-a", "route-b"),
                RoutePolicyContext(route_preference="route-a",
                                   preference_source=source))

    with pytest.raises(RouteSelectionUnavailable, match="no deterministic preference"):
        strategy.select(
            ("route-a", "route-b"),
            RoutePolicyContext(scores={"route-a": 1.0, "route-b": 1.0}))


def test_standalone_route_policy_owns_restorable_epoch_state():
    record = PolicyEpochRecord(
        epoch_index=1, parent_policy_hash="parent", child_policy_hash="child",
        reason="dynamic_replan", route_type="flow", selected_route="route-a",
        decision_source="metacognition", decision_ref="decision-1",
        semantic_decision_ref=None, state_revision=3)
    strategy = StandaloneRoutePolicy()
    strategy.restore_policy_epoch(record)
    assert strategy.policy_epoch_record == record
    assert PolicyEpochRecord.from_mapping(record.to_dict()) == record

    conflicting = PolicyEpochRecord(
        epoch_index=2, parent_policy_hash="child", child_policy_hash="other",
        reason="dynamic_replan", route_type="flow", selected_route="route-b",
        decision_source="metacognition", decision_ref="decision-2",
        semantic_decision_ref=None, state_revision=4)
    with pytest.raises(PermissionError, match="cannot be rebound"):
        strategy.restore_policy_epoch(conflicting)

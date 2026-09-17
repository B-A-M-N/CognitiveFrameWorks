"""Host-side consumer for validated DigitalPsychology routing profiles."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


class RoutingProfileError(ValueError):
    pass


NON_SUPPRESSIBLE_STAGES = {"ward", "sispis"}


def routing_plan_hash(plan: Mapping[str, Any]) -> str:
    data = dict(plan)
    data.pop("plan_hash", None)
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def routing_pack_semantic_hash(pack: Mapping[str, Any]) -> str:
    data = dict(pack)
    data.pop("semantic_hash", None)
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def assign_experiment_cohort(plan: Mapping[str, Any], trial_identity: str) -> str:
    """Deterministically assign a host-created trial to control/treatment."""
    if plan.get("assignment_algorithm") != "sha256(experiment_id + trial_identity)@v1":
        raise RoutingProfileError("unsupported routing experiment assignment algorithm")
    if plan.get("plan_hash") and plan.get("plan_hash") != routing_plan_hash(plan):
        raise RoutingProfileError("routing experiment plan hash mismatch")
    fraction = float(plan.get("canary_fraction", 0.0) or 0.0)
    if not 0 <= fraction <= 1:
        raise RoutingProfileError("routing experiment canary_fraction must be between 0 and 1")
    if fraction == 0:
        return "treatment"
    digest = hashlib.sha256(f"{plan.get('experiment_id')}:{trial_identity}".encode()).hexdigest()
    return "treatment" if int(digest[:8], 16) / float(0xFFFFFFFF) < fraction else "control"


def comparison_context_hash(context: Mapping[str, Any]) -> str:
    excluded = {"routing_experiment_plan", "routing_cohort", "candidate_profile_hash",
                "candidate_adjustment_applied", "comparison_context_hash",
                "session_id", "attempt_id", "task_id"}
    body = {key: value for key, value in context.items() if key not in excluded}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def validate_adaptive_adjustment(adjustment: Mapping[str, Any], *, static_stages: Mapping[str, Any],
                                 always_on_guards: Iterable[str] = ()) -> None:
    route_type = adjustment.get("route_type")
    disposition = adjustment.get("disposition")
    route = str(adjustment.get("route") or "")
    if disposition == "activate":
        raise RoutingProfileError("adaptive routing cannot activate a route outside static eligibility")
    if route_type in {"stage", "lifecycle_stage"} and disposition == "suppress":
        owner = static_stages.get(route, {}).get("owner", route)
        if route in NON_SUPPRESSIBLE_STAGES or owner in NON_SUPPRESSIBLE_STAGES:
            raise RoutingProfileError(f"adaptive routing cannot suppress mandatory stage {route!r}")
    if route_type == "guard" and disposition == "suppress" and route in set(always_on_guards):
        raise RoutingProfileError(f"adaptive routing cannot suppress always-on guard {route!r}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def profile_semantic_hash(profile: Mapping[str, Any]) -> str:
    data = dict(profile)
    data.pop("profile_hash", None)
    data.pop("created_at", None)
    evidence = dict(data.get("evidence") or {})
    stable_evidence = {"experiment_id": evidence.get("experiment_id")}
    if "candidate_adjustment" in evidence:
        stable_evidence["candidate_adjustment"] = {
            key: value for key, value in dict(evidence["candidate_adjustment"]).items()
            if key != "evidence_refs"
        }
    data["evidence"] = {key: value for key, value in stable_evidence.items()
                         if value is not None}
    data["observations"] = []
    data["routing_adjustments"] = [
        {key: value for key, value in dict(adjustment).items()
         if key != "evidence_refs"}
        for adjustment in data.get("routing_adjustments", [])
    ]
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def _criterion_hash(criterion: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(criterion)).encode("utf-8")).hexdigest()


def _profile_schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "behavioral-routing-profile.schema.json"


def _pack_schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "behavioral-routing-pack.schema.json"


def _validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    profile = dict(profile)
    if profile.get("profile_hash") != profile_semantic_hash(profile):
        raise RoutingProfileError("routing profile semantic hash mismatch")
    if profile.get("status") not in {"validated", "active"}:
        raise RoutingProfileError("only validated or active routing profiles may affect composition")
    evidence = profile.get("evidence") or {}
    receipt = evidence.get("receipt") or {}
    if not evidence.get("receipt_ref") or not receipt:
        raise RoutingProfileError("deployable routing profile requires a validation receipt")
    if (receipt.get("receipt_id") != evidence.get("receipt_ref")
            or receipt.get("kind") != "routing_experiment"
            or receipt.get("decision") != "pass"):
        raise RoutingProfileError("routing profile receipt is not bound to a passing experiment")
    receipt_body = dict(receipt)
    receipt_body.pop("receipt_hash", None)
    if receipt.get("receipt_hash") != hashlib.sha256(_canonical(receipt_body).encode("utf-8")).hexdigest():
        raise RoutingProfileError("routing experiment receipt hash mismatch")
    candidate = json.loads(json.dumps(profile))
    candidate["status"] = "candidate"
    candidate.pop("profile_hash", None)
    candidate_evidence = dict(candidate.get("evidence") or {})
    candidate_evidence.pop("receipt_ref", None)
    candidate_evidence.pop("receipt", None)
    candidate_evidence.pop("experiment_receipt", None)
    candidate["evidence"] = candidate_evidence
    if receipt.get("profile_candidate_hash") != profile_semantic_hash(candidate):
        raise RoutingProfileError("routing receipt is bound to a different candidate")
    if receipt.get("source_evidence_hash") != candidate_evidence.get("source_hash"):
        raise RoutingProfileError("routing receipt evidence mismatch")
    if receipt.get("experiment_id") != candidate_evidence.get("experiment_id"):
        raise RoutingProfileError("routing receipt experiment mismatch")
    if profile.get("expires_at"):
        expiry = datetime.fromisoformat(str(profile["expires_at"]).replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc):
            raise RoutingProfileError("routing profile has expired")
    return profile


def load_routing_profiles(path: Optional[Path] = None) -> tuple[list[dict[str, Any]], str]:
    if path is None:
        raw = os.environ.get("COGNITIVE_ROUTING_PROFILE")
        if raw:
            path = Path(raw)
        else:
            state_root = os.environ.get("DIGITALPSYCHOLOGY_STATE_ROOT")
            if state_root:
                path = Path(state_root) / "behavioral-routing-profile.json"
            else:
                xdg = os.environ.get("XDG_STATE_HOME")
                path = (Path(xdg) if xdg else Path.home() / ".local" / "state") / \
                    "digitalpsychology" / "behavioral-routing-profile.json"
    if not path.exists():
        return [], ""
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingProfileError(f"routing profile is unreadable: {path}") from exc
    try:
        import jsonschema
        schema = json.loads(_profile_schema_path().read_text(encoding="utf-8"))
        is_pack = isinstance(profile, dict) and "profiles" in profile
        if is_pack:
            if profile.get("routing_pack_version") != "1.0.0":
                raise RoutingProfileError("unsupported routing pack version")
            if profile.get("semantic_hash") != routing_pack_semantic_hash(profile):
                raise RoutingProfileError("routing pack semantic hash mismatch")
            if profile.get("source_revision") is not None and not isinstance(
                    profile.get("source_revision"), str):
                raise RoutingProfileError("routing pack source_revision must be a string or null")
        raw_profiles = profile.get("profiles") if is_pack else [profile]
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise RoutingProfileError("routing pack must contain at least one profile")
        for item in raw_profiles:
            errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(item),
                            key=lambda error: list(error.path))
            if errors:
                raise RoutingProfileError(errors[0].message)
    except ImportError as exc:
        raise RoutingProfileError("jsonschema is required for routing profile validation") from exc
    validated = [_validate_profile(item) for item in raw_profiles]
    pack_body = {"routing_pack_version": "1.0.0", "source_revision": None,
                 "profiles": validated}
    pack_hash = routing_pack_semantic_hash(pack_body)
    if isinstance(profile, dict) and "profiles" in profile:
        pack_hash = profile["semantic_hash"]
    return validated, pack_hash


def load_routing_profile(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    profiles, _ = load_routing_profiles(path)
    return profiles[0] if profiles else None


def _match(expected: Any, actual: Any) -> bool:
    if expected in (None, "", [], ()):  # wildcard
        return True
    if isinstance(expected, (list, tuple, set)):
        values = set(actual if isinstance(actual, (list, tuple, set)) else [actual])
        return bool(values & set(expected))
    return expected == actual


def _condition_matches(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    if not condition:
        return True
    if "all" in condition:
        return all(_condition_matches(item, context) for item in condition["all"])
    if "any" in condition:
        return any(_condition_matches(item, context) for item in condition["any"])
    field = condition.get("field")
    if not isinstance(field, str):
        return False
    if "contains" in condition:
        actual = context.get(field, ())
        return condition["contains"] in (actual if isinstance(actual, (list, tuple, set)) else [actual])
    return "equals" in condition and context.get(field) == condition["equals"]


def applicable_adjustments(profile: Optional[Mapping[str, Any]] | list[Mapping[str, Any]],
                           context: Mapping[str, Any]) -> list[dict[str, Any]]:
    if profile is None:
        return []
    profiles = profile if isinstance(profile, list) else [profile]
    result = []
    for item in profiles:
        subject = item.get("subject") or {}
        if not _match(subject.get("agent_instance_id"), context.get("agent_instance_id")):
            continue
        if any(not _match(subject.get(field), context.get(field)) for field in ("model", "harness")):
            continue
        if any(not _match(expected, context.get(field))
               for field, expected in (item.get("context") or {}).items()):
            continue
        for adjustment in item.get("routing_adjustments", []):
            if _condition_matches(adjustment.get("condition") or {}, context):
                enriched = dict(adjustment)
                enriched["profile_id"] = item.get("profile_id")
                enriched["profile_hash"] = item.get("profile_hash")
                result.append(enriched)
    return result

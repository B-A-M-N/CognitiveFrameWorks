"""Host-side consumer for validated DigitalPsychology routing profiles."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


class RoutingProfileError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def profile_semantic_hash(profile: Mapping[str, Any]) -> str:
    data = dict(profile)
    data.pop("profile_hash", None)
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def _criterion_hash(criterion: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(criterion)).encode("utf-8")).hexdigest()


def _profile_schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "behavioral-routing-profile.schema.json"


def load_routing_profile(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
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
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingProfileError(f"routing profile is unreadable: {path}") from exc
    try:
        import jsonschema
        schema = json.loads(_profile_schema_path().read_text(encoding="utf-8"))
        errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(profile),
                        key=lambda error: list(error.path))
        if errors:
            raise RoutingProfileError(errors[0].message)
    except ImportError as exc:
        raise RoutingProfileError("jsonschema is required for routing profile validation") from exc
    if profile.get("profile_hash") != profile_semantic_hash(profile):
        raise RoutingProfileError("routing profile semantic hash mismatch")
    if profile.get("status") not in {"validated", "active"}:
        raise RoutingProfileError("only validated or active routing profiles may affect composition")
    evidence = profile.get("evidence") or {}
    receipt = evidence.get("receipt") or {}
    if not evidence.get("receipt_ref") or not receipt:
        raise RoutingProfileError("deployable routing profile requires a validation receipt")
    if receipt.get("receipt_id") != evidence.get("receipt_ref") or receipt.get("decision") != "pass":
        raise RoutingProfileError("routing profile validation receipt is not bound to the profile")
    receipt_body = dict(receipt)
    receipt_body.pop("receipt_hash", None)
    if receipt.get("receipt_hash") != hashlib.sha256(_canonical(receipt_body).encode("utf-8")).hexdigest():
        raise RoutingProfileError("routing profile validation receipt hash mismatch")
    candidate = json.loads(json.dumps(profile))
    candidate["status"] = "candidate"
    candidate.pop("profile_hash", None)
    candidate_evidence = dict(candidate.get("evidence") or {})
    candidate_evidence.pop("receipt_ref", None)
    candidate_evidence.pop("receipt", None)
    candidate["evidence"] = candidate_evidence
    if receipt.get("profile_candidate_hash") != profile_semantic_hash(candidate):
        raise RoutingProfileError("routing profile receipt is bound to a different candidate")
    if receipt.get("criterion_hash") != _criterion_hash(profile.get("criterion") or {}):
        raise RoutingProfileError("routing profile receipt criterion mismatch")
    if receipt.get("evidence_hash") != candidate_evidence.get("source_hash"):
        raise RoutingProfileError("routing profile receipt evidence mismatch")
    if profile.get("expires_at"):
        expiry = datetime.fromisoformat(str(profile["expires_at"]).replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc):
            raise RoutingProfileError("routing profile has expired")
    return profile


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


def applicable_adjustments(profile: Optional[Mapping[str, Any]],
                           context: Mapping[str, Any]) -> list[dict[str, Any]]:
    if profile is None:
        return []
    subject = profile.get("subject") or {}
    if not _match(subject.get("agent_instance_id"), context.get("agent_instance_id")):
        return []
    for field in ("model", "harness"):
        if not _match(subject.get(field), context.get(field)):
            return []
    for field, expected in (profile.get("context") or {}).items():
        if not _match(expected, context.get(field)):
            return []
    return [dict(item) for item in profile.get("routing_adjustments", [])
            if _condition_matches(item.get("condition") or {}, context)]

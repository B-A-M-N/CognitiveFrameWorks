"""Host-side consumer for validated DigitalPsychology routing profiles."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from importlib.resources import files as resource_files
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


def routing_arm_identity(plan: Mapping[str, Any], arm: str,
                         trusted_context: Mapping[str, Any]) -> str:
    """Derive the exact semantic identity of one host-prepared policy arm."""
    if arm not in {"control", "treatment"}:
        raise RoutingProfileError("unknown routing experiment arm")
    body = dict(plan)
    body.pop("plan_hash", None)
    body.pop("control_policy_identity", None)
    body.pop("treatment_policy_identity", None)
    body["arm"] = arm
    body["trusted_policy_context"] = dict(trusted_context)
    if arm == "treatment":
        body["candidate_adjustment"] = dict(plan.get("candidate_adjustment") or {})
    else:
        body["candidate_adjustment"] = None
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def prepare_experiment_plan(plan: Mapping[str, Any],
                            trusted_context: Mapping[str, Any]) -> dict[str, Any]:
    """Host-side preparation of policy-arm identities before any trial."""
    prepared = dict(plan)
    prepared["control_policy_identity"] = routing_arm_identity(plan, "control", trusted_context)
    prepared["treatment_policy_identity"] = routing_arm_identity(plan, "treatment", trusted_context)
    prepared["plan_hash"] = routing_plan_hash(prepared)
    return prepared


def _validate_condition(condition: Any) -> None:
    if not isinstance(condition, Mapping) or not condition:
        raise RoutingProfileError("routing experiment eligibility must be a non-empty condition")
    if "all" in condition or "any" in condition:
        key = "all" if "all" in condition else "any"
        if set(condition) != {key} or not isinstance(condition.get(key), list) or not condition[key]:
            raise RoutingProfileError("routing experiment condition group is malformed")
        for item in condition[key]:
            _validate_condition(item)
        return
    if set(condition) - {"field", "equals", "contains"}:
        raise RoutingProfileError("routing experiment condition has unknown fields")
    if not isinstance(condition.get("field"), str) or not condition["field"]:
        raise RoutingProfileError("routing experiment condition requires a field")
    if ("equals" in condition) == ("contains" in condition):
        raise RoutingProfileError("routing experiment condition requires exactly one matcher")


def validate_experiment_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(plan)
    try:
        import jsonschema
        schema_resource = resource_files("runtime_contract").joinpath(
            "contracts/routing-experiment-plan.schema.json")
        schema = json.loads(schema_resource.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
    except ImportError as exc:
        raise RoutingProfileError("jsonschema is required for routing plan validation") from exc
    except jsonschema.ValidationError as exc:
        raise RoutingProfileError(f"routing experiment plan schema mismatch: {exc.message}") from exc
    required = (
        "plan_version", "experiment_id", "candidate_profile_hash", "candidate_profile_id",
        "candidate_adjustment", "eligibility", "assignment_algorithm", "canary_fraction",
        "target_evaluator", "target_evaluator_version", "target_evaluator_hash",
        "control_policy_identity", "treatment_policy_identity", "holdout_requirements",
        "plan_hash",
    )
    if any(key not in data or (not data.get(key) and key != "canary_fraction") for key in required):
        raise RoutingProfileError("routing experiment plan is incomplete")
    if data["plan_version"] != "1.0.0":
        raise RoutingProfileError("unsupported routing experiment plan version")
    if data["assignment_algorithm"] != "sha256(experiment_id + trial_identity)@v1":
        raise RoutingProfileError("unsupported routing experiment assignment algorithm")
    if not isinstance(data["canary_fraction"], (int, float)) or not 0 <= data["canary_fraction"] <= 1:
        raise RoutingProfileError("routing experiment canary_fraction must be between 0 and 1")
    if data["control_policy_identity"] in {"static-control", "control"}:
        raise RoutingProfileError("control policy identity must be host-prepared")
    if data["treatment_policy_identity"] in {"candidate-treatment", "treatment"}:
        raise RoutingProfileError("treatment policy identity must be host-prepared")
    expected_evaluator_hash = hashlib.sha256(
        f"{data['target_evaluator']}@{data['target_evaluator_version']}".encode("utf-8")
    ).hexdigest()
    if data["target_evaluator_hash"] != expected_evaluator_hash:
        raise RoutingProfileError("routing experiment evaluator hash mismatch")
    _validate_condition(data["eligibility"])
    holdout = data["holdout_requirements"]
    if not isinstance(holdout, Mapping) or not isinstance(holdout.get("evaluators"), list) \
            or not holdout["evaluators"] or int(holdout.get("minimum_samples", 0) or 0) < 1:
        raise RoutingProfileError("routing experiment holdout requirements are incomplete")
    if data["plan_hash"] != routing_plan_hash(data):
        raise RoutingProfileError("routing experiment plan hash mismatch")
    return data


def _condition_matches(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    if "all" in condition:
        return all(_condition_matches(item, context) for item in condition["all"])
    if "any" in condition:
        return any(_condition_matches(item, context) for item in condition["any"])
    actual = context.get(condition["field"])
    if "contains" in condition:
        values = actual if isinstance(actual, (list, tuple, set)) else [actual]
        return condition["contains"] in values
    return actual == condition["equals"]


def assign_experiment_cohort(plan: Mapping[str, Any], trial_identity: str,
                             task_context: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Assign only when the trusted task context proves eligibility."""
    plan = validate_experiment_plan(plan)
    context = dict(task_context or {})
    context.setdefault("trial_identity", trial_identity)
    if not _condition_matches(plan["eligibility"], context):
        return None
    holdout_ids = set((plan.get("holdout_requirements") or {}).get("holdout_task_ids") or ())
    if context.get("task_id") in holdout_ids:
        return "holdout"
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
    data.pop("lifecycle_revision", None)
    data.pop("lifecycle_status", None)
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
    resource = resource_files("runtime_contract").joinpath(
        "contracts/behavioral-routing-profile.schema.json")
    if resource.is_file():
        try:
            return Path(resource)
        except TypeError:
            pass
    return Path(__file__).resolve().parents[2] / "contracts" / "behavioral-routing-profile.schema.json"


def _profile_schema_text() -> str:
    resource = resource_files("runtime_contract").joinpath(
        "contracts/behavioral-routing-profile.schema.json")
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    return _profile_schema_path().read_text(encoding="utf-8")


def _pack_schema_path() -> Path:
    resource = resource_files("runtime_contract").joinpath(
        "contracts/behavioral-routing-pack.schema.json")
    if resource.is_file():
        try:
            return Path(resource)
        except TypeError:
            pass
    return Path(__file__).resolve().parents[2] / "contracts" / "behavioral-routing-pack.schema.json"


def _validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    profile = dict(profile)
    if profile.get("lifecycle_status") == "stale":
        raise RoutingProfileError("routing profile lifecycle record is stale")
    if profile.get("lifecycle_status") not in (None, profile.get("status")):
        raise RoutingProfileError("routing profile lifecycle status disagrees with profile status")
    trust = profile.get("artifact_trust")
    if profile.get("status") in {"validated", "active"}:
        if not isinstance(trust, Mapping) or trust.get("mode") != "filesystem-owner":
            raise RoutingProfileError("deployable routing profile lacks an explicit artifact trust mode")
        if int(trust.get("owner_uid", -1)) != os.getuid():
            raise RoutingProfileError("routing profile was produced by a different filesystem owner")
    if profile.get("profile_hash") != profile_semantic_hash(profile):
        raise RoutingProfileError("routing profile semantic hash mismatch")
    if profile.get("status") not in {"validated", "active"}:
        raise RoutingProfileError("only validated or active routing profiles may affect composition")
    if profile.get("scope_mode") not in {"exact", "validated_generalization"}:
        raise RoutingProfileError("deployable routing profile requires an explicit scope_mode")
    if (profile.get("selector_semantics") or {}).get("subject", "exact") != "exact":
        raise RoutingProfileError("learned subject selectors must be exact")
    subject = profile.get("subject") or {}
    required_identity = (
        "namespace_id", "application_id", "application_version", "application_instance_id",
        "provider_id", "model_id", "model_revision", "model_capability_hash",
        "harness_id", "harness_version", "agent_instance_id")
    missing_identity = [name for name in required_identity if subject.get(name) in (None, "")]
    if missing_identity:
        raise RoutingProfileError(
            "deployable routing profile lacks exact producer identity: "
            + ", ".join(missing_identity))
    if profile.get("scope_mode") == "validated_generalization":
        if not profile.get("validated_strata") or not isinstance(
                profile.get("generalization_receipt"), Mapping) \
                or profile["generalization_receipt"].get("decision") != "pass":
            raise RoutingProfileError(
                "generalized routing scope requires independent validation strata and receipt")
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


def _assert_trusted_artifact_path(path: Path) -> None:
    """Apply the declared filesystem-owner trust boundary to custom paths."""
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RoutingProfileError(
            "routing profile artifact must be owned by the runtime user and not group/other writable")
    parent = path.parent
    while True:
        parent_info = parent.lstat()
        shared_sticky_dir = (stat.S_ISDIR(parent_info.st_mode)
                             and bool(parent_info.st_mode & stat.S_ISVTX))
        if (stat.S_ISLNK(parent_info.st_mode)
                or (parent_info.st_mode & 0o022 and not shared_sticky_dir)):
            raise RoutingProfileError("routing profile parent directory is not a protected trust boundary")
        if parent == parent.parent:
            break
        parent = parent.parent


def load_routing_profiles(path: Optional[Path] = None) -> tuple[list[dict[str, Any]], str]:
    canonical_deployment = False
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
            deployment = path.with_name("active-routing-profile.json")
            if deployment.exists():
                path = deployment
                canonical_deployment = True
    if not path.exists():
        return [], ""
    if path.is_symlink():
        raise RoutingProfileError("routing profile path must not be a symlink")
    try:
        _assert_trusted_artifact_path(path)
    except OSError as exc:
        raise RoutingProfileError(f"routing profile trust boundary is unreadable: {path}") from exc
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingProfileError(f"routing profile is unreadable: {path}") from exc
    if canonical_deployment:
        if not isinstance(profile, dict) or not isinstance(profile.get("profile"), dict):
            raise RoutingProfileError("canonical routing deployment record is malformed")
        revision = profile.get("revision")
        deployed = profile["profile"]
        if not isinstance(revision, int) or revision < 1:
            raise RoutingProfileError("canonical routing deployment revision is invalid")
        if profile.get("status") != deployed.get("status"):
            raise RoutingProfileError("canonical routing deployment status disagrees with profile")
        if deployed.get("lifecycle_revision") != revision:
            raise RoutingProfileError("canonical routing deployment revision disagrees with profile")
        if deployed.get("lifecycle_status") != deployed.get("status"):
            raise RoutingProfileError("canonical routing lifecycle status disagrees with profile")
        profile = deployed
    try:
        import jsonschema
        schema = json.loads(_profile_schema_text())
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


def _match(expected: Any, actual: Any, *, semantics: str = "exact") -> bool:
    if expected in (None, "", [], ()) or actual in (None, ""):
        return False
    if semantics == "exact":
        if isinstance(expected, (list, tuple, set)):
            return list(expected) == list(actual) if isinstance(actual, (list, tuple, set)) else False
        return expected == actual
    expected_values = set(expected if isinstance(expected, (list, tuple, set)) else [expected])
    actual_values = set(actual if isinstance(actual, (list, tuple, set)) else [actual])
    if semantics == "contains_all":
        return expected_values.issubset(actual_values)
    if semantics == "contains_any":
        return bool(expected_values & actual_values)
    return False


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
        if any(not _match(subject.get(field), context.get(field), semantics="exact") for field in (
                "namespace_id", "application_id", "application_version", "application_instance_id",
                "provider_id", "model_id", "model_revision", "model_capability_hash",
                "harness_id", "harness_version", "agent_instance_id")):
            continue
        semantics = item.get("selector_semantics") or {}
        if any(not _match(expected, context.get(field),
                          semantics=str(semantics.get("context", "exact")))
               for field, expected in (item.get("context") or {}).items()):
            continue
        for adjustment in item.get("routing_adjustments", []):
            if _condition_matches(adjustment.get("condition") or {}, context):
                enriched = dict(adjustment)
                enriched["profile_id"] = item.get("profile_id")
                enriched["profile_hash"] = item.get("profile_hash")
                result.append(enriched)
    return result

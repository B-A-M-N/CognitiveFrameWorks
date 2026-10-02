"""CFW-owned guard artifact loading, eligibility, and compilation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from runtime_contract.guards import (
    enforcement_key as _contract_enforcement_key,
    nested_scope as _contract_nested_scope,
)
from runtime_contract.handlers import enforcement_for_guard, validate_enforcement
from resolver.common import (
    _stable_json, _thaw, dp_trusted_guard_pack_path, load_yaml, sha256,
    sha256_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONTRACT_ROOT = ROOT / "contracts"
if not LOCAL_CONTRACT_ROOT.exists():
    LOCAL_CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"

class GuardPackIntegrityError(ValueError):
    pass
GUARD_PACK_SCHEMA_URI = "https://digitalpsychology.dev/schemas/guard-pack.schema.json"
GUARD_SCHEMA_URI = "https://digitalpsychology.dev/schemas/guard.schema.json"
def _load_guard_schemas() -> Tuple[dict, dict, Any]:
    """Load the CFW-owned guard/guard-pack interop schemas with a local
    referencing.Registry binding the schema URIs to local files (never network). Returns
    (pack_schema, guard_schema, registry)."""
    pack_schema_path = LOCAL_CONTRACT_ROOT / "guard-pack.schema.json"
    guard_schema_path = LOCAL_CONTRACT_ROOT / "guard.schema.json"
    if not pack_schema_path.exists() or not guard_schema_path.exists():
        raise GuardPackIntegrityError(
            "guard artifact exists but no trusted local guard schemas are installed")
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        pack_schema = json.loads(pack_schema_path.read_text(encoding="utf-8"))
        guard_schema = json.loads(guard_schema_path.read_text(encoding="utf-8"))
        registry = Registry().with_resource(
            guard_schema.get("$id", GUARD_SCHEMA_URI), Resource.from_contents(guard_schema)
        ).with_resource(
            pack_schema.get("$id", GUARD_PACK_SCHEMA_URI), Resource.from_contents(pack_schema)
        )
        # Validate resource loadability as a sanity check
        Draft202012Validator(pack_schema, registry=registry)
        Draft202012Validator(guard_schema, registry=registry)
        return pack_schema, guard_schema, registry
    except Exception as exc:
        raise GuardPackIntegrityError(
            f"trusted guard schemas are unreadable or invalid: {exc}") from exc
def canonical_guard(guard: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical semantic guard representation shared by DP and CFW.

    The evaluator is intentionally semantic here: it selects the behavioral
    evidence used to score a guard. Volatile transport fields are excluded."""
    return {key: value for key, value in guard.items()
            if key not in {"content_hash", "semantic_hash", "compiled_at"}}
def semantic_guard_hash(guard: Dict[str, Any]) -> str:
    # Keep this byte-for-byte aligned with DP's guard_semantic_hash.  A
    # promotion receipt binds to the behavior contract, not the whole pack or
    # volatile transport fields.
    semantic = {
        key: guard.get(key)
        for key in ("family", "version", "target_behavior", "rule",
                    "enforcement", "scope", "trigger", "evaluator")
    }
    return sha256_bytes(_stable_json(_thaw(semantic)).encode("utf-8"))
def guard_pack_semantic_hash(pack_version: str, guards: List[Dict[str, Any]]) -> str:
    body = {
        "pack_version": pack_version,
        "guards": [canonical_guard(g) for g in guards],
    }
    return sha256_bytes(_stable_json(body).encode("utf-8"))
def _legacy_semantic_guard_hash(guard: Dict[str, Any]) -> str:
    """Hash the semantic identity of a guard, excluding volatile/timestamped
    fields (compiled_at etc.). Identical behavior -> identical hash."""
    data = {k: v for k, v in guard.items() if k not in {"evaluator"}}
    return sha256_bytes(_stable_json(data).encode("utf-8"))
def load_guard_pack(validate: bool = True) -> Dict[str, Any]:
    """FrameWorks reads only DP's compiled artifact; never imports DP code.
    The always-on kernel from shared/runtime-kernel.yaml is FrameWorks-owned
    and is always present even when DigitalPsychology is absent.

    The DP artifact is schema-validated and its declared semantic hashes are
    recomputed over the canonical semantic payload. A present-but-invalid
    artifact is a hard integrity failure; callers cannot accidentally start
    a silently degraded policy."""
    if not validate:
        raise GuardPackIntegrityError(
            "guard-pack validation cannot be disabled for runtime policy resolution")
    kernel = load_yaml(ROOT / "shared" / "runtime-kernel.yaml")
    pack: Dict[str, Any] = {
        "pack_version": kernel.get("version", "1.0.0"),
        "content_hash": sha256(ROOT / "shared" / "runtime-kernel.yaml"),
        "semantic_hash": sha256(ROOT / "shared" / "runtime-kernel.yaml"),
        "source": "framework-runtime-kernel",
        "guard_pack_status": "framework-kernel-only",
        "always_on": [{"id": r["id"], "rule": r["rule"], "target_behavior": r.get("target_behavior"),
                       "estimated_tokens": kernel.get("estimated_tokens", 0) // max(len(kernel.get("rules", [])), 1),
                       "status": "active", "priority": "P0", "family": r["id"], "key": f'{r["id"]}@{kernel.get("version", "1.0.0")}',
                       "version": kernel.get("version", "1.0.0"),
                       "enforcement_key": r.get("enforcement_key", r["id"]),
                       "enforcement": enforcement_for_guard(r)}
                      for r in kernel.get("rules", [])],
        "guards": [],
    }
    artifact = dp_trusted_guard_pack_path()
    if not artifact.exists():
        return pack
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise GuardPackIntegrityError(
            "compiled-guard-pack.json is unreadable or malformed JSON") from exc
    if not isinstance(data, dict):
        raise GuardPackIntegrityError("compiled-guard-pack.json root is not an object")

    pack_schema, guard_schema, local_registry = _load_guard_schemas()
    if guard_schema is not None:
        from jsonschema import Draft202012Validator
        bad = []
        for i, g in enumerate(data.get("guards", [])):
            try:
                guard_json = {k: v for k, v in g.items() if k != "evaluator"}
                Draft202012Validator(guard_schema, registry=local_registry).validate(guard_json)
            except Exception as exc:
                bad.append(f"guards[{i}] {getattr(exc, 'message', exc)}")
        if bad:
            raise GuardPackIntegrityError(
                "guard artifact failed guard schema validation: " + "; ".join(bad))

    guards = [g for g in data.get("guards", []) if isinstance(g, dict)]
    for guard in guards:
        try:
            validate_enforcement(guard)
        except ValueError as exc:
            raise GuardPackIntegrityError(str(exc)) from exc
    # recompute the semantic content hash across the guards (excluding
    # compiled_at and any evaluator derivation fields)
    semantic_body = _stable_json({
        "pack_version": data.get("pack_version", pack["pack_version"]),
        "guards": [{k: v for k, v in g.items() if k not in {"compiled_at", "evaluator"}}
                   for g in guards],
    })
    computed_hash = guard_pack_semantic_hash(
        data.get("pack_version", pack["pack_version"]), guards)
    declared_semantic = data.get("semantic_hash", data.get("content_hash"))
    declared_content = data.get("content_hash", data.get("semantic_hash"))
    if declared_semantic != computed_hash:
        raise GuardPackIntegrityError(
            f"guard artifact semantic hash mismatch: declared={declared_semantic!r} "
            f"computed={computed_hash!r}")
    if declared_content != computed_hash:
        raise GuardPackIntegrityError(
            f"guard artifact content hash mismatch: declared={declared_content!r} "
            f"computed={computed_hash!r}")
    if pack_schema is not None:
        try:
            import jsonschema
            from jsonschema import Draft202012Validator
            packed = {
                "pack_version": data.get("pack_version", pack["pack_version"]),
                "content_hash": computed_hash,
                "semantic_hash": computed_hash,
                "compiled_at": data.get("compiled_at", ""),
                "rollout": data.get("rollout", {}),
                "guards": [{k: v for k, v in g.items() if k != "evaluator"} for g in guards],
            }
            validator = Draft202012Validator(pack_schema, registry=local_registry)
            errors = sorted(validator.iter_errors(packed), key=lambda e: list(e.path))
            if errors:
                raise jsonschema.ValidationError(errors[0].message)
        except GuardPackIntegrityError:
            raise
        except Exception as exc:
            raise GuardPackIntegrityError(
                f"guard-pack schema violation: {getattr(exc, 'message', exc)}") from exc
    pack["pack_version"] = data.get("pack_version", pack["pack_version"])
    pack["content_hash"] = computed_hash
    pack["semantic_hash"] = computed_hash
    pack["declared_hash"] = data.get("content_hash")
    pack["source"] = "framework-runtime-kernel + digitalpsychology-artifact"
    pack["guard_pack_status"] = "valid"
    pack["guards"] = guards
    return pack
def canary_assigned(guard_key: str, rollout: Optional[Dict[str, Any]],
                   task_id: Optional[str]) -> bool:
    """Deterministic canary assignment at task composition time (the review's
    exact order): stable hash of (guard-version + task-id) percentile <
    canary_fraction; explicit canary_task_ids remain an override for tests."""
    if task_id is None:
        return False
    rollout = rollout or {}
    if isinstance(rollout, dict) and rollout.get("canary_task_ids"):
        if task_id in set(rollout["canary_task_ids"]):
            return True
    canary_fraction = float(rollout.get("canary_fraction", 0.0) or 0.0)
    if canary_fraction <= 0:
        return False
    key = f"{guard_key}+{task_id}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    percentile = int(digest[:8], 16) / float(0xFFFFFFFF)
    return percentile < canary_fraction
def nested_guard_scope(guard: Dict[str, Any]) -> Dict[str, List[str]]:
    """The only valid guard scope contract. Legacy top-level fields are
    accepted for compatibility but nested values always win and are never
    OR-ed with their legacy counterparts."""
    return _contract_nested_scope(guard)
def applicable_guards(pack: Dict[str, Any], domains: Iterable[str], shapes: Iterable[str],
                      model: Optional[str], harness: Optional[str],
                      trigger: Optional[str], task_id: Optional[str] = None,
                      stateworks: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Guard applicability is fail-closed for validation-sensitive scope:
      guard has model restriction  + runtime model unknown  = NOT ELIGIBLE
      guard has harness restriction + runtime harness unknown = NOT ELIGIBLE
      guard has domain restriction  + domain unknown        = NOT ELIGIBLE
    Only when the runtime value is provided and matches does the guard apply."""
    domains = set(domains)
    shapes = set(shapes)
    stateworks = set(stateworks or ())
    out = []
    for g in pack["guards"]:
        scope = nested_guard_scope(g)
        if scope["domains"] and not (domains & set(scope["domains"])):
            continue
        if scope["stateworks"] and not (stateworks & set(scope["stateworks"])):
            continue
        if scope["task_shapes"] and not (shapes & set(scope["task_shapes"])):
            continue
        if scope["models"] and (model is None or model not in scope["models"]):
            continue
        if scope["harnesses"] and (harness is None or harness not in scope["harnesses"]):
            continue
        if g.get("trigger") is not None and g.get("trigger") != trigger:
            continue
        status = g.get("status")
        if status == "canary":
            # deterministic canary assignment at task composition time
            if not canary_assigned(g.get("key") or g.get("id"), g.get("rollout"), task_id):
                continue
        elif status not in (None, "active"):
            continue
        out.append(g)
    return out
class GuardCompilationError(ValueError):
    pass
def _guard_ref(ref: str, guards: List[Dict[str, Any]]) -> Optional[str]:
    """Resolve a versioned key, exact id, or unambiguous family reference."""
    matching_keys = [g.get("key") or g["id"] for g in guards if g.get("key") == ref]
    if len(matching_keys) > 1:
        return None
    if matching_keys:
        return matching_keys[0]
    keys = {g.get("key") or g["id"] for g in guards}
    if ref in keys:
        return ref
    ids = {g["id"] for g in guards}
    if ref in ids:
        return next(g.get("key") or g["id"] for g in guards if g["id"] == ref)
    families = [g for g in guards if g.get("family") == ref]
    return (families[0].get("key") or families[0]["id"]) if len(families) == 1 else None
def compile_task_guards(guards: List[Dict[str, Any]], domains: Iterable[str],
                        shapes: Iterable[str], model: Optional[str],
                        harness: Optional[str], trigger: Optional[str],
                        task_id: Optional[str], token_budget: int = 200,
                        mandatory_ids: Optional[Iterable[str]] = None,
                        stateworks: Optional[Iterable[str]] = None,
                        routing_adjustments: Optional[Iterable[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Apply DP-compatible supersession, conflict, priority and budget rules."""
    def guard_cost(g: Dict[str, Any]) -> int:
        declared = max(int(g.get("estimated_tokens", 10) or 10), 1)
        measured = max(len(str(g.get("rule", g.get("target_behavior", ""))).split()) * 2, 10)
        return max(declared, measured)
    mandatory = set(mandatory_ids or [])
    eligible = [g for g in guards if g.get("status") == "active" or (
        g.get("status") == "canary" and canary_assigned(
            g.get("key") or g["id"], g.get("rollout"), task_id))]
    scoped = []
    for g in eligible:
        scope = nested_guard_scope(g)
        if scope["domains"] and not (set(domains) & set(scope["domains"])):
            continue
        if scope["stateworks"] and not (set(stateworks or ()) & set(scope["stateworks"])):
            continue
        if scope["task_shapes"] and not (set(shapes) & set(scope["task_shapes"])):
            continue
        if scope["models"] and (model is None or model not in scope["models"]):
            continue
        if scope["harnesses"] and (harness is None or harness not in scope["harnesses"]):
            continue
        if g.get("trigger") is not None and g.get("trigger") != trigger:
            continue
        scoped.append(g)

    preferred_guards = {str(item.get("route")) for item in (routing_adjustments or ()) if item.get("route_type") == "guard" and item.get("disposition") == "prefer"}
    if preferred_guards:
        scoped = [dict(item, _routing_preferred=True) if (item.get("key") or item.get("id")) in preferred_guards or item.get("id") in preferred_guards else item for item in scoped]

    # A family is one semantic intervention.  Different versions may be
    # present in the artifact, but task composition must never activate two
    # of them unless the newer guard explicitly supersedes the older one.
    family_groups: Dict[str, List[Dict[str, Any]]] = {}
    for guard in scoped:
        family_groups.setdefault(str(guard.get("family") or guard.get("id")), []).append(guard)
    family_scoped: List[Dict[str, Any]] = []
    for family, members in family_groups.items():
        if len(members) == 1:
            family_scoped.extend(members)
            continue
        keys = {g.get("key") or g["id"] for g in members}
        superseded: set[str] = set()
        for newer in members:
            for raw in newer.get("supersedes") or []:
                ref = _guard_ref(str(raw), members)
                if ref in keys:
                    superseded.add(ref)
        survivors = [g for g in members if (g.get("key") or g["id"]) not in superseded]
        if len(survivors) != 1:
            raise GuardCompilationError(
                f"guard family {family!r} has multiple effective versions; "
                "declare an explicit supersedes relation")
        family_scoped.extend(survivors)
    scoped = family_scoped

    # Deduplicate exact behavioral contracts before supersession/conflict
    # analysis. Universal kernel entries win over experimental duplicates.
    deduped: List[Dict[str, Any]] = []
    seen_enforcement: set[str] = set()
    for g in sorted(scoped, key=lambda item: (
            item.get("id") not in mandatory, str(item.get("priority", "P3")),
            item.get("key") or item["id"])):
        enforcement_key = _contract_enforcement_key(g)
        if enforcement_key in seen_enforcement:
            continue
        seen_enforcement.add(enforcement_key)
        deduped.append(g)
    scoped = deduped
    keys = {g.get("key") or g["id"] for g in scoped}
    supersession: Dict[str, List[str]] = {}
    for g in scoped:
        key = g.get("key") or g["id"]
        refs=[]
        for raw in g.get("supersedes") or []:
            ref=_guard_ref(str(raw),scoped)
            if ref is None: raise GuardCompilationError(f"guard {key!r} supersedes unknown or ambiguous reference {raw!r}")
            if ref==key: raise GuardCompilationError(f"guard {key!r} supersedes itself")
            refs.append(ref)
        supersession[key]=refs
    visiting:set=set(); done:set=set()
    def visit(key:str):
        if key in done:return
        if key in visiting:raise GuardCompilationError(f"supersession cycle includes {key!r}")
        visiting.add(key)
        for ref in supersession.get(key,[]):visit(ref)
        visiting.remove(key);done.add(key)
    for key in supersession:visit(key)
    superseded={ref for refs in supersession.values() for ref in refs}
    remaining=[g for g in scoped if (g.get("key") or g["id"]) not in superseded]

    conflict_refs:Dict[str,List[str]]={}
    for g in remaining:
        key=g.get("key") or g["id"];refs=[]
        for raw in g.get("conflicts_with") or []:
            ref=_guard_ref(str(raw),remaining)
            if ref is None:raise GuardCompilationError(f"guard {key!r} conflicts with unknown or ambiguous reference {raw!r}")
            refs.append(ref)
        conflict_refs[key]=refs
    for g in remaining:
        key=g.get("key") or g["id"]
        for other in (x.get("key") or x["id"] for x in remaining if (x.get("key") or x["id"]) != key):
            if other in conflict_refs.get(key,[]) or key in conflict_refs.get(other,[]):
                raise GuardCompilationError(f"unresolved guard conflict involving {key!r}")

    def rank(g:Dict[str,Any]):
        p=str(g.get("priority","P3"))
        preferred = 0 if g.get("_routing_preferred") else 1
        return (preferred, int(p[1:]) if p.startswith("P") and p[1:].isdigit() else 3, g.get("key") or g["id"])
    mandatory_g=[g for g in remaining if g["id"] in mandatory]
    optional_g=sorted((g for g in remaining if g["id"] not in mandatory),key=rank)
    mandatory_g.sort(key=rank)
    active=list(mandatory_g)
    tokens=sum(guard_cost(g) for g in active)
    for g in optional_g:
        cost=guard_cost(g)
        if tokens+cost>token_budget:continue
        active.append(g);tokens+=cost
    return active

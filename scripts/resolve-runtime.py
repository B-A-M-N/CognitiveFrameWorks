#!/usr/bin/env python3
"""Runtime resolver: compose FrameWorks kernel + StateWork chain + guard pack
for a task, freeze an immutable policy snapshot, and emit the active bundle.

Composition contract (no imports of DigitalPsychology code):

    TASK START
    ├─ read activation profile from shared/pipeline.yaml (single source)
    ├─ expand aliases (anchor/dox -> concrete stages)
    ├─ StateWork registry: deterministic best match + ambiguity handling
    ├─ plan required consumed-packet prerequisites (typed handoffs,
    │   producer chain, cycle detection; optional inputs never recurse)
    ├─ kernel = union(profile kernel, statework core_requirements)
    ├─ order kernel by manifest order_after edges + requires closure
    │   (canonical manifest order for ties; no append-order influence)
    ├─ guard pack = always-on shared/runtime-kernel.yaml
    │               + DigitalPsychology compiled-guard-pack.json artifact
    │               (schema-validated; semantic content hash; fail-closed
    │                scope matching; never overwrites a snapshot)
    ├─ PolicySnapshot: immutable pin (framework/stateworks/guard pack/hash/
    │   model/harness/toolset/frozen_at)
    └─ EmergencyOverride: separate record, never mutates the snapshot

This module is a pure library: `resolve()` returns RuntimeBundle; the CLI is
only a debugging front end. The bundle writes to an out-of-tree per-task
destination ($XDG_RUNTIME_DIR/cognitiveframeworks/<task>/<snapshot-hash>.json),
never into the source tree.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Set, Union

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONTRACT_ROOT = ROOT / "contracts"
if not LOCAL_CONTRACT_ROOT.exists():
    LOCAL_CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"
_bundled_cow = ROOT / "statework"
_sibling_cow = ROOT.parent / "CognitiveStateWork"
COW_ROOT = Path(os.environ.get("COGNITIVE_STATEWORK_ROOT",
                               _bundled_cow if _bundled_cow.exists() else _sibling_cow))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from runtime_contract.guards import (enforcement_key as _contract_enforcement_key,
                                     nested_scope as _contract_nested_scope)
from runtime_contract.pipeline import load_pipeline_document
from runtime_contract.policy import freeze_contracts as _freeze_contracts
from runtime_contract.policy import stable_hash as _stable_contract_hash
from runtime_contract.stateworks import current_packet as _current_packet
from runtime_contract.task import TaskRequest
from runtime_contract.advisor import BehavioralAdvisor, bounded_adjustments
from runtime_contract.manifests import build_task_manifest
from runtime_contract.handlers import (enforcement_for_guard, handler_registry_hash,
                                       validate_enforcement)
from runtime_contract.validators import validator_registry_hash
from runtime_contract.actions import (action_contract_hash, default_action_registry,
                                       effective_action_registry)
from runtime_contract.compatibility import validate_compatibility
from runtime_contract.routing import (
    applicable_adjustments, assign_experiment_cohort, comparison_context_hash,
    load_routing_profiles, validate_adaptive_adjustment,
    validate_experiment_plan,
)


def dp_trusted_guard_pack_path() -> Path:
    """The only DP deployment artifact CFW consumes."""
    if os.environ.get("DIGITALPSYCHOLOGY_STATE_ROOT"):
        return Path(os.environ["DIGITALPSYCHOLOGY_STATE_ROOT"]) / "compiled-guard-pack.json"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "digitalpsychology" / "compiled-guard-pack.json"

STAGE_FIELDS = ["id", "owner", "requires", "order_after", "execution_mode", "frequency"]
BANNED_SISPIS_DIMENSIONS = [
    "option_multiplicity", "tradeoff_density", "ambiguity_of_framing",
    "comparative_intent", "downstream_impact",
]


class PacketDependencyCycle(ValueError):
    pass


class PacketDependencyError(ValueError):
    pass


class PacketValidationError(PacketDependencyError):
    pass


class GuardPackIntegrityError(ValueError):
    pass


class SnapshotIntegrityError(ValueError):
    pass


def load_yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _load_document(path: Path, loader: Callable[[Path], Any]) -> Any:
    if not path.exists():
        raise SnapshotIntegrityError(f"required policy contract is missing: {path}")
    return loader(path)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _tree_runtime_hash(paths: Iterable[Path], *, relative_to: Optional[Path] = None) -> str:
    digest = hashlib.sha256()
    for path in sorted({Path(path) for path in paths}):
        if not path.exists() or not path.is_file():
            continue
        label_root = relative_to or ROOT
        try:
            label = str(path.relative_to(label_root))
        except ValueError:
            label = str(path)
        digest.update(label.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cow_runtime_hash(cow_root: Path = COW_ROOT) -> str:
    """Hash the executable StateWork engine and its policy schemas."""
    if not (cow_root / "registry.yaml").is_file():
        return hashlib.sha256(b"statework-capability:absent\0" + str(cow_root).encode()).hexdigest()
    paths = [cow_root / "scripts" / "control_plane.py"]
    paths.extend((cow_root / "schemas").glob("*.json"))
    paths.extend((cow_root / "schemas").glob("*.yaml"))
    return _tree_runtime_hash(paths, relative_to=cow_root)


def runtime_build_manifest(cow_root: Path = COW_ROOT,
                           action_registry: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    manifest = {
        "runtime_version": str(json.loads((ROOT / "plugin.json").read_text(
            encoding="utf-8")).get("version", "unknown")),
        "runtime_behavior_hash": runtime_behavior_hash(cow_root),
        "handler_registry_hash": handler_registry_hash(),
        "validator_registry_hash": validator_registry_hash(),
        "action_contract_hash": action_contract_hash(
            effective_action_registry(action_registry)),
        "cow_runtime_hash": cow_runtime_hash(cow_root),
    }
    manifest["manifest_hash"] = _stable_contract_hash(manifest)
    return manifest


def runtime_behavior_hash(cow_root: Path = COW_ROOT) -> str:
    """Hash all executable enforcement code included in a composed policy."""
    paths = [ROOT / "scripts" / "runtime.py",
             ROOT / "scripts" / "resolve-runtime.py",
             ROOT / "scripts" / "cognitive_runtime.py",
             ROOT / "SISPIS" / "runtime" / "calibrate.py",
             cow_root / "scripts" / "validate-repository-truth.py"]
    paths.extend(sorted((ROOT / "scripts" / "runtime_contract").glob("*.py")))
    if (cow_root / "registry.yaml").is_file():
        paths.append(cow_root / "scripts" / "control_plane.py")
    return _tree_runtime_hash(paths, relative_to=ROOT)


# ---------------------------------------------------------------- pipeline

def load_pipeline() -> Dict[str, Any]:
    return load_pipeline_document(ROOT / "shared" / "pipeline.yaml")


def expand(nodes: Iterable[str], aliases: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for n in nodes:
        if n in aliases:
            out.extend(expand(aliases[n], aliases))
        else:
            out.append(n)
    return out


def canonical_stage_order(stages: Dict[str, Any]) -> List[str]:
    """Canonical order of all stages from the pipeline manifest itself,
    resolved by topologically sorting on order_after edges. Used for stable
    tie-breaking in ordinal kernels: StateWork-added requirements must not
    change lifecycle merely because they were appended later."""
    active = list(stages.keys())
    indeg = {sid: 0 for sid in active}
    edges: Dict[str, List[str]] = {sid: [] for sid in active}
    for sid in active:
        for pred in stages.get(sid, {}).get("order_after", []):
            if pred in active and pred != sid:
                edges[pred].append(sid)
                indeg[sid] += 1
    ready = [sid for sid in active if indeg[sid] == 0]
    ready.sort()
    ordered: List[str] = []
    while ready:
        cur = ready.pop(0)
        ordered.append(cur)
        nxt = sorted(edges[cur])
        for nxt_sid in nxt:
            indeg[nxt_sid] -= 1
            if indeg[nxt_sid] == 0:
                ready.append(nxt_sid)
                ready.sort()
    return ordered


def requires_closure(active: Iterable[str], stages: Dict[str, Any]) -> List[str]:
    """Transitive closure over `requires` edges. The validator computes this;
    the resolver must too, so a profile can never run with a hard dependency
    missing."""
    out = list(dict.fromkeys(active))
    changed = True
    while changed:
        changed = False
        for sid in list(out):
            for req in stages.get(sid, {}).get("requires", []):
                if req not in out:
                    out.append(req)
                    changed = True
    return out


def order_kernel(active: Iterable[str], stages: Dict[str, Any]) -> List[str]:
    """Order the active subset by order_after edges. Ties break by the
    canonical manifest stage order — never by insertion order."""
    canonical = canonical_stage_order(stages)
    canonical_idx = {sid: i for i, sid in enumerate(canonical)}
    active = list(dict.fromkeys(active))
    indeg = {sid: 0 for sid in active}
    edges: Dict[str, List[str]] = {sid: [] for sid in active}
    for sid in active:
        for pred in stages.get(sid, {}).get("order_after", []):
            if pred in active and pred != sid:
                edges[pred].append(sid)
                indeg[sid] += 1
    ready = [sid for sid in active if indeg[sid] == 0]
    ready.sort(key=lambda s: canonical_idx.get(s, len(canonical)))
    ordered: List[str] = []
    while ready:
        cur = ready.pop(0)
        ordered.append(cur)
        nxt = sorted(edges[cur], key=lambda s: canonical_idx.get(s, len(canonical)))
        for nxt_sid in nxt:
            indeg[nxt_sid] -= 1
            if indeg[nxt_sid] == 0:
                ready.append(nxt_sid)
                ready.sort(key=lambda s: canonical_idx.get(s, len(canonical)))
    if len(ordered) != len(active):
        raise ValueError(f"order_after cycle among active kernel: {active}")
    return ordered


# -------------------------------------------------------- StateWork registry

def load_stateworks(cow_root: Path = COW_ROOT) -> List[Dict[str, Any]]:
    reg_file = cow_root / "registry.yaml"
    if not reg_file.exists():
        return []
    registry = load_yaml(reg_file)
    out = []
    for name in registry.get("stateworks", []):
        manifest = cow_root / name / "manifest.yaml"
        if manifest.exists():
            data = load_yaml(manifest)
            data["_manifest_path"] = manifest
            out.append(data)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(text: str) -> set:
    return set(_norm(text).split())


def match_statework(stateworks: List[Dict[str, Any]], domains: Iterable[str],
                    phase: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Deterministic matching:
    1. normalize case + punctuation
    2. exact canonical label match first (manifest id or name)
    3. token/phrase alias match second with weighted score
    4. tie -> explicit ambiguity (never first-match)
    """
    if phase is not None:
        stateworks = [m for m in stateworks if m.get("phase") == phase]
    if not stateworks:
        return None, []
    domain_list = [d.strip() for d in domains if d.strip()]
    if not domain_list:
        return None, []
    exact_hits: List[Dict[str, Any]] = []
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for m in stateworks:
        labels = [str(m.get("id", "")), str(m.get("name", ""))]
        normalized_labels = [_norm(l) for l in labels]
        triggers = [str(t) for t in m.get("triggers", [])]
        target_tokens = set().union(*[_tokens(d) for d in domain_list]) if domain_list else set()
        for d in domain_list:
            nd = _norm(d)
            if nd in normalized_labels or any(nd == _norm(t) for t in triggers):
                exact_hits.append(m)
        if exact_hits:
            continue
        best = 0.0
        for t in triggers:
            tt = _tokens(t)
            if target_tokens and tt:
                overlap = len(tt & target_tokens) / len(tt)
                best = max(best, overlap)
        if best > 0:
            scored.append((best, m))
    if exact_hits:
        unique = {id(m): m for m in exact_hits}
        if len(unique) == 1:
            return next(iter(unique.values())), []
        return None, [m.get("id") for m in unique.values()]
    if scored:
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id"))))
        top = scored[0][0]
        winners = [m for score, m in scored if abs(score - top) < 1e-9]
        if len(winners) == 1:
            return winners[0], []
        return None, [m.get("id") for m in winners]
    return None, []


# ------------------------------------------------- packet-prerequisite chain

def parse_packet_registry(cow_root: Path = COW_ROOT) -> Dict[str, Any]:
    reg = cow_root / "schemas" / "packet-registry.yaml"
    if not reg.exists():
        return {"packets": {}}
    data = load_yaml(reg)
    return data if isinstance(data, dict) else {"packets": {}}


def _consumed_inputs(sw: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Required and optional consumed packets. A required packet that is
    missing routes its registered producer first; optional packets never
    recurse (Infrae may accept a prior truth packet when resuming, but never
    requires it)."""
    inputs = sw.get("inputs")
    if isinstance(inputs, dict):
        required = [p for p in inputs.get("required", []) if p != "task_context"]
        optional = [p for p in inputs.get("optional", []) if p != "task_context"]
    else:
        # legacy flat `consumes` bag — treat everything as required
        required = [p for p in sw.get("consumes", []) if p != "task_context"]
        optional = []
    return required, optional


def _load_cow_packet_store(packets: Optional[Iterable[Dict[str, Any]]],
                           cow_root: Path = COW_ROOT):
    """Load CognitiveStateWork's validated PacketStore without importing DP."""
    module_path = cow_root / "scripts" / "control_plane.py"
    if not module_path.exists():
        raise PacketDependencyError("CognitiveStateWork control_plane.py is missing")
    spec = importlib.util.spec_from_file_location("cfw_cow_control_plane", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cfw_cow_control_plane"] = module
    spec.loader.exec_module(module)
    return module.PacketStore(
        packets, packet_registry_path=cow_root / "schemas" / "packet-registry.yaml")


def plan_statework_chain(statework: Optional[Dict[str, Any]], stateworks: List[Dict[str, Any]],
                         packet_registry: Dict[str, Any],
                         packet_store: Optional[Dict[str, Dict[str, Any]]] = None,
                         allow_missing_required: bool = True,
                         missing_required: Optional[List[str]] = None,
                         task_id: Optional[str] = None,
                         subject_ref: Optional[str] = None,
                         observation_context: Optional[Mapping[str, Any]] = None,
                         cow_root: Path = COW_ROOT) -> List[Dict[str, Any]]:
    """Given a desired StateWork, walk its required consumed packet types.
    Any required packet that is not present/fresh routes a registered
    producer (from packet-registry.yaml) first. Returns the ordered chain.

    DFS with explicit visiting set: a future A -> B -> C -> A contract fails
    with PacketDependencyCycle instead of recursing forever. Unknown required
    packets raise PacketDependencyError (never silently skipped)."""
    if statework is None:
        return []
    by_id = {m.get("id"): m for m in stateworks}
    producers = packet_registry.get("packets", {})
    valid_packets = [value for value in (packet_store or {}).values()
                     if isinstance(value, dict) and value.get("packet_type")]
    try:
        store = _load_cow_packet_store(valid_packets, cow_root=cow_root)
    except Exception as exc:
        raise PacketValidationError(str(exc)) from exc
    missing = missing_required if missing_required is not None else []
    chain: List[Dict[str, Any]] = []
    resolved: Set[str] = set()
    visiting: Set[str] = set()

    def resolve(sw: Dict[str, Any]) -> None:
        sid = sw.get("id")
        if sid in resolved:
            return
        if sid in visiting:
            raise PacketDependencyCycle(
                f"packet dependency cycle detected at StateWork {sid!r} "
                f"(chain: {sorted(visiting)} -> {sid})")
        visiting.add(sid)
        required, _optional = _consumed_inputs(sw)
        for packet in required:
            meta = producers.get(packet)
            if not meta:
                if allow_missing_required:
                    missing.append(packet)
                    continue
                raise PacketDependencyError(
                    f"required packet {packet!r} consumed by {sid!r} is not "
                    f"registered in packet-registry.yaml")
            packet_task = task_id or "task-unspecified"
            # Subject identity is part of the packet contract.  A task id is
            # not a substitute for the branch/repository/entity being
            # observed; accepting that fallback would permit cross-subject
            # truth to satisfy a prerequisite.
            expected_subject = subject_ref
            if not expected_subject:
                current = None
            else:
                current = _current_packet(
                    store, packet, task_id=packet_task, subject_ref=expected_subject,
                    observation_context=observation_context)
            if current is not None:
                continue  # validated typed packet satisfies prerequisite
            producer_id = meta.get("producer")
            producer = by_id.get(producer_id)
            if producer is None:
                raise PacketDependencyError(
                    f"packet {packet} declares producer {producer_id!r} but no "
                    f"StateWork manifest exists")
            resolve(producer)
        visiting.remove(sid)
        resolved.add(sid)
        chain.append(sw)

    resolve(statework)
    return chain


# ---------------------------------------------------------------- guard pack

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


# ------------------------------------------------------- immutable snapshot

@dataclass(frozen=True)
class PolicySnapshot:
    """Immutable pin of everything a task runs under. Normal task policy
    cannot mutate. A safety containment creates an EmergencyOverride record;
    the next task gets a new snapshot."""
    task_id: str
    kernel: Tuple[str, ...]
    stateworks: Tuple[StateWorkPin, ...]
    guard_pack_version: str
    guard_pack_hash: str
    framework_version: str
    framework_hash: str
    model: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    model_revision: Optional[str]
    model_capability_hash: Optional[str]
    harness: Optional[str]
    harness_version: Optional[str]
    toolset: Optional[str]
    namespace_id: str
    application_id: str
    application_version: Optional[str]
    application_instance_id: str
    frozen_at: str
    policy_hash: str = ""
    subject_ref: Optional[str] = None
    contracts: Mapping[str, Any] = field(default_factory=dict)
    phase: Optional[str] = None
    domain_tags: Tuple[str, ...] = ()
    task_shape: str = "implement"
    operation: Optional[str] = None
    trigger: Optional[str] = None
    execution_plan: Tuple[Dict[str, Any], ...] = ()
    agent_id: str = "agent"
    agent_instance_id: str = "agent"
    attempt_id: str = "attempt-1"
    interaction_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    delegator_agent_id: Optional[str] = None
    delegate_agent_id: Optional[str] = None
    delegation_id: Optional[str] = None
    role: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", _freeze_contracts(self.contracts))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kernel": list(self.kernel),
            "stateworks": [pin.to_dict() for pin in self.stateworks],
            "guard_pack_version": self.guard_pack_version,
            "guard_pack_hash": self.guard_pack_hash,
            "framework_version": self.framework_version,
            "framework_hash": self.framework_hash,
            "model": self.model,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_revision": self.model_revision,
            "model_capability_hash": self.model_capability_hash,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "toolset": self.toolset,
            "namespace_id": self.namespace_id,
            "application_id": self.application_id,
            "application_version": self.application_version,
            "application_instance_id": self.application_instance_id,
            "frozen_at": self.frozen_at,
            "policy_hash": self.policy_hash,
            "subject_ref": self.subject_ref,
            "agent_id": self.agent_id,
            "agent_instance_id": self.agent_instance_id,
            "attempt_id": self.attempt_id,
            "interaction_id": self.interaction_id,
            "parent_session_id": self.parent_session_id,
            "delegator_agent_id": self.delegator_agent_id,
            "delegate_agent_id": self.delegate_agent_id,
            "delegation_id": self.delegation_id,
            "role": self.role,
            "phase": self.phase,
            "domain_tags": list(self.domain_tags),
            "task_shape": self.task_shape,
            "operation": self.operation,
            "trigger": self.trigger,
            "execution_plan": [_thaw(item) for item in self.execution_plan],
            "contracts": _thaw(self.contracts),
        }


@dataclass(frozen=True)
class EmergencyOverride:
    """Separate event: a safety containment changes the effective pack for
    the remainder of a task. It never mutates the PolicySnapshot; future
    tasks compile a fresh snapshot."""
    task_id: str
    override_reason: str
    rolled_back_to_guard_pack: str
    recorded_at: str


@dataclass(frozen=True)
class CapsulePin:
    owner: str
    stage: str
    version: str
    content_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner, "stage": self.stage,
                "version": self.version, "content_hash": self.content_hash}


@dataclass(frozen=True)
class StateWorkPin:
    id: str
    version: str
    manifest_hash: str
    runtime_hash: str
    selected_flow: Optional[Tuple[str, str]]
    selected_specialists: Tuple[Tuple[str, str], ...]
    transitions_hash: str = ""
    transitions_contract: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "manifest_hash": self.manifest_hash,
            "runtime_hash": self.runtime_hash,
            "selected_flow": (
                {"path": self.selected_flow[0], "content_hash": self.selected_flow[1]}
                if self.selected_flow else None
            ),
            "selected_specialists": [
                {"path": path, "content_hash": content_hash}
                for path, content_hash in self.selected_specialists
            ],
            "transitions_hash": self.transitions_hash,
            "transitions_contract": _thaw(self.transitions_contract),
        }


@dataclass(frozen=True)
class GuardPin:
    guard_key: str
    rule_hash: str
    status_at_resolution: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "guard_key": self.guard_key,
            "rule_hash": self.rule_hash,
            "status_at_resolution": self.status_at_resolution,
        }


@dataclass(frozen=True)
class EffectivePolicy:
    profile: str
    kernel: Tuple[str, ...]
    capsules: Tuple[CapsulePin, ...]
    stateworks: Tuple[StateWorkPin, ...]
    guards: Tuple[GuardPin, ...]
    model: Optional[str]
    model_id: Optional[str]
    provider_id: Optional[str]
    model_revision: Optional[str]
    model_capability_hash: Optional[str]
    harness: Optional[str]
    harness_version: Optional[str]
    toolset: Optional[str]
    namespace_id: str
    application_id: str
    application_version: Optional[str]
    application_instance_id: str
    trigger: Optional[str]
    pipeline_hash: str
    runtime_kernel_hash: str
    calibration_hash: str
    subject_ref: Optional[str] = None
    signal_schema_hash: str = ""
    signal_registry_hash: str = ""
    behavior_event_schema_hash: str = ""
    behavior_event_schema_version: str = ""
    runtime_behavior_hash: str = ""
    validator_registry_hash: str = ""
    handler_registry_hash: str = ""
    action_contract_hash: str = ""
    cow_runtime_hash: str = ""
    runtime_build_manifest_hash: str = ""
    routing_profile_hash: str = ""
    policy_contract_version: str = "2.1.0"

    def canonical(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "kernel": list(self.kernel),
            "capsules": [pin.to_dict() for pin in self.capsules],
            "stateworks": [pin.to_dict() for pin in self.stateworks],
            "guards": [pin.to_dict() for pin in self.guards],
            "model": self.model,
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_revision": self.model_revision,
            "model_capability_hash": self.model_capability_hash,
            "harness": self.harness,
            "harness_version": self.harness_version,
            "toolset": self.toolset,
            "namespace_id": self.namespace_id,
            "application_id": self.application_id,
            "application_version": self.application_version,
            "application_instance_id": self.application_instance_id,
            "trigger": self.trigger,
            "pipeline_hash": self.pipeline_hash,
            "runtime_kernel_hash": self.runtime_kernel_hash,
            "calibration_hash": self.calibration_hash,
            "subject_ref": self.subject_ref,
            "signal_schema_hash": self.signal_schema_hash,
            "signal_registry_hash": self.signal_registry_hash,
            "behavior_event_schema_hash": self.behavior_event_schema_hash,
            "behavior_event_schema_version": self.behavior_event_schema_version,
            "runtime_behavior_hash": self.runtime_behavior_hash,
            "validator_registry_hash": self.validator_registry_hash,
            "handler_registry_hash": self.handler_registry_hash,
            "action_contract_hash": self.action_contract_hash,
            "cow_runtime_hash": self.cow_runtime_hash,
            "runtime_build_manifest_hash": self.runtime_build_manifest_hash,
            "routing_profile_hash": self.routing_profile_hash,
            "policy_contract_version": self.policy_contract_version,
        }


# ------------------------------------------------------------------ runtime

def _frozen_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen_mapping(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen_mapping(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value

def load_runtime_capsules() -> Dict[str, Dict[str, Any]]:
    """Load compact runtime capsules for core protocols (runtime.md), falling
    back to SKILL.md frontmatter description only when the capsule is
    absent. Used to build the effective injected instruction surface."""
    out: Dict[str, Dict[str, Any]] = {}
    for name in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS", "cogframe"]:
        skill_dir = ROOT / name
        capsule_path = skill_dir / "runtime.md"
        if (skill_dir / "SKILL.md").exists():
            out[name] = {"skill_words": len((skill_dir / "SKILL.md").read_text(encoding="utf-8").split())}
            if capsule_path.exists():
                out[name]["capsule_words"] = len(capsule_path.read_text(encoding="utf-8").split())
                out[name]["capsule"] = capsule_path.read_text(encoding="utf-8")
    return out


def runtime_state_dir() -> Path:
    """Per-task immutable out-of-tree destination. Never overwrites a
    snapshot (content-addressed by policy hash). Prefers $XDG_RUNTIME_DIR;
    falls back to the system temp dir when the runtime dir is not writable
    (e.g. sandboxes/read-only mounts). Never writes into the source tree."""
    base_candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base_candidates.append(Path(xdg))
    # A shared /tmp/cognitiveframeworks directory lets another local user
    # replace policy/telemetry state.  The fallback is deliberately UID
    # private and is rejected if an existing path is a symlink or another
    # user's directory.
    base_candidates.append(Path(tempfile.gettempdir()))
    uid = os.getuid()
    for base in base_candidates:
        leaf = "cognitiveframeworks" if xdg and base == Path(xdg) else f"cognitiveframeworks-{uid}"
        candidate = base / leaf
        try:
            if candidate.is_symlink():
                continue
            candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
            stat = candidate.stat()
            if stat.st_uid != uid:
                continue
            os.chmod(candidate, 0o700)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise OSError("no writable runtime state directory available for cognitiveframeworks snapshots")


def compute_policy_hash(policy: EffectivePolicy) -> str:
    return _stable_contract_hash(policy.canonical())


def _policy_from_bundle(bundle: "RuntimeBundle") -> EffectivePolicy:
    expected = compute_policy_hash(bundle.policy)
    if bundle.pinned.get("policy_hash") != expected:
        raise SnapshotIntegrityError(
            f"runtime bundle policy hash mismatch: pinned={bundle.pinned.get('policy_hash')!r} "
            f"computed={expected!r}")

    capsules = tuple(
        CapsulePin(owner=stage.rsplit(":", 1)[-1].split("_")[0].upper(),
                   stage=stage, version=bundle.bundle_version,
                   content_hash=sha256_bytes(content.encode("utf-8")))
        for stage, (content, _trusted_hash) in sorted(bundle.injected_capsules)
    )
    guards = tuple(
        GuardPin(guard_key=guard.get("key") or guard["id"],
                 rule_hash=semantic_guard_hash(guard),
                 status_at_resolution=guard.get("status", "active"))
        for guard in bundle.guard_pack.get("guards", [])
    )
    stateworks = tuple(
        StateWorkPin(
            id=item["id"], version=item["version"],
            manifest_hash=item["manifest_hash"], runtime_hash=item["runtime_hash"],
            selected_flow=(tuple(item["selected_flow"])
                           if item.get("selected_flow") else None),
            selected_specialists=tuple(
                tuple(pair) for pair in item.get("selected_specialists", [])),
            transitions_hash=item.get("transitions_hash", ""),
            transitions_contract=item.get("transitions_contract", {}))
        for item in bundle.stateworks
    )
    if (capsules, guards, stateworks) != (bundle.policy.capsules,
                                          bundle.policy.guards,
                                          bundle.policy.stateworks):
        differences = []
        if capsules != bundle.policy.capsules:
            differences.append("capsules")
        if guards != bundle.policy.guards:
            differences.append("guards")
        if stateworks != bundle.policy.stateworks:
            differences.append("stateworks")
        raise SnapshotIntegrityError(
            "runtime bundle content differs from its pinned policy: " + ",".join(differences))
    return bundle.policy


@dataclass(frozen=True)
class RuntimeBundle:
    """Composed, immutable runtime policy for a single task."""
    bundle_version: str
    task_id: str
    profile: str
    kernel: Tuple[str, ...]
    kernel_instructions: Tuple[Dict[str, Any], ...]
    stateworks: Tuple[Dict[str, Any], ...]
    handoff_plan: Tuple[Dict[str, Any], ...]
    guard_pack: Dict[str, Any]
    pinned: Dict[str, Any]
    policy: EffectivePolicy
    capsule_hashes: Dict[str, str]
    injected_capsules: Tuple[Tuple[str, Tuple[str, str]], ...]
    instruction_words: int
    execution_plan: Tuple[Dict[str, Any], ...] = ()
    # Runtime-only source root.  It is intentionally not serialized into the
    # policy snapshot because absolute installation paths are not policy
    # identity; the executable COW hash is pinned in EffectivePolicy.
    statework_root: str = ""
    # Host-owned descriptors composed before resolution.  Runtime-only so
    # absolute/source-specific objects never become policy artifact fields.
    action_registry: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kernel", tuple(self.kernel))
        object.__setattr__(self, "kernel_instructions", tuple(self.kernel_instructions))
        object.__setattr__(self, "stateworks", tuple(self.stateworks))
        object.__setattr__(self, "handoff_plan", tuple(self.handoff_plan))
        object.__setattr__(self, "guard_pack", _frozen_mapping(self.guard_pack))
        object.__setattr__(self, "pinned", _frozen_mapping(self.pinned))
        object.__setattr__(self, "capsule_hashes", _frozen_mapping(self.capsule_hashes))
        object.__setattr__(self, "injected_capsules", tuple(
            (key, tuple(value)) for key, value in self.injected_capsules
        ))
        object.__setattr__(self, "execution_plan", tuple(self.execution_plan))
        object.__setattr__(self, "action_registry",
                           MappingProxyType(dict(self.action_registry)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundle_version": self.bundle_version,
            "task_id": self.task_id,
            "profile": self.profile,
            "kernel": list(self.kernel),
            "kernel_instructions": [_thaw(item) for item in self.kernel_instructions],
            "stateworks": [_thaw(item) for item in self.stateworks],
            "handoff_plan": [_thaw(item) for item in self.handoff_plan],
            "guard_pack": _thaw(self.guard_pack),
            "pinned": _thaw(self.pinned),
            "capsule_hashes": _thaw(self.capsule_hashes),
            "injected_capsules": [[stage, [text, content_hash]]
                                  for stage, (text, content_hash) in self.injected_capsules],
            "instruction_words": self.instruction_words,
            "execution_plan": [_thaw(item) for item in self.execution_plan],
        }


def injected_capsules_for(kernel: Iterable[str]) -> Dict[str, str]:
    """The compact runtime capsules actually injected for the active kernel
    stages. This is the effective instruction surface the model sees — not
    the authoring SKILL.md and not activation metadata."""
    out: Dict[str, str] = {}
    for stage_id in kernel:
        name = stage_id.split("_")[0].upper()
        if name == "SISPIS":
            path = ROOT / "SISPIS" / "runtime.md"
        else:
            path = ROOT / name / "runtime.md"
        if path.exists():
            out[stage_id] = path.read_text(encoding="utf-8")
    return out


def select_statework_runtime(
        sw: Dict[str, Any], cow_root: Path,
        requested_flow: Optional[str] = None, *, operation: Optional[str] = None,
        trigger: Optional[str] = None, task_shape: Optional[str] = None,
        current_state: Optional[str] = None,
        domain_tags: Optional[Iterable[str]] = None,
        suppressed_specialists: Optional[Iterable[str]] = None,
        preferred_specialists: Optional[Iterable[str]] = None,
        suppressed_flows: Optional[Iterable[str]] = None) -> Tuple[Dict[str, Any], Dict[str, Tuple[str, str]]]:
    """Select and pin the StateWork's active hierarchical runtime bytes.

    Selection contract: exactly one flow when flows exist; all required
    specialists; optional specialists until the StateWork budget's selected
    specialist limit. Missing/ambiguous flow declarations fail closed."""
    sid = str(sw.get("id"))
    item = {
        "id": sid, "version": sw.get("version", "0.0.0"),
        "entrypoint": sw.get("entrypoint"),
        "manifest_hash": read_manifest_hash(sw),
    }
    runtime_path = cow_root / sid / "runtime.md"
    if not runtime_path.exists():
        raise PacketDependencyError(f"StateWork {sid!r} is missing its runtime capsule")
    runtime_text = runtime_path.read_text(encoding="utf-8")
    runtime_hash = sha256(runtime_path)
    item["runtime_hash"] = runtime_hash

    capsules: Dict[str, Tuple[str, str]] = {
        f"statework:{sid}": (runtime_text, runtime_hash)
    }
    flows_dir = cow_root / sid / "flows"
    flow_paths = sorted(flows_dir.glob("*/flow.md")) if flows_dir.is_dir() else []
    selected_flow_path: Optional[Path] = None
    selected_flow_name = None
    specialists: list[Tuple[Path, str]] = []
    suppressed_specialists = set(suppressed_specialists or ())
    preferred_specialists = set(preferred_specialists or ())
    suppressed_flows = set(suppressed_flows or ())
    if flow_paths:
        named: Dict[str, Path] = {
            path.parent.name: path for path in flow_paths
        }
        import yaml
        selectors: Dict[str, Dict[str, Any]] = {}
        for name, path in named.items():
            if name in suppressed_flows:
                continue
            pieces = path.read_text(encoding="utf-8").split("---", 2)
            if len(pieces) < 3:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow {name!r} is missing machine-readable frontmatter")
            try:
                meta = yaml.safe_load(pieces[1]) or {}
            except Exception as exc:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow {name!r} has invalid frontmatter") from exc
            if not isinstance(meta, dict):
                raise PacketDependencyError(f"StateWork {sid!r} flow {name!r} frontmatter must be a mapping")
            selectors[name] = meta
        tags = set(domain_tags or ())
        scored: List[Tuple[int, str]] = []
        for name, meta in selectors.items():
            score = 0
            operations = set(meta.get("operations") or [])
            triggers = set(meta.get("triggers") or [])
            shapes = set(meta.get("task_shapes") or [])
            flow_tags = set(meta.get("domain_tags") or [])
            states = meta.get("states") or {}
            if operation is not None:
                if operations and operation not in operations:
                    continue
                score += int(operation in operations)
            if trigger is not None:
                if triggers and trigger not in triggers:
                    continue
                score += int(trigger in triggers)
            if task_shape is not None:
                if shapes and task_shape not in shapes:
                    continue
                score += int(task_shape in shapes)
            if current_state is not None:
                state_from = set(states.get("from") or []) if isinstance(states, dict) else set()
                if state_from and current_state not in state_from:
                    continue
                score += int(current_state in state_from)
            if tags and flow_tags:
                score += len(tags & flow_tags)
            scored.append((score, name))
        if requested_flow:
            eligible_names = {name for _score, name in scored}
            if requested_flow not in eligible_names:
                raise PacketDependencyError(
                    f"StateWork {sid!r} requested flow {requested_flow!r} is not eligible "
                    f"for operation={operation!r}, trigger={trigger!r}, "
                    f"shape={task_shape!r}, state={current_state!r}")
            selected_flow_path = named[requested_flow]
        elif not scored:
            raise PacketDependencyError(
                f"StateWork {sid!r} has no flow matching operation={operation!r}, "
                f"trigger={trigger!r}, shape={task_shape!r}, state={current_state!r}")
        else:
            scored.sort(key=lambda pair: (-pair[0], pair[1]))
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow selection is ambiguous: "
                    f"{[name for score, name in scored if score == scored[0][0]]}")
            selected_flow_path = named[scored[0][1]]
        if selected_flow_path is not None:
            try:
                selected_flow_path.resolve().relative_to(cow_root.resolve())
            except ValueError as exc:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow escapes its owner boundary") from exc
            selected_flow_name = selected_flow_path.parent.name
            text = selected_flow_path.read_text(encoding="utf-8")
            capsules[f"statework-flow:{sid}:{selected_flow_name}"] = (
                text, sha256(selected_flow_path))
            try:
                import yaml
                frontmatter = yaml.safe_load(text.split("---", 2)[1]) or {}
            except Exception as exc:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow {selected_flow_name!r} has invalid frontmatter") from exc
            required = frontmatter.get("required_specialists") or []
            optional = frontmatter.get("optional_specialists") or []
            if frontmatter.get("flow") != selected_flow_name:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow metadata name does not match directory {selected_flow_name!r}")
            flow_schema_path = cow_root / "schemas" / "statework-flow.schema.json"
            if flow_schema_path.exists():
                try:
                    import jsonschema
                    jsonschema.validate(
                        instance=frontmatter,
                        schema=load_yaml(flow_schema_path) if flow_schema_path.suffix in {".yaml", ".yml"}
                        else _load_json(flow_schema_path))
                except Exception as exc:
                    raise PacketDependencyError(
                        f"StateWork {sid!r} flow {selected_flow_name!r} violates its schema") from exc
            if not isinstance(required, list) or not isinstance(optional, list):
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow {selected_flow_name!r} specialist lists are invalid")
            budget_limit = {"low": 1, "medium": 2, "high": 3}.get(
                sw.get("context_budget", "medium"), 2)
            relative_root = selected_flow_path.parent
            owner_root = (cow_root / sid).resolve()
            candidates: list[Tuple[str, bool]] = [
                (str(path), True) for path in required
            ] + sorted(((str(path), False) for path in optional),
                       key=lambda item: (item[0] not in preferred_specialists, item[0]))
            for relative, is_required in candidates:
                if relative in suppressed_specialists or Path(relative).name in suppressed_specialists:
                    if is_required:
                        raise PacketDependencyError(
                            f"StateWork {sid!r} required specialist {relative!r} cannot be suppressed")
                    continue
                if not is_required and len(specialists) >= budget_limit:
                    continue
                path = (relative_root / relative).resolve()
                try:
                    path.relative_to(owner_root)
                except ValueError as exc:
                    raise PacketDependencyError(
                        f"StateWork {sid!r} specialist escapes its owner boundary: {relative!r}") from exc
                if not path.is_file():
                    raise PacketDependencyError(
                        f"StateWork {sid!r} flow {selected_flow_name!r} specialist {relative!r} is missing")
                specialists.append((path, sha256(path)))
            if len(specialists) > budget_limit:
                raise PacketDependencyError(
                    f"StateWork {sid!r} selected specialists exceed budget "
                    f"({len(specialists)} > {budget_limit})")
            item["flow_requirements"] = list(frontmatter.get("framework_requirements") or [])
            item["flow_states"] = dict(frontmatter.get("states") or {})
            item["flow_metadata"] = {
                key: frontmatter.get(key)
                for key in ("flow", "operations", "triggers", "task_shapes",
                            "domain_tags", "states", "framework_requirements",
                            "required_specialists", "optional_specialists")
                if key in frontmatter
            }
    for index, (path, content_hash) in enumerate(specialists):
        capsules[f"statework-specialist:{sid}:{index}"] = (
            path.read_text(encoding="utf-8"), content_hash)
    item["selected_flow"] = (
        [str(selected_flow_path.relative_to(cow_root)), sha256(selected_flow_path)]
        if selected_flow_path else None
    )
    item["selected_specialists"] = [
        [str(path.relative_to(cow_root)), content_hash]
        for path, content_hash in specialists
    ]
    return item, capsules


def _statework_runtime_pins(
        chain: List[Dict[str, Any]], cow_root: Path,
        requested_flows: Optional[Dict[str, str]] = None, *, operation: Optional[str] = None,
        trigger: Optional[str] = None, task_shape: Optional[str] = None,
        current_state: Optional[str] = None,
        domain_tags: Optional[Iterable[str]] = None,
        routing_adjustments: Optional[Iterable[Mapping[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    """Pin every selected StateWork runtime byte for the task."""
    enriched: List[Dict[str, Any]] = []
    capsules: Dict[str, Tuple[str, str]] = {}
    requested_flows = requested_flows or {}
    suppressed_by_statework: Dict[str, set[str]] = {}
    preferred_by_statework: Dict[str, set[str]] = {}
    suppressed_flows_by_statework: Dict[str, set[str]] = {}
    for adjustment in routing_adjustments or ():
        owner = str(adjustment.get("statework_id") or "")
        if adjustment.get("route_type") == "specialist":
            if adjustment.get("disposition") == "suppress":
                suppressed_by_statework.setdefault(owner, set()).add(str(adjustment.get("route")))
            elif adjustment.get("disposition") == "prefer":
                preferred_by_statework.setdefault(owner, set()).add(str(adjustment.get("route")))
        if adjustment.get("route_type") == "flow" and adjustment.get("disposition") == "suppress":
            suppressed_flows_by_statework.setdefault(owner, set()).add(str(adjustment.get("route")))
    for sw in chain:
        item, selected = select_statework_runtime(
            sw, cow_root, requested_flows.get(str(sw.get("id"))), operation=operation,
            trigger=trigger, task_shape=task_shape, current_state=current_state,
            domain_tags=domain_tags,
            suppressed_specialists=suppressed_by_statework.get(str(sw.get("id")), ()),
            preferred_specialists=preferred_by_statework.get(str(sw.get("id")), ()),
            suppressed_flows=suppressed_flows_by_statework.get(str(sw.get("id")), ()))
        transition_path = cow_root / str(sw.get("id")) / "transitions.yaml"
        if transition_path.exists():
            item["transitions_hash"] = sha256(transition_path)
            item["transitions_contract"] = load_yaml(transition_path)
            transition_schema_path = cow_root / "schemas" / "transition-contract.schema.json"
            if transition_schema_path.exists():
                try:
                    import jsonschema
                    jsonschema.validate(instance=item["transitions_contract"],
                                        schema=_load_json(transition_schema_path))
                except Exception as exc:
                    raise PacketDependencyError(
                        f"StateWork {sw.get('id')!r} transition contract violates its schema") from exc
            declared_states = set(item.get("transitions_contract", {}).get("states") or [])
            flow_states = item.get("flow_states") or {}
            referenced = set((flow_states.get("from") or []) +
                             (flow_states.get("through") or []))
            if referenced and not referenced.issubset(declared_states):
                raise PacketDependencyError(
                    f"StateWork {sw.get('id')!r} flow references undeclared states "
                    f"{sorted(referenced - declared_states)}")
            item["transition_requirements"] = sorted({
                requirement
                for edge in item["transitions_contract"].get("transitions", [])
                if trigger is None or edge.get("trigger") == trigger
                for requirement in edge.get("framework_requirements", [])
            })
        enriched.append(item)
        capsules.update(selected)
    return enriched, capsules


def resolve(task_id: Union[str, TaskRequest], shape: str = "implement", domains: Optional[Iterable[str]] = None,
            model: Optional[str] = None, harness: Optional[str] = None,
            toolset: Optional[str] = None, trigger: Optional[str] = None,
            packet_store: Optional[Dict[str, Dict[str, Any]]] = None,
            cow_root: Path = COW_ROOT,
            allow_missing_required: bool = False,
            missing_required: Optional[List[str]] = None,
            requested_flows: Optional[Dict[str, str]] = None,
            subject_ref: Optional[str] = None,
            phase: Optional[str] = None,
            operation: Optional[str] = None,
            observation_context: Optional[Mapping[str, Any]] = None,
            agent_id: str = "agent",
            application_id: str = "cognitiveframeworks-runtime",
            application_version: Optional[str] = None,
            application_instance_id: Optional[str] = None,
            provider_id: Optional[str] = None,
            model_id: Optional[str] = None,
            model_revision: Optional[str] = None,
            model_capability_hash: Optional[str] = None,
            harness_version: Optional[str] = None,
            namespace_id: str = "default",
            action_registry: Optional[Mapping[str, Any]] = None,
            routing_profile_path: Optional[Path] = None,
            behavioral_advisor: Optional[BehavioralAdvisor] = None) -> RuntimeBundle:
    """Pure library entry point: TaskRequest -> RuntimeBundle. No source-tree
    writes (the CLI snapshot writer is separate and out-of-tree)."""
    if isinstance(task_id, TaskRequest):
        request = task_id
        task_id = request.task_id
        application_id = request.application_id
        application_version = request.application_version
        application_instance_id = request.application_instance_id
        namespace_id = request.namespace_id
        shape = request.shape
        domains = list(request.domain_tags)
        subject_ref = request.subject_ref
        agent_id = request.agent_id
        agent_instance_id = request.agent_instance_id or request.agent_id
        attempt_id = request.attempt_id
        interaction_id = request.interaction_id
        parent_session_id = request.parent_session_id
        delegator_agent_id = request.delegator_agent_id
        delegate_agent_id = request.delegate_agent_id
        delegation_id = request.delegation_id
        role = request.role
        routing_experiment_plan = dict(request.routing_experiment_plan)
        phase = request.phase
        operation = request.operation
        trigger = request.trigger
        model = request.model
        model_id = request.model_id
        provider_id = request.provider_id
        model_revision = request.model_revision
        model_capability_hash = request.model_capability_hash
        harness = request.harness
        harness_version = request.harness_version
        toolset = request.toolset
        observation_context = dict(request.observation_context)
        if action_registry is None:
            action_registry = request.action_registry
    else:
        domains = list(domains or [])
        phase = None
        agent_instance_id = agent_id
        attempt_id = "attempt-1"
        interaction_id = None
        parent_session_id = None
        delegator_agent_id = None
        delegate_agent_id = None
        delegation_id = None
        role = None
        routing_experiment_plan = {}
        application_instance_id = application_instance_id or application_id
        model_id = model_id or model
    application_id = str(application_id or "")
    namespace_id = str(namespace_id or "")
    application_instance_id = str(application_instance_id or "")
    if not application_id or not namespace_id or not application_instance_id:
        raise ValueError("application_id, namespace_id, and application_instance_id are required")
    if model is not None and model_id is not None and model != model_id:
        raise ValueError("model and model_id must agree when both are provided")
    model_id = model_id or model
    model = model_id
    task_id = str(task_id)
    agent_id = str(agent_id or "agent")
    effective_actions = effective_action_registry(action_registry)
    domains = list(domains or [])
    pipeline = load_pipeline()
    profiles = pipeline["profiles"]
    aliases = pipeline["aliases"]
    stages = pipeline["stages"]

    routing_profiles, routing_pack_hash = load_routing_profiles(routing_profile_path)
    routing_profile = ({"routing_pack_version": "1.0.0",
                        "semantic_hash": routing_pack_hash,
                        "profiles": routing_profiles}
                       if len(routing_profiles) != 1
                       else (routing_profiles[0] if routing_profiles else None))
    routing_plugin = _load_document(ROOT / "plugin.json", _load_json)
    routing_pack_for_context = load_guard_pack()
    routing_context = {
        "task_id": task_id,
        "session_id": None,
        "attempt_id": attempt_id,
        "agent_instance_id": agent_instance_id,
        "namespace_id": namespace_id, "application_id": application_id,
        "application_version": application_version,
        "application_instance_id": application_instance_id,
        "provider_id": provider_id, "model_id": model_id,
        "model_revision": model_revision,
        "model_capability_hash": model_capability_hash,
        "model": model, "harness": harness, "harness_id": harness,
        "harness_version": harness_version, "toolset": toolset,
        "task_family": shape, "task_shape": shape, "domain_tags": domains,
        "phase": phase, "environment": os.environ.get("CFW_ENVIRONMENT", "runtime"),
        "statework_versions": {str(item.get("id")): str(item.get("version"))
                                for item in load_stateworks(cow_root)},
        "framework_version": routing_plugin.get("version", "unknown"),
        "guard_pack_hash": routing_pack_for_context.get("semantic_hash"),
    }
    routing_context["comparison_context_hash"] = comparison_context_hash(routing_context)
    routing_adjustments = applicable_adjustments(routing_profiles, routing_context)
    advice_receipt: Optional[Dict[str, Any]] = None
    routing_experiment = None
    routing_cohort = None
    candidate_profile_hash = None
    candidate_adjustment_applied = False
    comparison_hash = comparison_context_hash(routing_context)
    if routing_experiment_plan:
        routing_experiment_plan = validate_experiment_plan(routing_experiment_plan)
        routing_cohort = assign_experiment_cohort(
            routing_experiment_plan,
            f"{agent_instance_id}:{task_id}:{attempt_id}", routing_context)
        candidate_profile_hash = routing_experiment_plan.get("candidate_profile_hash")
        candidate_adjustment = dict(routing_experiment_plan.get("candidate_adjustment") or {})
        if routing_cohort == "treatment":
            candidate_adjustment_applied = True
            routing_adjustments.append({
                **candidate_adjustment,
                "profile_id": f"experiment:{routing_experiment_plan.get('experiment_id')}",
                "profile_hash": candidate_profile_hash,
                "experiment_id": routing_experiment_plan.get("experiment_id"),
                "experiment_plan_hash": routing_experiment_plan.get("plan_hash"),
                "candidate_adjustment_applied": True,
            })
        routing_experiment = {
            **routing_experiment_plan,
            "routing_cohort": routing_cohort,
            "eligible": routing_cohort is not None,
            "cohort_policy_identity": (
                routing_experiment_plan.get("treatment_policy_identity")
                if routing_cohort == "treatment"
                else routing_experiment_plan.get("control_policy_identity")
                if routing_cohort in {"control", "holdout"} else None),
            "candidate_profile_hash": candidate_profile_hash,
            "candidate_adjustment_applied": candidate_adjustment_applied,
            "comparison_context_hash": comparison_hash,
        }

    if shape not in profiles:
        raise ValueError(f"unknown task shape {shape!r}; expected one of {sorted(profiles)}")

    profile = expand(profiles[shape], aliases)
    # Build the host-computed eligibility snapshot before asking DP.  DP may
    # express preferences over these choices, but it never supplies them.
    pre_stateworks = load_stateworks(cow_root)
    pre_desired, pre_ambiguous = match_statework(pre_stateworks, domains, phase=phase)
    if pre_ambiguous:
        raise ValueError(f"ambiguous StateWork match for domain(s) {domains}; candidates: {sorted(pre_ambiguous)}")
    eligible_routes = [{"route_type": "stage", "route": route} for route in profile]
    if pre_desired is not None:
        eligible_routes.append({"route_type": "statework", "route": pre_desired.get("id"), "statework_id": pre_desired.get("id")})
        try:
            selected, _capsules = select_statework_runtime(
                pre_desired, cow_root, operation=operation, trigger=trigger,
                task_shape=shape, current_state=(observation_context or {}).get("state"),
                domain_tags=domains)
            flow = selected.get("selected_flow")
            if flow:
                eligible_routes.append({"route_type": "flow", "route": str(Path(flow[0]).parent.name), "statework_id": pre_desired.get("id")})
            for specialist in selected.get("selected_specialists", []):
                path = specialist[0] if isinstance(specialist, (list, tuple)) else str(specialist)
                eligible_routes.append({"route_type": "specialist", "route": str(path), "statework_id": pre_desired.get("id")})
        except PacketDependencyError:
            # A StateWork-backed task will fail closed later with its precise
            # selector error; do not fabricate an eligible flow for DP.
            pass
    for guard in routing_pack_for_context.get("always_on", []) + routing_pack_for_context.get("guards", []):
        eligible_routes.append({"route_type": "guard", "route": str(guard.get("key") or guard.get("id") or "")})
    for action_id in effective_actions:
        eligible_routes.append({"route_type": "tool_policy", "route": str(action_id), "action_id": str(action_id)})
    static_policy_hash = hashlib.sha256(_stable_json({
        "profile": profile, "aliases": aliases, "stages": stages,
    }).encode("utf-8")).hexdigest()
    eligible_choice_hash = hashlib.sha256(_stable_json(eligible_routes).encode("utf-8")).hexdigest()
    if behavioral_advisor is not None:
        advice = behavioral_advisor.advice_for_task(
            context=routing_context,
            eligible_routes=eligible_routes,
            static_policy={
                "profile": shape,
                "framework_version": routing_context["framework_version"],
                "static_policy_hash": static_policy_hash,
                "eligible_choice_hash": eligible_choice_hash,
                "context_identity": comparison_context_hash(routing_context),
            })
        # An advisor response is a candidate mutation, not trusted policy.
        # Validate the complete combined set after every source has spoken.
        raw_adjustments = list(advice.get("adjustments", [])) if isinstance(advice, Mapping) else []
        bounded = bounded_adjustments(advice, eligible_routes)
        if len(bounded) != len(raw_adjustments):
            raise ValueError("behavioral advisor returned ineligible or malformed adjustments")
        proposed = routing_adjustments + bounded
        # If advice was returned, it must be entirely legal; malformed or
        # authority-crossing items are quarantined rather than partially
        # applied.
        if advice.get("status") not in {"ok", "unavailable"} and advice.get("adjustments"):
            raise ValueError("behavioral advisor returned an invalid response")
        for adjustment in proposed:
            validate_adaptive_adjustment(
                adjustment, static_stages=stages,
                always_on_guards=[g.get("key") or g.get("id") for g in load_guard_pack().get("always_on", [])])
        routing_adjustments = proposed
    else:
        for adjustment in routing_adjustments:
            validate_adaptive_adjustment(
                adjustment, static_stages=stages,
                always_on_guards=[g.get("key") or g.get("id") for g in load_guard_pack().get("always_on", [])])
    routing_context["statework_versions"] = {
        str(item.get("id")): str(item.get("version")) for item in load_stateworks(cow_root)
    }
    routing_context["comparison_context_hash"] = comparison_context_hash(routing_context)
    if behavioral_advisor is not None:
        advice_receipt = {
            "contract_version": "1.0.0",
            "context": {**routing_context, "context_identity": comparison_context_hash(routing_context),
                        "static_policy_hash": static_policy_hash,
                        "eligible_choice_hash": eligible_choice_hash,
                        "advice_version": str(advice.get("advice_id") or advice.get("semantic_hash") or "unversioned"),
                        "expires_at": advice.get("expires_at"),
                        "source_profile_hash": advice.get("profile_pack_hash")},
            "eligible_choices": list(eligible_routes),
            "adjustments": [dict(item) for item in bounded],
            "final_decision": {"policy_hash": "", "selected_routes": [], "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        }
    suppressed_stage_routes = {
        str(item.get("route")) for item in routing_adjustments
        if item.get("route_type") in {"stage", "lifecycle_stage"}
        and item.get("disposition") == "suppress"
    }
    if suppressed_stage_routes:
        profile = [stage for stage in profile if not any(
            stage == route or stage.split("_", 1)[0] == route
            for route in suppressed_stage_routes)]
    profile = requires_closure(profile, stages)
    kernel_stages = order_kernel(profile, stages)

    # StateWork is a capability, not a hard source-tree dependency.  Missing
    # capability is fatal only when the task actually requires domain state.
    stateworks = pre_stateworks
    statework_available = bool(stateworks)
    if not statework_available and (domains or phase is not None or subject_ref is not None
                                    or requested_flows):
        raise ValueError("StateWork capability is required by this task but no registry is installed")
    packet_registry = parse_packet_registry(cow_root)
    desired, ambiguous = pre_desired, pre_ambiguous
    if ambiguous:
        raise ValueError(f"ambiguous StateWork match for domain(s) {domains}; candidates: {sorted(ambiguous)}")
    if desired is not None and any(
            item.get("route_type") == "statework"
            and item.get("route") == desired.get("id")
            and item.get("disposition") == "suppress"
            for item in routing_adjustments):
        raise ValueError("adaptive routing cannot suppress a statically required StateWork")
    effective_requested_flows = dict(requested_flows or {})
    suppressed_flows = {(str(item.get("statework_id") or ""), str(item.get("route") or "")) for item in routing_adjustments if item.get("route_type") == "flow" and item.get("disposition") == "suppress"}
    for (statework_id, flow) in list(effective_requested_flows.items()):
        if (statework_id, flow) in suppressed_flows:
            effective_requested_flows.pop(statework_id, None)
    for item in routing_adjustments:
        if item.get("route_type") == "flow" and item.get("disposition") in {"prefer", "activate"}:
            statework_id = item.get("statework_id")
            if statework_id and (str(statework_id), str(item.get("route") or "")) not in suppressed_flows:
                effective_requested_flows[str(statework_id)] = str(item.get("route"))
    chain = plan_statework_chain(desired, stateworks, packet_registry,
                                 packet_store=packet_store,
                                 allow_missing_required=allow_missing_required,
                                 missing_required=missing_required,
                                 task_id=task_id, subject_ref=subject_ref,
                                 observation_context=observation_context,
                                 cow_root=cow_root)
    if chain and not subject_ref:
        raise ValueError(
            "StateWork-backed tasks require an authoritative subject_ref; "
            "task_id is not a subject identity")

    # Select flows and pin their transition requirements before constructing
    # the kernel.  Merging raw manifest requirements first can omit a flow's
    # FUSE/WARD requirements even though that flow is selected later.
    enriched_stateworks, statework_capsules = _statework_runtime_pins(
        chain, cow_root, effective_requested_flows, operation=operation, trigger=trigger,
        task_shape=shape, domain_tags=domains,
        routing_adjustments=routing_adjustments)

    # kernel = union(profile kernel, every active StateWork core_requirements)
    # core_requirements may use owner names (anchor/dox); expand via aliases
    # so the final kernel contains only concrete stage ids.
    merged = list(kernel_stages)
    for sw in enriched_stateworks:
        for req in sw.get("core_requirements", []):
            for concrete in expand([req], aliases):
                if concrete not in merged:
                    merged.append(concrete)
        for req in sw.get("framework_enhancements", []):
            for concrete in expand([req], aliases):
                if concrete not in merged:
                    merged.append(concrete)
        for req in sw.get("flow_requirements", []):
            for concrete in expand([req], aliases):
                if concrete not in merged:
                    merged.append(concrete)
        for req in sw.get("transition_requirements", []):
            for concrete in expand([req], aliases):
                if concrete not in merged:
                    merged.append(concrete)
    merged = requires_closure(merged, stages)
    kernel_ordered = order_kernel(merged, stages)

    # Guard pack (schema-validated artifact)
    pack = routing_pack_for_context
    statework_registry = ({"version": "1.0.0", "stateworks": []}
                          if not statework_available else
                          _load_document(cow_root / "registry.yaml", load_yaml))
    behavior_contract_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
    behavior_contract = _load_document(behavior_contract_path, _load_json)
    behavior_contract_version = str(
        behavior_contract.get("properties", {}).get("schema_version", {}).get("const", ""))
    if statework_available:
        validate_compatibility(
            statework_registry_version=statework_registry.get("version"),
            behavior_event_version=behavior_contract_version,
            guard_pack_version=pack.get("pack_version"))
    statework_ids = [sw.get("id") for sw in enriched_stateworks]
    suppressed_guard_routes = {
        str(item.get("route")) for item in routing_adjustments
        if item.get("route_type") == "guard" and item.get("disposition") == "suppress"
    }
    deployable_pack_guards = [g for g in pack.get("guards", [])
                             if (g.get("key") or g.get("id")) not in suppressed_guard_routes
                             and g.get("id") not in suppressed_guard_routes
                             and g.get("family") not in suppressed_guard_routes]
    scoped_pack = dict(pack)
    scoped_pack["guards"] = deployable_pack_guards
    guard_guards = applicable_guards(scoped_pack, domains, [shape],
                                     model=model, harness=harness, trigger=trigger,
                                     task_id=task_id, stateworks=statework_ids)

    # Policy hashes over actual inputs, not plugin.json alone.
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    framework_version = plugin.get("version", "unknown")
    pipeline_hash = sha256(ROOT / "shared" / "pipeline.yaml")
    kernel_hash = sha256(ROOT / "shared" / "runtime-kernel.yaml")
    capsule_hashes: Dict[str, str] = {}
    for name in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD", "SISPIS"]:
        capsule = ROOT / name / "runtime.md"
        if capsule.exists():
            capsule_hashes[name] = sha256(capsule)
    cal_path = ROOT / "SISPIS" / "references" / "signal-calibration.yaml"
    sispis_calibration_hash = sha256(cal_path) if cal_path.exists() else ""
    signal_schema_path = ROOT / "shared" / "signal.schema.json"
    signal_registry_path = ROOT / "shared" / "signal-registry.json"
    behavior_event_schema_path = LOCAL_CONTRACT_ROOT / "behavior-event.schema.json"
    policy_documents: Dict[str, Any] = {
        "sispis_calibration": _load_document(cal_path, load_yaml),
        "signal_schema": _load_document(signal_schema_path, _load_json),
        "signal_registry": _load_document(signal_registry_path, _load_json),
        "behavior_event_schema": _load_document(behavior_event_schema_path, _load_json),
        "validator_registry_hash": validator_registry_hash(),
        "handler_registry_hash": handler_registry_hash(),
        "action_contract_hash": action_contract_hash(effective_actions),
        "cow_runtime_hash": cow_runtime_hash(cow_root),
        "runtime_behavior_hash": runtime_behavior_hash(cow_root),
        "routing_profile": routing_profile,
        "routing_experiment": routing_experiment,
    }
    behavior_schema = policy_documents["behavior_event_schema"]
    behavior_schema_version = str(
        behavior_schema.get("properties", {}).get("schema_version", {}).get("const", ""))
    if not behavior_schema_version:
        raise SnapshotIntegrityError("behavior-event schema has no pinned schema_version")
    policy_documents["behavior_event_schema_version"] = behavior_schema_version
    policy_documents["runtime_build_manifest"] = runtime_build_manifest(
        cow_root, effective_actions)
    policy_documents["runtime_build_manifest_hash"] = (
        policy_documents["runtime_build_manifest"]["manifest_hash"])
    policy_documents["statework_transitions"] = {
        item["id"]: item.get("transitions_contract", {}) for item in enriched_stateworks
    }
    statework_pins = tuple(
        StateWorkPin(id=item["id"], version=item["version"],
                     manifest_hash=item["manifest_hash"], runtime_hash=item["runtime_hash"],
                     selected_flow=(tuple(item["selected_flow"])
                                    if item["selected_flow"] else None),
                     selected_specialists=tuple(
                         tuple(pair) for pair in item["selected_specialists"]),
                     transitions_hash=item.get("transitions_hash", ""),
                     transitions_contract=item.get("transitions_contract", {}))
        for item in enriched_stateworks
    )

    unique_guards = compile_task_guards(
        pack["always_on"] + guard_guards, domains, [shape],
        model=model, harness=harness, trigger=trigger, task_id=task_id,
        token_budget=max(200, sum(max(int(g.get("estimated_tokens", 10) or 10), 1)
                                  for g in pack["always_on"])),
        stateworks=statework_ids,
        mandatory_ids=[g.get("id") for g in pack["always_on"]],
        routing_adjustments=routing_adjustments)

    def guard_scope_matches(guard: Dict[str, Any]) -> bool:
        scope = nested_guard_scope(guard)
        return not (
            (scope["domains"] and not (set(domains) & set(scope["domains"])))
            or (scope["stateworks"] and not (set(statework_ids) & set(scope["stateworks"])))
            or (scope["task_shapes"] and shape not in set(scope["task_shapes"]))
            or (scope["models"] and (model is None or model not in set(scope["models"])))
            or (scope["harnesses"] and (harness is None or harness not in set(scope["harnesses"])))
            or (guard.get("trigger") is not None and guard.get("trigger") != trigger)
        )

    eligible_guard_keys = []
    guard_assignments: Dict[str, str] = {}
    for guard in pack["always_on"] + pack.get("guards", []):
        key = guard.get("key") or guard["id"]
        if key in suppressed_guard_routes or guard.get("id") in suppressed_guard_routes:
            guard_assignments[key] = "ineligible"
            continue
        if guard.get("status") not in {"active", "canary"} or not guard_scope_matches(guard):
            guard_assignments[key] = "ineligible"
            continue
        eligible_guard_keys.append(key)
        if guard.get("status") == "canary" and not canary_assigned(
                key, guard.get("rollout"), task_id):
            guard_assignments[key] = "control"
        else:
            guard_assignments[key] = "treatment"
    assigned_guard_keys = [g.get("key") or g["id"] for g in unique_guards]

    preferred_stages = {str(item.get("route")) for item in routing_adjustments if item.get("route_type") == "stage" and item.get("disposition") == "prefer"}
    kernel_instructions = [
        {
            "stage": sid,
            "routing_preference": "preferred" if sid in preferred_stages else "default",
            "activation": stages[sid].get("activation_rule"),
            "outputs": stages[sid].get("output", []),
            "execution_mode": stages[sid].get("execution_mode"),
            "frequency": stages[sid].get("frequency"),
            "activation_predicates": stages[sid].get("activation_predicates", []),
        }
        for sid in kernel_ordered
    ]
    handoff_plan = []
    for sw in chain:
        packet = sw.get("handoff") or (sw.get("emits") or [""])[0]
        handoff_plan.append({"producer": sw.get("id"), "packet": packet})
    core_capsules = injected_capsules_for(kernel_ordered)
    capsules = {stage: (text, sha256_bytes(text.encode("utf-8")))
                for stage, text in core_capsules.items()}
    capsules.update(statework_capsules)
    capsule_pins = tuple(
        CapsulePin(owner=stage.rsplit(":", 1)[-1].split("_")[0].upper(),
                   stage=stage, version="2.0.0", content_hash=content_hash)
        for stage, (_, content_hash) in sorted(capsules.items())
    )
    guard_pins = tuple(
        GuardPin(guard_key=guard.get("key") or guard["id"],
                 rule_hash=semantic_guard_hash(guard),
                 status_at_resolution=guard.get("status", "active"))
        for guard in unique_guards
    )
    effective_policy = EffectivePolicy(
        profile=shape, kernel=tuple(kernel_ordered), capsules=capsule_pins,
        stateworks=statework_pins, guards=guard_pins, model=model, model_id=model_id,
        provider_id=provider_id, model_revision=model_revision,
        model_capability_hash=model_capability_hash, harness=harness,
        harness_version=harness_version, toolset=toolset,
        namespace_id=namespace_id, application_id=application_id,
        application_version=application_version,
        application_instance_id=application_instance_id,
        trigger=trigger, pipeline_hash=pipeline_hash,
        runtime_kernel_hash=kernel_hash,
        calibration_hash=sispis_calibration_hash,
        subject_ref=subject_ref,
        signal_schema_hash=sha256(signal_schema_path),
        signal_registry_hash=sha256(signal_registry_path),
        behavior_event_schema_hash=sha256(behavior_event_schema_path),
        behavior_event_schema_version=behavior_schema_version,
        runtime_behavior_hash=policy_documents["runtime_behavior_hash"],
        validator_registry_hash=policy_documents["validator_registry_hash"],
        handler_registry_hash=policy_documents["handler_registry_hash"],
        action_contract_hash=policy_documents["action_contract_hash"],
        cow_runtime_hash=policy_documents["cow_runtime_hash"],
        runtime_build_manifest_hash=policy_documents["runtime_build_manifest"]["manifest_hash"],
        routing_profile_hash=hashlib.sha256(
            f"{routing_pack_hash}:{routing_experiment_plan.get('plan_hash', '')}:{routing_cohort or ''}".encode()
        ).hexdigest() if routing_experiment_plan else routing_pack_hash,
    )
    frozen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    instruction_words = sum(len(str(k["activation"] or "").split()) + sum(len(str(o).split()) for o in k["outputs"]) for k in kernel_instructions)
    instruction_words += sum(
        len(str(g.get("rule", "")).split())
        for g in unique_guards
        if enforcement_for_guard(g).get("mode") == "prompt")
    instruction_words += sum(len(text.split()) for text, _hash in capsules.values())

    policy_hash = compute_policy_hash(effective_policy)
    execution_plan = tuple({
        "segment_id": f"segment-{index}",
        "index": index,
        "statework_id": item.get("id"),
        "subject_ref": subject_ref,
        "input_packet": ((item.get("inputs") or {}).get("required") or [None])[0]
            if isinstance(item.get("inputs"), dict) else None,
        "output_packet": (item.get("handoff") or (item.get("emits") or [None])[0]),
        "selected_flow": item.get("selected_flow"),
        "exclusive_group": item.get("exclusive_group"),
    } for index, item in enumerate(enriched_stateworks))
    if not execution_plan:
        execution_plan = ({"segment_id": "segment-0", "index": 0,
                           "statework_id": None, "subject_ref": subject_ref,
                           "input_packet": None, "output_packet": None},)
    if advice_receipt is not None:
        selected_routes = [{"route_type": "stage", "route": str(stage), "selection_reason": "effective_kernel"} for stage in kernel_ordered]
        selected_routes.extend({"route_type": "statework", "route": str(item.get("id")), "selection_reason": "effective_statework"} for item in enriched_stateworks)
        for item in enriched_stateworks:
            if item.get("selected_flow"):
                selected_routes.append({"route_type": "flow", "route": str(Path(item["selected_flow"][0]).parent.name), "selection_reason": "effective_flow"})
        advice_receipt["final_decision"] = {"policy_hash": compute_policy_hash(effective_policy), "selected_routes": selected_routes, "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    snapshot = PolicySnapshot(
        task_id=task_id,
        kernel=tuple(kernel_ordered),
        stateworks=statework_pins,
        guard_pack_version=pack["pack_version"],
        guard_pack_hash=pack["semantic_hash"],
        framework_version=framework_version,
        framework_hash=sha256(ROOT / "plugin.json"),
        model=model,
        model_id=model_id,
        provider_id=provider_id,
        model_revision=model_revision,
        model_capability_hash=model_capability_hash,
        harness=harness,
        harness_version=harness_version,
        toolset=toolset,
        namespace_id=namespace_id,
        application_id=application_id,
        application_version=application_version,
        application_instance_id=application_instance_id,
        frozen_at=frozen_at,
        policy_hash=policy_hash,
        subject_ref=subject_ref,
        agent_id=agent_id,
        phase=phase,
        domain_tags=tuple(domains),
        task_shape=shape,
        operation=operation,
        trigger=trigger,
        execution_plan=execution_plan,
        agent_instance_id=agent_instance_id,
        attempt_id=attempt_id,
        interaction_id=interaction_id,
        parent_session_id=parent_session_id,
        delegator_agent_id=delegator_agent_id,
        delegate_agent_id=delegate_agent_id,
        delegation_id=delegation_id,
        role=role,
        contracts=policy_documents,
    )

    bundle = RuntimeBundle(
        bundle_version="2.0.0",
        task_id=task_id,
        profile=shape,
        kernel=kernel_ordered,
        kernel_instructions=kernel_instructions,
        stateworks=enriched_stateworks,
        handoff_plan=handoff_plan,
        guard_pack={
            "version": pack["pack_version"],
            "hash": pack["semantic_hash"],
            "status": pack.get("guard_pack_status", "valid"),
            "eligible_guard_keys": eligible_guard_keys,
            "assigned_guard_keys": assigned_guard_keys,
            "guard_assignments": guard_assignments,
            "suppressed_guard_keys": sorted(suppressed_guard_routes),
            "routing_profile": routing_profile,
            "routing_adjustments": routing_adjustments,
            "advice_exchange": advice_receipt,
            "routing_experiment": routing_experiment,
            "structural_handlers": {
                (g.get("key") or g["id"]): enforcement_for_guard(g)
                for g in unique_guards
                if enforcement_for_guard(g).get("mode") != "prompt"
            },
            "guards": [
                {"id": g["id"], "key": g.get("key") or g["id"],
                 "family": g.get("family") or g["id"], "version": g.get("version", "1"),
                 "status": g.get("status", "active"),
                 "target_behavior": g.get("target_behavior"),
                 "rule": g.get("rule", g.get("target_behavior", "")),
                 "scope": g.get("scope"),
                 "trigger": g.get("trigger"),
                 "evaluator": g.get("evaluator"),
                 "enforcement_key": g.get("enforcement_key"),
                 "enforcement": enforcement_for_guard(g)}
                for g in unique_guards
            ],
        },
        pinned=snapshot.to_dict(),
        policy=effective_policy,
        capsule_hashes=capsule_hashes,
        injected_capsules=tuple(capsules.items()),
        instruction_words=instruction_words,
        execution_plan=execution_plan,
        statework_root=str(cow_root.resolve()),
        action_registry=effective_actions,
    )
    return bundle


def write_snapshot(bundle: RuntimeBundle, task_id: Optional[str] = None) -> Path:
    """Write separate semantic-policy and task-instance artifacts.

    ``policy_hash`` identifies stable policy meaning; ``frozen_at`` and the
    task/session envelope belong to the instance artifact and are therefore
    not allowed to make a semantic policy conflict.
    """
    task = task_id or bundle.task_id
    policy_hash = bundle.pinned.get("policy_hash", "unknown")
    policy_dir = runtime_state_dir() / "policies"
    policy_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(policy_dir, 0o700)
    policy_path = policy_dir / f"{policy_hash}.json"
    policy_artifact = {
        "artifact_version": "policy-1",
        "policy_hash": policy_hash,
        "policy": bundle.policy.canonical(),
        "contracts": _thaw(bundle.pinned.get("contracts", {})),
    }
    if policy_path.exists():
        try:
            existing_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SnapshotIntegrityError(f"existing policy artifact is unreadable: {exc}") from exc
        if existing_policy != policy_artifact:
            raise SnapshotIntegrityError(
                f"existing semantic policy differs from requested policy: {policy_path}")
    else:
        fd, temporary = tempfile.mkstemp(dir=str(policy_dir), prefix=".policy-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(policy_artifact, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, policy_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    task_dir = runtime_state_dir() / _safe_task_id(task)
    task_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(task_dir, 0o700)
    _policy_from_bundle(bundle)
    out_path = task_dir / f"{policy_hash}.json"
    task_projection = bundle.to_dict()
    task_projection.setdefault("pinned", {}).pop("frozen_at", None)
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SnapshotIntegrityError(f"existing snapshot is unreadable: {exc}") from exc
        existing_hash = existing.get("pinned", {}).get("policy_hash")
        if existing_hash != policy_hash:
            raise SnapshotIntegrityError(
                f"existing snapshot policy mismatch: path={out_path} "
                f"existing={existing_hash!r} requested={policy_hash!r}")
        existing_projection = dict(existing)
        existing_projection.setdefault("pinned", {}).pop("frozen_at", None)
        if existing_projection != task_projection:
            raise SnapshotIntegrityError(
                f"existing snapshot content differs from requested policy: {out_path}")
        return out_path
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(task_dir), prefix=".snapshot-", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(bundle.to_dict(), indent=2) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, out_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return out_path


def write_task_manifest(bundle: RuntimeBundle, *, session_id: str = "prestart",
                        attempt_id: str = "attempt-1", interaction_id: Optional[str] = None,
                        parent_session_id: Optional[str] = None,
                        delegator_agent_id: Optional[str] = None,
                        delegate_agent_id: Optional[str] = None,
                        delegation_id: Optional[str] = None,
                        role: Optional[str] = None) -> Path:
    """Write the immutable policy/telemetry join manifest beside telemetry."""
    root = runtime_state_dir() / "telemetry"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    digest = hashlib.sha256(
        f"{bundle.task_id}:{session_id}".encode("utf-8")).hexdigest()[:24]
    path = root / f"{digest}.manifest.json"
    payload = build_task_manifest(
        bundle, session_id=session_id, attempt_id=attempt_id,
        interaction_id=interaction_id, parent_session_id=parent_session_id,
        delegator_agent_id=delegator_agent_id, delegate_agent_id=delegate_agent_id,
        delegation_id=delegation_id, role=role)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise SnapshotIntegrityError(f"task manifest already exists with different policy: {path}")
        return path
    fd, temporary = tempfile.mkstemp(dir=str(root), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def _safe_task_id(task_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._")
    return cleaned or "task"


def read_manifest_hash(sw: Dict[str, Any]) -> str:
    path = sw.get("_manifest_path")
    return sha256(path) if path else ""


# ------------------------------------------------------------------- main

def main(argv: List[str]) -> int:
    task_id = argv[1] if len(argv) > 1 else "task-default"
    shape = argv[2] if len(argv) > 2 else "implement"
    raw_domains = argv[3] if len(argv) > 3 else ""
    subject_ref = argv[4] if len(argv) > 4 else None
    domains = [d.strip() for d in raw_domains.split(",") if d.strip()]

    try:
        bundle = resolve(task_id, shape, domains, subject_ref=subject_ref)
    except (PacketDependencyCycle, PacketDependencyError, ValueError) as exc:
        print(f"resolution error: {exc}")
        return 1
    except RecursionError:
        print("resolution error: recursion exceeded (likely cyclic packet dependency)")
        return 1

    out_path = write_snapshot(bundle)
    kernel_ordered = bundle.kernel
    chain = bundle.stateworks
    unique_guards = bundle.guard_pack.get("guards", [])
    snapshot = bundle.pinned
    print(f"guards:     {', '.join(g['id'] for g in unique_guards) or '(none)'} "
          f"(pack {bundle.guard_pack.get('version')}, "
          f"status={bundle.guard_pack.get('status')})")
    print(f"task:       {task_id}")
    print(f"profile:    {shape}")
    print(f"stateworks: {', '.join(s['id'] for s in chain) or '(none)'}")
    print(f"kernel:     {', '.join(kernel_ordered)}")
    print(f"bundle:     {out_path} ({bundle.instruction_words} instruction words)")
    sw_pins = ",".join(f"{s['id']}@{s['version']}" for s in snapshot.get("stateworks", [])) or "-"
    print(f"pinned:     framework={snapshot.get('framework_version')} "
          f"stateworks={sw_pins} guards={snapshot.get('guard_pack_version')}")
    print(f"policy:     {snapshot.get('policy_hash', 'n/a')}")
    print(f"frozen_at:  {snapshot.get('frozen_at')}")
    print(f"telemetry:  out-of-band (DP event plane; snapshot immutable)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

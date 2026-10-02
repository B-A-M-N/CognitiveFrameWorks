"""StateWork flow selection and immutable runtime pin construction."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from resolver.common import _load_json, load_yaml, sha256
from resolver.errors import PacketDependencyError

def _statework_root(cow_root: Path, statework: Any) -> Path:
    try:
        return statework_provider(cow_root).bundle_root(statework)
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

def _validate_statework_flow_contracts(cow_root: Path, statework_id: str, *,
                                       owner_root: Optional[Path] = None) -> None:
    try:
        statework_provider(cow_root).validate_flow_contracts(
            statework_id, owner_root=owner_root)
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

def _statework_flow_metadata(sw: Mapping[str, Any], cow_root: Path
                             ) -> Dict[str, Dict[str, Any]]:
    try:
        return statework_provider(cow_root).flow_metadata(sw)
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

def _flow_selector(cow_root: Path) -> Any:
    try:
        return statework_provider(cow_root).flow_selector()
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

def enumerate_eligible_routes(sw: Dict[str, Any], cow_root: Path, *,
                                operation: Optional[str] = None,
                                trigger: Optional[str] = None,
                                task_shape: Optional[str] = None,
                                phase: Optional[str] = None,
                                current_state: Optional[str] = None,
                                domain_tags: Optional[Iterable[str]] = None,
                                suppressed_flows: Optional[Iterable[str]] = None,
                                suppressed_specialists: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Enumerate host-computed legal routes without choosing one."""
    sid = str(sw.get("id"))
    suppressed_flows = set(suppressed_flows or ())
    suppressed_specialists = set(suppressed_specialists or ())
    result: List[Dict[str, Any]] = []
    owner_root = _statework_root(cow_root, sw)
    flows_dir = owner_root / "flows"
    if flows_dir.is_dir():
        flow_metadata = _statework_flow_metadata(sw, cow_root)
        selection = _flow_selector(cow_root).select(
            flow_metadata,
            {"operation": operation, "trigger": trigger, "task_shape": task_shape,
             "phase": phase, "current_state": current_state,
             "domain_tags": list(domain_tags or ())},
            dict(sw.get("flow_selection") or {}),
            excluded=suppressed_flows)
        for name in selection.candidates:
            meta = flow_metadata[name]
            path = flows_dir / name / "flow.md"
            name = path.parent.name
            budget = {"low": 1, "medium": 2, "high": 3}.get(sw.get("context_budget", "medium"), 2)
            result.append({"route_type": "flow", "route": name, "statework_id": sid,
                           "eligibility_conditions": {"when": meta.get("when"),
                                                       "fallback": meta.get("fallback") is True},
                           "context_cost": {"specialist_budget": budget},
                           "dependencies": list(meta.get("required_specialists") or ())})
            root = path.parent
            for specialist in list(meta.get("required_specialists") or ()) + list(meta.get("optional_specialists") or ()):
                if specialist in suppressed_specialists:
                    continue
                candidate = (root / specialist).resolve()
                if candidate.is_file():
                    try: candidate.relative_to(owner_root.resolve())
                    except ValueError: continue
                    result.append({"route_type": "specialist", "route": f"{sid}/{candidate.relative_to(owner_root).as_posix()}",
                                   "statework_id": sid, "eligibility_conditions": {"flow": name},
                                   "context_cost": {"required": specialist in (meta.get("required_specialists") or ()),
                                                   "specialist_budget": budget},
                                   "dependencies": [name]})
    return result

def select_statework_runtime(
        sw: Dict[str, Any], cow_root: Path,
        requested_flow: Optional[str] = None, *, operation: Optional[str] = None,
        trigger: Optional[str] = None, task_shape: Optional[str] = None,
        phase: Optional[str] = None,
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
        "domain": str(sw.get("domain") or ""),
        "purpose": str(sw.get("purpose") or ""),
        "entrypoint": sw.get("entrypoint"),
        "manifest_hash": read_manifest_hash(sw),
    }
    owner_root = _statework_root(cow_root, sw)
    compiled_engine = dict(sw.get("_compiled_engine") or {})
    engine_decl = dict(sw.get("engine") or {})
    engine_kind = str(compiled_engine.get("kind") or engine_decl.get("kind") or "state_machine")
    workflow_contract_hash = str(compiled_engine.get("contract_hash") or "")
    workflow_contract_version = str(
        compiled_engine.get("contract_version") or engine_decl.get("contract_version") or "")
    if not workflow_contract_hash:
        raise PacketDependencyError(
            f"StateWork {sid!r} has no content-pinned workflow contract")
    item["workflow_engine_kind"] = engine_kind
    item["workflow_contract_version"] = workflow_contract_version
    item["workflow_contract_hash"] = workflow_contract_hash
    item["flow_selection"] = dict(sw.get("flow_selection") or {})
    runtime_text = str(sw.get("_runtime_capsule") or "")
    runtime_hash = hashlib.sha256(runtime_text.encode("utf-8")).hexdigest()
    if not runtime_text or runtime_hash != str(sw.get("_runtime_capsule_hash") or ""):
        raise PacketDependencyError(
            f"StateWork {sid!r} has no valid compiled runtime capsule")
    item["runtime_hash"] = runtime_hash

    capsules: Dict[str, Tuple[str, str]] = {
        f"statework:{sid}": (runtime_text, runtime_hash)
    }
    flows_dir = owner_root / "flows"
    flow_paths = sorted(flows_dir.glob("*/flow.md")) if flows_dir.is_dir() else []
    selected_flow_path: Optional[Path] = None
    selected_flow_name = None
    specialists: list[Tuple[Path, str]] = []
    suppressed_specialists = set(suppressed_specialists or ())
    preferred_specialists = set(preferred_specialists or ())
    suppressed_flows = set(suppressed_flows or ())
    if flow_paths:
        flow_metadata = _statework_flow_metadata(sw, cow_root)
        selection = _flow_selector(cow_root).select(
            flow_metadata,
            {"operation": operation, "trigger": trigger, "task_shape": task_shape,
             "phase": phase, "current_state": current_state,
             "domain_tags": list(domain_tags or ())},
            item["flow_selection"], requested=requested_flow,
            excluded=suppressed_flows)
        if selection.selected is None:
            if selection.reason == "error":
                top_score = max(selection.scores.values(), default=0)
                tied = sorted(name for name, score in selection.scores.items()
                              if score == top_score)
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow selection is ambiguous: {tied}")
            if selection.reason == "requested_ineligible":
                raise PacketDependencyError(
                    f"StateWork {sid!r} requested flow {requested_flow!r} is not eligible")
            if selection.reason == "blocked":
                raise PacketDependencyError(
                    f"StateWork {sid!r} has no matching flow and policy is blocked")
            if selection.reason == "inactive":
                item["flow_selection_status"] = "inactive"
        else:
            selected_flow_path = flows_dir / selection.selected / "flow.md"
        if selected_flow_path is not None:
            try:
                selected_flow_path.resolve().relative_to(owner_root.resolve())
            except ValueError as exc:
                raise PacketDependencyError(
                    f"StateWork {sid!r} flow escapes its owner boundary") from exc
            selected_flow_name = selected_flow_path.parent.name
            text = selected_flow_path.read_text(encoding="utf-8")
            capsules[f"statework-flow:{sid}:{selected_flow_name}"] = (
                text, sha256(selected_flow_path))
            frontmatter = flow_metadata[selected_flow_name]
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
            owner_root = owner_root.resolve()
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
            item["flow_integrations"] = dict(frontmatter.get("integrations") or {})
            item["flow_metadata"] = {
                key: frontmatter.get(key)
                for key in ("flow", "when", "integrations",
                            "required_specialists", "optional_specialists",
                            "completion", "fallback")
                if key in frontmatter
            }
    for index, (path, content_hash) in enumerate(specialists):
        capsules[f"statework-specialist:{sid}:{index}"] = (
            path.read_text(encoding="utf-8"), content_hash)
    item["selected_flow"] = (
        [f"{sid}/{selected_flow_path.relative_to(owner_root).as_posix()}", sha256(selected_flow_path)]
        if selected_flow_path else None
    )
    item["selected_specialists"] = [
        [f"{sid}/{path.relative_to(owner_root).as_posix()}", content_hash]
        for path, content_hash in specialists
    ]
    return item, capsules

def _statework_runtime_pins(
        chain: List[Dict[str, Any]], cow_root: Path,
        requested_flows: Optional[Dict[str, str]] = None, *, operation: Optional[str] = None,
        trigger: Optional[str] = None, task_shape: Optional[str] = None,
        phase: Optional[str] = None,
        current_state: Optional[str] = None,
        domain_tags: Optional[Iterable[str]] = None,
        statework_subject_refs: Optional[Mapping[str, str]] = None,
        default_subject_ref: Optional[str] = None,
        routing_adjustments: Optional[Iterable[Mapping[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    """Pin every selected StateWork runtime byte for the task."""
    enriched: List[Dict[str, Any]] = []
    capsules: Dict[str, Tuple[str, str]] = {}
    requested_flows = requested_flows or {}
    statework_subject_refs = statework_subject_refs or {}
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
            trigger=trigger, task_shape=task_shape, phase=phase,
            current_state=current_state,
            domain_tags=domain_tags,
            suppressed_specialists=suppressed_by_statework.get(str(sw.get("id")), ()),
            preferred_specialists=preferred_by_statework.get(str(sw.get("id")), ()),
            suppressed_flows=suppressed_flows_by_statework.get(str(sw.get("id")), ()))
        if item.get("flow_selection_status") == "inactive":
            continue
        item["subject_ref"] = statework_subject_refs.get(
            str(sw.get("id")), default_subject_ref)
        enriched.append(item)
        capsules.update(selected)
    return enriched, capsules

def read_manifest_hash(sw: Dict[str, Any]) -> str:
    path = sw.get("_manifest_path")
    return sha256(path) if path else ""

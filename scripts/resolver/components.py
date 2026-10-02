"""Pipeline component loading and deterministic lifecycle ordering."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

from runtime_contract.pipeline import load_pipeline_document


ROOT = Path(__file__).resolve().parents[2]


def load_pipeline() -> Dict[str, Any]:
    return load_pipeline_document(ROOT / "shared" / "pipeline.yaml")


def expand(nodes: Iterable[str], aliases: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for node in nodes:
        if node in aliases:
            out.extend(expand(aliases[node], aliases))
        else:
            out.append(node)
    return out


def canonical_stage_order(stages: Dict[str, Any]) -> List[str]:
    """Topologically order manifest stages with stable lexical tie breaks."""
    active = list(stages.keys())
    indegree = {stage_id: 0 for stage_id in active}
    edges: Dict[str, List[str]] = {stage_id: [] for stage_id in active}
    for stage_id in active:
        for predecessor in stages.get(stage_id, {}).get("order_after", []):
            if predecessor in active and predecessor != stage_id:
                edges[predecessor].append(stage_id)
                indegree[stage_id] += 1
    ready = sorted(stage_id for stage_id in active if indegree[stage_id] == 0)
    ordered: List[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for successor in sorted(edges[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort()
    return ordered


def requires_closure(active: Iterable[str], stages: Dict[str, Any]) -> List[str]:
    """Return the active stages plus every transitive hard requirement."""
    ordered = list(dict.fromkeys(active))
    changed = True
    while changed:
        changed = False
        for stage_id in list(ordered):
            for required in stages.get(stage_id, {}).get("requires", []):
                if required not in ordered:
                    ordered.append(required)
                    changed = True
    return ordered


def order_kernel(active: Iterable[str], stages: Dict[str, Any]) -> List[str]:
    """Order active stages by lifecycle edges, then canonical manifest order."""
    canonical = canonical_stage_order(stages)
    canonical_index = {stage_id: index for index, stage_id in enumerate(canonical)}
    active_ids = list(dict.fromkeys(active))
    indegree = {stage_id: 0 for stage_id in active_ids}
    edges: Dict[str, List[str]] = {stage_id: [] for stage_id in active_ids}
    for stage_id in active_ids:
        for predecessor in stages.get(stage_id, {}).get("order_after", []):
            if predecessor in active_ids and predecessor != stage_id:
                edges[predecessor].append(stage_id)
                indegree[stage_id] += 1
    sort_key = lambda stage_id: canonical_index.get(stage_id, len(canonical))
    ready = sorted((stage_id for stage_id in active_ids if indegree[stage_id] == 0),
                   key=sort_key)
    ordered: List[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for successor in sorted(edges[current], key=sort_key):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort(key=sort_key)
    if len(ordered) != len(active_ids):
        raise ValueError(f"order_after cycle among active kernel: {active_ids}")
    return ordered

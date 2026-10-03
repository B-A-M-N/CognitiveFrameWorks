"""Deprecated compatibility calls delegated to CSW's workflow authority."""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider


def _provider_root_from_manifests(stateworks: Iterable[Mapping[str, Any]],
                                  fallback: Path = DEFAULT_STATEWORK_ROOT) -> Path:
    for item in stateworks:
        bundle = item.get("_bundle_root") if isinstance(item, Mapping) else None
        if not isinstance(bundle, str) or not bundle:
            continue
        path = Path(bundle).resolve()
        for candidate in (path, *path.parents):
            if (candidate / "registry.yaml").is_file():
                return candidate
    return Path(fallback)


def _provider_matches(domains: Iterable[str], phase: Optional[str], cow_root: Path
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    provider = statework_provider(cow_root)
    domain_list = [domains] if isinstance(domains, str) else [str(item) for item in domains]
    identity = {"task_id": "cfw-legacy-workflow-adapter",
                "subject_ref": "cfw:legacy-workflow-adapter"}
    context = {"domains": domain_list, "phase": phase,
               "allow_missing_required": True}
    try:
        eligibility = provider.eligible_routes(
            runtime_identity=identity, task_context=context)
    except Exception as exc:
        candidates = getattr(exc, "candidates", ())
        if candidates:
            return [], sorted(str(item) for item in candidates)
        raise
    routes = (eligibility.get("eligible_routes", ())
              if isinstance(eligibility, dict)
              else getattr(eligibility, "eligible_routes", ()))
    statework_ids = sorted({str(item["statework_id"]) for item in routes
                            if isinstance(item, dict)
                            and item.get("route_type") == "statework"
                            and item.get("statework_id")})
    if not statework_ids:
        return [], []
    manifests = {str(item.get("id")): item
                 for item in provider.load_stateworks()
                 if isinstance(item, dict) and item.get("id")}
    return [manifests[item] for item in statework_ids if item in manifests], []


def match_statework(stateworks: List[Dict[str, Any]], domains: Iterable[str],
                    phase: Optional[str] = None, *,
                    cow_root: Path = DEFAULT_STATEWORK_ROOT
                    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Compatibility matcher backed by CSW's canonical eligibility API."""
    warnings.warn(
        "match_statework is deprecated; call the CSW WorkflowPlanner directly",
        DeprecationWarning, stacklevel=2)
    matches, ambiguous = _provider_matches(
        domains, phase, _provider_root_from_manifests(stateworks, cow_root))
    if ambiguous or len(matches) > 1:
        return None, ambiguous or sorted(str(item.get("id")) for item in matches)
    return (matches[0] if matches else None), []


def match_stateworks(stateworks: List[Dict[str, Any]], domains: Iterable[str],
                     phase: Optional[str] = None, *,
                     cow_root: Path = DEFAULT_STATEWORK_ROOT
                     ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Compatibility multi-match backed by CSW's canonical eligibility API."""
    warnings.warn(
        "match_stateworks is deprecated; call the CSW WorkflowPlanner directly",
        DeprecationWarning, stacklevel=2)
    return _provider_matches(
        domains, phase, _provider_root_from_manifests(stateworks, cow_root))


__all__ = ["_provider_root_from_manifests", "match_statework", "match_stateworks"]

"""Deterministic StateWork matching and ambiguity reporting."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
        stateworks = [m for m in stateworks
                      if phase in set(m.get("phase_tags") or
                                      ([m.get("phase")] if m.get("phase") else []))]
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
        match_contract = m.get("match") or {}
        required_any = [str(value) for value in match_contract.get("required_any") or ()]
        secondary = [str(value) for value in match_contract.get("secondary") or ()]
        normalized_domains = [_norm(value) for value in domain_list]
        if required_any and not any(
                _norm(term) and any(
                    _norm(term) == domain or f" {_norm(term)} " in f" {domain} "
                    for domain in normalized_domains)
                for term in required_any):
            # Secondary vocabulary may rank an already-eligible StateWork,
            # but cannot turn generic layout/input work into terminal UI work.
            continue
        triggers = triggers + secondary
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

def match_stateworks(stateworks: List[Dict[str, Any]], domains: Iterable[str],
                     phase: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Match all independently eligible domain roots with explicit ambiguity."""
    roots: Dict[str, Dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for domain in domains:
        candidate, conflicts = match_statework(stateworks, [domain], phase=phase)
        ambiguous.update(str(item) for item in conflicts)
        if candidate is not None:
            roots[str(candidate.get("id"))] = candidate
    return [roots[key] for key in sorted(roots)], sorted(ambiguous)

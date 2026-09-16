"""Host-executed validator registry.

Validator callers provide typed tool results; they do not provide the pass
boolean that becomes authority.  The registered implementation derives it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping


@dataclass(frozen=True)
class ValidatorResult:
    validator_id: str
    version: str
    passed: bool
    evidence_type: str
    details: Mapping[str, Any]


Validator = Callable[[Mapping[str, Any]], ValidatorResult]


def _status(result: Mapping[str, Any]) -> str:
    return str(result.get("status") or result.get("result_class") or "").lower()


def _completion_boundary(result: Mapping[str, Any]) -> ValidatorResult:
    status = _status(result)
    passed = status in {"pass", "passed", "success", "completed", "boundary"}
    return ValidatorResult("completion-boundary-v1", "1", passed,
                          "completion_boundary", dict(result))


def _reinspection(result: Mapping[str, Any]) -> ValidatorResult:
    passed = bool(result.get("fresh") and result.get("relevant", True))
    return ValidatorResult("reinspection-v1", "1", passed,
                          "validation_result", dict(result))


def _state_transition(result: Mapping[str, Any]) -> ValidatorResult:
    passed = bool(result.get("validated") and result.get("state"))
    return ValidatorResult("state-transition-v1", "1", passed,
                          "validation_result", dict(result))


VALIDATOR_REGISTRY: Dict[str, Validator] = {
    "completion-boundary-v1": _completion_boundary,
    "reinspection-v1": _reinspection,
    "state-transition-v1": _state_transition,
}


def validator_registry_hash() -> str:
    import hashlib
    import json
    payload = {key: {"version": "1", "name": fn.__name__}
               for key, fn in sorted(VALIDATOR_REGISTRY.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def evaluate_validator(validator_id: str, result: Mapping[str, Any]) -> ValidatorResult:
    validator = VALIDATOR_REGISTRY.get(validator_id)
    if validator is None:
        raise ValueError(f"unregistered validator {validator_id!r}")
    return validator(result)

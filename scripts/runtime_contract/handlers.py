"""Machine-owned guard enforcement handlers.

Only handlers listed here may be exported as non-prompt guards.  The model
context contains prompt-mode rules; these handlers execute at the host
boundary and consume no prompt tokens.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Dict, Mapping, Optional


class CompletionBoundaryGateV1:
    version = "1"
    hooks = ("before_completion",)

    @staticmethod
    def before_completion(_session: Any, claim: Any,
                          _enforcement: Mapping[str, Any]) -> Optional[Any]:
        # The runtime performs the complete evidence/claim binding check.  The
        # handler is an actual completion hook: no opaque claim means no
        # completion boundary can be crossed.
        return None if claim is not None else "require_reinspection"


class RetryHypothesisGateV1:
    version = "1"
    hooks = ("before_retry",)

    @staticmethod
    def before_retry(_session: Any, _request: Any,
                     _enforcement: Mapping[str, Any]) -> Optional[Any]:
        return "require_reinspection"


class ContradictionReinspectionGateV1:
    version = "1"
    hooks = ("before_action", "before_transition")

    @staticmethod
    def gate(session: Any, request: Any, _enforcement: Mapping[str, Any]) -> Optional[Any]:
        if any(not record["resolved"] and
               (record.get("subject") in {None, request.subject_ref})
               for record in session.contradictions.values()):
            # Avoid importing the runtime enum into this contract package;
            # the caller maps this sentinel to its GateDecision type.
            return "require_reinspection"
        return None


class AuthorityBoundariesGateV1:
    version = "1"
    hooks = ("before_action",)

    @staticmethod
    def gate(_session: Any, request: Any, enforcement: Mapping[str, Any]) -> Optional[Any]:
        blocked = set((enforcement.get("parameters") or {}).get("blocked_bindings") or ())
        return "block" if request.binding_id in blocked else None


class ActionOutcomeEvidenceGateV1:
    version = "1"
    hooks = ("after_action",)

    @staticmethod
    def after_action(_session: Any, _request: Any,
                     _enforcement: Mapping[str, Any]) -> Optional[Any]:
        # This hook is intentionally not exported structurally until the host
        # execution adapter supplies an actual result.  It remains available
        # for explicit prompt-mode compatibility checks.
        return "requires_tool_result"


STRUCTURAL_HANDLERS = {
    "stale_model.reinspect": ContradictionReinspectionGateV1,
    "completion.boundary_evidence": CompletionBoundaryGateV1,
    "contradiction.reinspect": ContradictionReinspectionGateV1,
    "retry.changed_hypothesis": RetryHypothesisGateV1,
    "authority.boundaries": AuthorityBoundariesGateV1,
    "tool_gate.bad_action": AuthorityBoundariesGateV1,
}

SUPPORTED_HANDLERS: Dict[str, str] = {
    key: handler.version for key, handler in STRUCTURAL_HANDLERS.items()
}

# Handler parameters are part of the executable policy contract.  Keeping the
# schemas next to the implementation lets the runtime reject a malformed
# guard before it can reach an action boundary.
HANDLER_PARAMETER_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "authority.boundaries": {
        "type": "object",
        "required": ["blocked_bindings"],
        "properties": {
            "blocked_bindings": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            }
        },
        "additionalProperties": False,
    },
    "tool_gate.bad_action": {
        "type": "object",
        "required": ["blocked_bindings"],
        "properties": {
            "blocked_bindings": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            }
        },
        "additionalProperties": False,
    },
    "completion.boundary_evidence": {
        "type": "object", "properties": {}, "additionalProperties": False,
    },
    "stale_model.reinspect": {
        "type": "object", "properties": {}, "additionalProperties": False,
    },
    "contradiction.reinspect": {
        "type": "object", "properties": {}, "additionalProperties": False,
    },
    "retry.changed_hypothesis": {
        "type": "object", "properties": {}, "additionalProperties": False,
    },
}


def handler_capabilities() -> Dict[str, Dict[str, Any]]:
    """Return the machine-readable capability surface consumed by DP."""
    return {
        key: {
            "handler": key,
            "version": cls.version,
            "hooks": list(getattr(cls, "hooks", ())),
            "modes": [
                mode for mode, hook in {
                    "structural": None,
                    "tool_gate": "before_action",
                    "state_transition": "before_transition",
                }.items()
                if hook is None or hook in getattr(cls, "hooks", ())
            ],
            "parameter_schema": HANDLER_PARAMETER_SCHEMAS.get(
                key, {"type": "object", "additionalProperties": False}),
        }
        for key, cls in sorted(STRUCTURAL_HANDLERS.items())
    }


def handler_registry_hash() -> str:
    payload = handler_capabilities()
    for key, cls in sorted(STRUCTURAL_HANDLERS.items()):
        payload[key]["implementation"] = hashlib.sha256(
            inspect.getsource(cls).encode("utf-8")).hexdigest()
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_handler_parameters(handler: str, parameters: Any) -> None:
    if not isinstance(parameters, Mapping):
        raise ValueError(f"handler {handler!r} parameters must be an object")
    schema = HANDLER_PARAMETER_SCHEMAS.get(handler)
    if schema is None:
        raise ValueError(f"no parameter schema registered for handler {handler!r}")
    try:
        import jsonschema
        jsonschema.Draft202012Validator(schema).validate(dict(parameters))
    except ImportError as exc:  # pragma: no cover - dependency is a runtime contract
        raise ValueError("jsonschema is required to validate handler parameters") from exc


def handler_for_enforcement(enforcement: Mapping[str, Any]):
    handler = enforcement.get("handler")
    version = enforcement.get("handler_version")
    cls = STRUCTURAL_HANDLERS.get(handler)
    if cls is None or cls.version != version:
        return None
    return cls


def enforcement_for_guard(guard: Mapping[str, Any]) -> Dict[str, Any]:
    raw = guard.get("enforcement")
    if isinstance(raw, Mapping):
        result = dict(raw)
    else:
        key = guard.get("enforcement_key")
        handler = str(key) if key in SUPPORTED_HANDLERS else None
        result = {
            "mode": "structural" if handler else "prompt",
            "handler": handler,
            "handler_version": SUPPORTED_HANDLERS.get(handler or "", "1"),
            "parameters": {},
            "fallback_prompt": None if handler else guard.get("rule"),
        }
    result.setdefault("mode", "prompt")
    result.setdefault("handler", None)
    result.setdefault("handler_version", "1")
    result.setdefault("parameters", {})
    result.setdefault("fallback_prompt", guard.get("rule"))
    return result


def validate_enforcement(guard: Mapping[str, Any]) -> None:
    enforcement = enforcement_for_guard(guard)
    mode = enforcement.get("mode")
    handler = enforcement.get("handler")
    version = enforcement.get("handler_version")
    if mode not in {"structural", "state_transition", "tool_gate", "prompt"}:
        raise ValueError(f"unsupported guard enforcement mode {mode!r}")
    if mode == "prompt":
        return
    if not isinstance(handler, str) or SUPPORTED_HANDLERS.get(handler) != version:
        raise ValueError(f"unsupported guard enforcement handler/version {handler!r}@{version!r}")
    validate_handler_parameters(handler, enforcement.get("parameters", {}))
    handler_cls = STRUCTURAL_HANDLERS[handler]
    hooks = set(getattr(handler_cls, "hooks", ()))
    required_hook = {
        "tool_gate": "before_action",
        "state_transition": "before_transition",
    }.get(mode)
    if required_hook and required_hook not in hooks:
        raise ValueError(
            f"handler {handler!r} does not implement the {mode} hook {required_hook!r}")
    if mode == "structural" and not hooks.intersection({
            "before_action", "before_transition", "before_completion", "before_retry"}):
        raise ValueError(f"structural handler {handler!r} has no dispatched runtime hook")

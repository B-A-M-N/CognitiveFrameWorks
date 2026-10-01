"""Explicit CFW component port for the DigitalPsychology runtime host.

This adapter composes with a host-owned RuntimeSession and agent handle. It
does not replace, subclass, or patch the CFW runtime classes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Sequence

from runtime import context_for


_SCOPE = re.compile(r"^dp-runtime-[0-9a-f]{64}$")
_MODES = {"RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY", "READY_TO_COMPLETE",
          "DELIVER", "REPLAN", "BLOCKED"}
_FRAMEWORK_MODES = {"off", "passive", "normal", "elevated", "reset", "reopen",
                    "mandatory", "recovery", "constrained"}
_CONTROL_VALUES = {
    "verification_intensity": {"passive", "normal", "high", "strict"},
    "exploration_budget": {"disabled", "low", "normal", "bounded"},
    "retry_policy": {"normal", "materially_different_only", "no_retry"},
    "context_strategy": {"standard", "compact", "criterion_focused"},
    "response_strategy": {"direct", "detailed", "structured"},
}


@dataclass(frozen=True)
class PreparedBehaviorDirectiveBatch:
    directives: tuple[dict[str, Any], ...]
    prior_controls: Mapping[str, Mapping[str, Any]]
    prior_behavior_state: Mapping[str, Any] | None


class CFWBehaviorAdapter:
    """Expose validated CFW signals and a reversible DP context-control port."""

    def __init__(self, session: RuntimeSession, agent_handle: Any) -> None:
        required_session_fields = ("bundle", "signals", "injected", "active_statework_ids")
        if any(not hasattr(session, field) for field in required_session_fields):
            raise TypeError("CFW behavior adapter requires a started RuntimeSession")
        if (not callable(getattr(session, "enable_external_behavior_authority", None))
                or not callable(getattr(session, "apply_external_behavior_state", None))
                or not callable(getattr(session, "restore_external_behavior_startup", None))
                or not callable(getattr(session, "available_component_ids", None))):
            raise TypeError("CFW runtime lacks the explicit DP behavior authority API")
        if not callable(getattr(agent_handle, "before_model_call", None)) or not callable(
                getattr(agent_handle, "before_tool_planning", None)):
            raise TypeError("CFW behavior adapter requires an agent context handle")
        self._session = session
        self._agent_handle = agent_handle
        self._controls: dict[str, dict[str, Any]] = {}
        self._current_action_index = -1
        self._behavior_state: dict[str, Any] | None = None
        session.enable_external_behavior_authority()

    def available_component_ids(self) -> tuple[str, ...]:
        return tuple(self._session.available_component_ids())

    def collect_behavior_signals(self, observation: Any) -> tuple[dict[str, Any], ...]:
        self._current_action_index = int(observation.action_index)
        self._controls = {
            key: value for key, value in self._controls.items()
            if self._current_action_index <= value["expires_after_action_index"]
        }
        result = []
        for envelope in self._session.signals[-32:]:
            signal_type = str(envelope.get("signal_type") or "")
            result.append({
                "source": str(envelope.get("source") or "cfw"),
                "code": signal_type,
                "active": True,
                "severity": str(envelope.get("severity") or "warning"),
                "criterion_id": envelope.get("criterion_id"),
                "evidence_refs": list(envelope.get("evidence_refs") or ()),
            })
        return tuple(result)

    def available_statework_ids(self) -> tuple[str, ...]:
        values = tuple(sorted(str(item.get("id") or "")
                              for item in self._session.bundle.stateworks))
        if len(values) > 32 or any(not item or len(item) > 128 for item in values):
            raise ValueError("CFW bundle StateWork IDs exceed the component-port contract")
        if len(values) != len(set(values)):
            raise ValueError("CFW bundle has duplicate StateWork IDs")
        return values

    def prepare_behavior_directives(
            self, directives: Sequence[Mapping[str, Any]]) -> PreparedBehaviorDirectiveBatch:
        if len(directives) > 32:
            raise ValueError("CFW directive batch exceeds its bound")
        normalized = tuple(self._validate_directive(item) for item in directives)
        if len({item["directive_id"] for item in normalized}) != len(normalized):
            raise ValueError("CFW directive IDs must be unique")
        return PreparedBehaviorDirectiveBatch(
            directives=normalized,
            prior_controls={key: dict(value) for key, value in self._controls.items()},
            prior_behavior_state=(dict(self._behavior_state)
                                  if self._behavior_state is not None else None),
        )

    def apply_behavior_directives(self, prepared: PreparedBehaviorDirectiveBatch,
                                  behavior_state: Mapping[str, Any]
                                  ) -> tuple[str, ...]:
        if not isinstance(prepared, PreparedBehaviorDirectiveBatch):
            raise TypeError("CFW directive batch was not prepared by this adapter")
        if not isinstance(behavior_state, Mapping):
            raise TypeError("DP behavior state must be a mapping")
        self._validate_directive_state(prepared.directives, behavior_state)
        self._session.apply_external_behavior_state(behavior_state)
        controls = dict(self._controls)
        for directive in prepared.directives:
            applied = dict(directive)
            applied["expires_after_action_index"] = (
                self._current_action_index + directive["expires_after_actions"])
            controls[self._target_key(directive)] = applied
        if len(controls) > 16:
            raise ValueError("CFW active behavior-control set exceeds its bound")
        self._controls = controls
        self._behavior_state = json.loads(
            json.dumps(dict(behavior_state), sort_keys=True))
        return tuple(item["directive_id"] for item in prepared.directives)

    def synchronize_behavior_state(self, behavior_state: Mapping[str, Any]) -> None:
        if not isinstance(behavior_state, Mapping):
            raise TypeError("DP behavior state must be a mapping")
        self._session.apply_external_behavior_state(behavior_state)
        self._behavior_state = json.loads(
            json.dumps(dict(behavior_state), sort_keys=True))

    def rollback_behavior_directives(self, prepared: PreparedBehaviorDirectiveBatch) -> None:
        if not isinstance(prepared, PreparedBehaviorDirectiveBatch):
            raise TypeError("CFW directive batch was not prepared by this adapter")
        self._controls = {key: dict(value)
                          for key, value in prepared.prior_controls.items()}
        if prepared.prior_behavior_state is None:
            self._session.restore_external_behavior_startup()
            self._behavior_state = None
        else:
            self._session.apply_external_behavior_state(prepared.prior_behavior_state)
            self._behavior_state = dict(prepared.prior_behavior_state)

    def render_behavior_context(self, context: str, *,
                                active_statework_ids: Sequence[str] = ()) -> str:
        if not isinstance(context, str) or len(context) > 16_384:
            raise ValueError("DP behavior projection must be bounded text")
        control_lines = [
            "===== APPLIED DIGITAL PSYCHOLOGY CONTROLS =====",
            "These controls shape component guidance; CFW guards and WARD remain authoritative.",
        ]
        for directive in sorted(self._controls.values(),
                                key=lambda item: (item["target"], item["directive_id"])):
            control_lines.append(
                f"- {directive['operation']} {directive['target']}={directive['value']} "
                f"(cause: {directive['cause']}; expires after action "
                f"{directive['expires_after_action_index']})")
        return context_for(
            self._session,
            behavior_context="\n".join(control_lines) + "\n" + context,
            active_statework_ids=active_statework_ids)

    def before_model_call(self, context: str, *, tool_capable: bool = False,
                          action_hint: Mapping[str, Any] | None = None,
                          active_statework_ids: Sequence[str] = ()) -> str:
        return self._agent_handle.before_model_call(
            tool_capable=tool_capable, action_hint=action_hint,
            behavior_context=self._behavior_context(context),
            active_statework_ids=active_statework_ids)

    def before_tool_planning(self, context: str,
                             action_hint: Mapping[str, Any] | None = None,
                             active_statework_ids: Sequence[str] = ()) -> str:
        return self._agent_handle.before_tool_planning(
            action_hint, behavior_context=self._behavior_context(context),
            active_statework_ids=active_statework_ids)

    def _behavior_context(self, context: str) -> str:
        if not isinstance(context, str) or len(context) > 16_384:
            raise ValueError("DP behavior projection must be bounded text")
        control_lines = [
            "===== APPLIED DIGITAL PSYCHOLOGY CONTROLS =====",
            "These controls shape component guidance; CFW guards and WARD remain authoritative.",
        ]
        for directive in sorted(self._controls.values(),
                                key=lambda item: (item["target"], item["directive_id"])):
            control_lines.append(
                f"- {directive['operation']} {directive['target']}={directive['value']} "
                f"(cause: {directive['cause']}; expires after action "
                f"{directive['expires_after_action_index']})")
        return "\n".join(control_lines) + "\n" + context

    @staticmethod
    def _validate_directive_state(directives: Sequence[Mapping[str, Any]],
                                  state: Mapping[str, Any]) -> None:
        frameworks = state.get("framework_modes")
        if not isinstance(frameworks, Mapping):
            raise ValueError("DP behavior state has no framework modes")
        for directive in directives:
            operation = directive["operation"]
            target = directive["target"]
            expected = directive["value"]
            if operation == "set_mode":
                actual = state.get("cognitive_mode")
            elif operation == "set_framework":
                actual = frameworks.get(target.lower())
            elif operation == "set_control":
                actual = state.get(target)
                if target == "focus_criteria":
                    actual = json.dumps(actual, separators=(",", ":"))
                else:
                    actual = str(actual)
            else:
                raise ValueError("CSW preference directives cannot be applied by CFW")
            if actual != expected:
                raise ValueError(
                    f"DP behavior state does not contain directive value for {target!r}")

    @staticmethod
    def _target_key(directive: Mapping[str, Any]) -> str:
        return f"{directive['operation']}:{directive['target']}"

    @classmethod
    def _validate_directive(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"directive_id", "operation", "target", "value", "cause",
                  "expected_effect", "scope", "expires_after_actions", "intervention_id"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("CFW directive has unknown or missing fields")
        item = dict(value)
        for field in ("directive_id", "target", "cause", "intervention_id", "scope"):
            text = item[field]
            limit = 256 if field == "scope" else 128
            if not isinstance(text, str) or not text or len(text) > limit:
                raise ValueError(f"CFW directive {field} is invalid")
        if not _SCOPE.fullmatch(item["scope"]):
            raise ValueError("CFW directive scope is not a DP runtime identity")
        if (not isinstance(item["expected_effect"], str)
                or not item["expected_effect"] or len(item["expected_effect"]) > 512):
            raise ValueError("CFW directive expected effect is invalid")
        expiry = item["expires_after_actions"]
        if not isinstance(expiry, int) or not 1 <= expiry <= 64:
            raise ValueError("CFW directive expiry is invalid")
        operation, target, raw_value = item["operation"], item["target"], item["value"]
        if not isinstance(raw_value, str) or len(raw_value) > 2048:
            raise ValueError("CFW directive value is invalid")
        if operation == "set_mode":
            if target != "mode" or raw_value not in _MODES:
                raise ValueError("CFW mode directive is unsupported")
        elif operation == "set_framework":
            if (target.lower() not in {"owl", "anchor", "fuse", "ward", "flow", "sispis", "dox"}
                    or raw_value not in _FRAMEWORK_MODES):
                raise ValueError("CFW framework directive is unsupported")
            if target.lower() == "ward" and raw_value != "mandatory":
                raise PermissionError("DigitalPsychology cannot lower the WARD hard floor")
        elif operation == "set_control":
            if target == "recovery_level":
                try:
                    if not 0 <= int(raw_value) <= 3:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("CFW recovery level is invalid") from exc
            elif target == "focus_criteria":
                values = json.loads(raw_value)
                if (not isinstance(values, list) or len(values) > 8
                        or any(not isinstance(item, str) or not item or len(item) > 128
                               for item in values)):
                    raise ValueError("CFW focus criteria are invalid")
            elif target not in _CONTROL_VALUES or raw_value not in _CONTROL_VALUES[target]:
                raise ValueError("CFW control directive is unsupported")
        else:
            raise ValueError("CSW preference directives must use the CSW adapter")
        return item


__all__ = ["CFWBehaviorAdapter", "PreparedBehaviorDirectiveBatch"]

"""Explicit CFW component port for the DigitalPsychology runtime host.

This adapter composes with a host-owned RuntimeSession and agent handle through
CFW's explicit runtime and component-overlay APIs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from runtime import context_for
from runtime_contract.component_control import (COMPONENT_ALLOWED_MODES,
                                                COMPONENT_HARD_FLOOR_MODES,
                                                COMPONENT_IDS)


_SCOPE = re.compile(r"^dp-runtime-[0-9a-f]{64}$")
_MODES = {"RECOVER", "REINSPECT", "DIAGNOSE", "VERIFY", "READY_TO_COMPLETE",
          "DELIVER", "REPLAN", "BLOCKED"}
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
                or not callable(getattr(session, "synchronize_external_behavior_state", None))
                or not callable(getattr(session, "rollback_external_behavior_state", None))
                or not callable(getattr(session, "restore_external_behavior_startup", None))
                or not callable(getattr(session, "preflight_action", None))
                or not callable(getattr(session, "prepare_action", None))
                or not callable(getattr(session, "available_component_ids", None))):
            raise TypeError("CFW runtime lacks the explicit DP behavior and action-gate APIs")
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

    @property
    def lifecycle_revision(self) -> int:
        return self._session.external_behavior_revision

    def authority_identity(self) -> Mapping[str, str]:
        """Stable identity for a CFW execution and its immutable component set."""
        execution = getattr(self._session, "execution_identity", None)
        if execution is not None and callable(getattr(execution, "to_dict", None)):
            identity = execution.to_dict()
        else:
            identity = {
                "session_id": str(getattr(self._session, "session_id", "")),
                "agent_instance_id": str(getattr(
                    self._session, "agent_instance_id", "")),
                "attempt_id": str(getattr(self._session, "attempt_id", "")),
                "task_id": str(getattr(self._session.bundle, "task_id", "")),
            }
        identity = {key: value for key, value in identity.items() if value is not None}
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ValueError("CFW execution identity is incomplete")
        scope_hash = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        components = tuple(sorted(self.available_component_ids()))
        capability_hash = hashlib.sha256(json.dumps(
            components, separators=(",", ":")).encode()).hexdigest()
        return {"adapter_kind": "cfw-component-runtime",
                "authority_id": "cfw-execution:" + scope_hash,
                "protocol_version": "1.0.0", "scope": scope_hash,
                "scope_hash": scope_hash, "capability_hash": capability_hash}

    @property
    def applied_behavior_state(self) -> Mapping[str, Any] | None:
        state = self._session.external_behavior_state
        return dict(state) if state else None

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

    def preflight_action(self, action: Mapping[str, Any]) -> Mapping[str, str]:
        """Apply CFW's hard gate before DP evaluates a sanitized candidate."""
        result = self._session.preflight_action(dict(action))
        return {"action_digest": result.action_digest,
                "decision": result.decision.value}

    def authorize_action(self, action: Mapping[str, Any]) -> Mapping[str, str]:
        """Run CFW's final gate after DP directives have been materialized."""
        result = self._session.prepare_action(dict(action))
        return {"action_digest": result.action_digest,
                "action_request_id": result.action_request_id,
                "decision": result.decision.value}

    def expire_local_behavior_leases(self, action_index: int) -> bool:
        """Drop DP overlays locally when the DP control process is unavailable."""
        if type(action_index) is not int or action_index < 0:
            raise ValueError("local CFW lease expiry requires a non-negative action index")
        self._current_action_index = max(self._current_action_index, action_index)
        expired = any(self._current_action_index > int(
            item["expires_after_action_index"]) for item in self._controls.values())
        if not expired:
            return False
        self._session.restore_external_behavior_startup()
        self._controls.clear()
        self._behavior_state = None
        return True

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
        controls = dict(self._controls)
        for directive in prepared.directives:
            applied = dict(directive)
            applied["expires_after_action_index"] = (
                self._current_action_index + directive["expires_after_actions"])
            controls[self._target_key(directive)] = applied
        if len(controls) > 16:
            raise ValueError("CFW active behavior-control set exceeds its bound")
        self._session.apply_external_behavior_state(behavior_state)
        self._controls = controls
        self._behavior_state = json.loads(
            json.dumps(dict(behavior_state), sort_keys=True))
        return tuple(item["directive_id"] for item in prepared.directives)

    def synchronize_behavior_state(self, behavior_state: Mapping[str, Any]) -> None:
        if not isinstance(behavior_state, Mapping):
            raise TypeError("DP behavior state must be a mapping")
        self._session.synchronize_external_behavior_state(behavior_state)
        self._behavior_state = json.loads(
            json.dumps(dict(behavior_state), sort_keys=True))

    def rollback_behavior_directives(self, prepared: PreparedBehaviorDirectiveBatch) -> None:
        if not isinstance(prepared, PreparedBehaviorDirectiveBatch):
            raise TypeError("CFW directive batch was not prepared by this adapter")
        if prepared.prior_behavior_state is None:
            self._session.restore_external_behavior_startup()
            self._behavior_state = None
        else:
            self._session.rollback_external_behavior_state(
                prepared.prior_behavior_state,
                expected_current_revision=self._session.external_behavior_revision)
            self._behavior_state = dict(prepared.prior_behavior_state)
        self._controls = {key: dict(value)
                          for key, value in prepared.prior_controls.items()}

    def restore_behavior_state(self, behavior_state: Mapping[str, Any], *,
                               expected_current_revision: int) -> None:
        """Restore DP's last committed projection after host restart recovery."""
        self._session.rollback_external_behavior_state(
            behavior_state,
            expected_current_revision=expected_current_revision,
        )
        self._controls = {}
        self._behavior_state = json.loads(
            json.dumps(dict(behavior_state), sort_keys=True))

    def render_behavior_context(self, context: str, *,
                                active_statework_ids: Sequence[str] = ()) -> str:
        if not isinstance(context, str) or len(context) > 16_384:
            raise ValueError("DP behavior projection must be bounded text")
        return context_for(
            self._session,
            behavior_context=self._replace_guidance_slot(context),
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
        return self._replace_guidance_slot(context)

    @staticmethod
    def _replace_guidance_slot(context: str) -> str:
        opening = "===== CURRENT BEHAVIOR GUIDANCE ====="
        closing = "===== END CURRENT BEHAVIOR GUIDANCE ====="
        starts = []
        cursor = 0
        while (index := context.find(opening, cursor)) >= 0:
            starts.append(index)
            cursor = index + len(opening)
        if not starts:
            return context
        first, last = starts[0], starts[-1]
        last_end = context.find(closing, last)
        if last_end < 0:
            raise ValueError("DP model guidance slot is missing its closing marker")
        if len(starts) == 1:
            return context
        block = context[last:last_end + len(closing)]
        suffix = context[last_end + len(closing):]
        return (context[:first].rstrip() + "\n\n" + block + suffix).strip()

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
            component_id = target.lower()
            if (component_id not in COMPONENT_IDS
                    or raw_value not in COMPONENT_ALLOWED_MODES[component_id]):
                raise ValueError("CFW framework directive is unsupported")
            if (component_id in COMPONENT_HARD_FLOOR_MODES
                    and raw_value != COMPONENT_HARD_FLOOR_MODES[component_id]):
                raise PermissionError(
                    f"DigitalPsychology cannot lower the {component_id.upper()} hard floor")
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

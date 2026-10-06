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
                                                COMPONENT_BASELINE_MODES,
                                                COMPONENT_DYNAMIC_CONTROL,
                                                COMPONENT_HARD_FLOOR_MODES,
                                                COMPONENT_IDS,
                                                COMPONENT_MODEL_VISIBILITY,
                                                PIPELINE_MANIFEST_HASH,
                                                PIPELINE_MANIFEST_VERSION)


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
    prior_behavior_projection: Mapping[str, Any] | None


# CFW's supported actuation-projection contract. DP reports its own version in
# the projection payload; an unknown version is rejected at the boundary rather
# than discovered during the first behavioral intervention.
CFW_BEHAVIOR_PROJECTION_VERSION = "2.1.0"
CFW_ADAPTER_PROTOCOL_VERSION = "1.2.0"


class CFWBehaviorAdapter:
    """Expose validated CFW signals and a reversible DP context-control port."""

    #: Projection contract this adapter accepts. Declared on the class so a host
    #: can read it before it has an adapter instance.
    behavior_projection_version = CFW_BEHAVIOR_PROJECTION_VERSION

    def __init__(self, session: RuntimeSession, agent_handle: Any) -> None:
        required_session_fields = (
            "bundle", "identity_authority", "behavior", "lifecycle_workflow",
            "injected",
        )
        if any(not hasattr(session, field) for field in required_session_fields):
            raise TypeError("CFW behavior adapter requires a started RuntimeSession")
        if (not hasattr(session.identity_authority, "identity")
                or not hasattr(session.identity_authority, "authorities")
                or not hasattr(session.lifecycle_workflow, "active_statework_ids")
                or not hasattr(session.behavior, "external_projection")):
            raise TypeError("CFW RuntimeSession is missing a required state aggregate")
        if (not callable(getattr(session, "enable_external_behavior_authority", None))
                or not callable(getattr(session, "apply_external_behavior_projection", None))
                or not callable(getattr(session, "synchronize_external_behavior_projection", None))
                or not callable(getattr(session, "rollback_external_behavior_projection", None))
                or not callable(getattr(session, "restore_external_behavior_startup", None))
                or not callable(getattr(session, "peek_behavior_signals", None))
                or not callable(getattr(session, "acknowledge_behavior_signals", None))
                or not callable(getattr(session, "preflight_action", None))
                or not callable(getattr(session, "prepare_action", None))
                or not callable(getattr(session, "available_component_ids", None))):
            raise TypeError("CFW runtime lacks the explicit DP behavior and action-gate APIs")
        if not callable(getattr(agent_handle, "before_model_call", None)) or not callable(
                getattr(agent_handle, "before_tool_planning", None)):
            raise TypeError("CFW behavior adapter requires an agent context handle")
        self._session = session
        self._agent_handle = agent_handle
        session.enable_external_behavior_authority()

    def available_component_ids(self) -> tuple[str, ...]:
        return tuple(self._session.available_component_ids())

    def task_behavior_contract(self) -> Mapping[str, Any]:
        contract = getattr(self._session, "task_behavior_contract", None)
        if not callable(contract):
            raise RuntimeError("CFW runtime lacks its task behavior contract")
        value = contract()
        if not isinstance(value, Mapping):
            raise RuntimeError("CFW returned a malformed task behavior contract")
        return dict(value)

    def composite_runtime_snapshot(self) -> Mapping[str, Any]:
        """Expose the host-composed task identity to DP over the private port."""
        snapshot = getattr(self._session.bundle, "composite_snapshot", None)
        serializer = getattr(snapshot, "to_dict", None)
        value = serializer() if callable(serializer) else None
        if not isinstance(value, Mapping):
            raise RuntimeError("CFW runtime lacks its composite task snapshot")
        return dict(value)

    def workflow_authority_identities(self) -> Mapping[str, Mapping[str, str]]:
        """Return CSW identities for the StateWorks attached to this CFW task."""
        authority = self._session.identity_authority.authorities.state_authority
        if authority is None:
            return {}
        provider = getattr(authority, "workflow_authority_identity", None)
        if not callable(provider):
            raise RuntimeError(
                "CFW StateAuthority does not expose stable workflow authority identities")
        pinned_subjects = dict(self._session.bundle.pinned.get(
            "statework_subject_refs") or {})
        result: dict[str, Mapping[str, str]] = {}
        for statework in self._session.bundle.stateworks:
            if not isinstance(statework, Mapping):
                raise RuntimeError("CFW task contains a malformed StateWork binding")
            statework_id = statework.get("id")
            subject_ref = (statework.get("subject_ref")
                           or pinned_subjects.get(statework_id)
                           or self._session.bundle.policy.subject_ref)
            contract_hash = statework.get("workflow_contract_hash")
            if (not isinstance(statework_id, str) or not statework_id
                    or not isinstance(subject_ref, str) or not subject_ref
                    or not isinstance(contract_hash, str) or not contract_hash):
                raise RuntimeError(
                    "CFW task omitted a pinned StateWork scope or contract hash")
            identity = provider(statework_id=statework_id,
                                subject_ref=subject_ref,
                                contract_hash=contract_hash)
            if callable(getattr(identity, "to_dict", None)):
                identity = identity.to_dict()
            if (not isinstance(identity, Mapping)
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           for key, value in identity.items())):
                raise RuntimeError("CFW StateAuthority returned a malformed workflow identity")
            result[statework_id] = dict(identity)
        return result

    @property
    def lifecycle_revision(self) -> int:
        return self._session.external_behavior_revision

    def authority_identity(self) -> Mapping[str, str]:
        """Stable identity for a CFW execution and its immutable component set."""
        identity_state = self._session.identity_authority.identity
        execution = identity_state.execution_identity
        if execution is not None and callable(getattr(execution, "to_dict", None)):
            identity = execution.to_dict()
        else:
            identity = {
                "session_id": str(identity_state.session_id),
                "agent_instance_id": str(identity_state.agent_instance_id),
                "attempt_id": str(identity_state.attempt_id),
                "task_id": str(getattr(self._session.bundle, "task_id", "")),
            }
        identity = {key: value for key, value in identity.items() if value is not None}
        if any(not isinstance(value, str) or not value for value in identity.values()):
            raise ValueError("CFW execution identity is incomplete")
        scope_hash = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        components = tuple(sorted(self.available_component_ids()))
        component_contract = {
            component_id: {
                "allowed_modes": sorted(COMPONENT_ALLOWED_MODES[component_id]),
                "baseline_mode": COMPONENT_BASELINE_MODES[component_id],
                "dynamic_control": COMPONENT_DYNAMIC_CONTROL[component_id],
                "hard_floor_mode": COMPONENT_HARD_FLOOR_MODES.get(component_id),
                "model_visibility": COMPONENT_MODEL_VISIBILITY[component_id],
            }
            for component_id in components
        }
        component_contract_hash = hashlib.sha256(json.dumps({
            "components": component_contract,
            "pipeline_manifest_version": PIPELINE_MANIFEST_VERSION,
            "pipeline_manifest_hash": PIPELINE_MANIFEST_HASH,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        task_contract = self.task_behavior_contract()
        capability_hash = hashlib.sha256(json.dumps({
            "component_contract_hash": component_contract_hash,
            "behavior_projection_version": self.behavior_projection_version,
            "task_behavior_contract_hash": task_contract["contract_hash"],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"adapter_kind": "cfw-component-runtime",
                "authority_id": "cfw-execution:" + scope_hash,
                "adapter_protocol_version": CFW_ADAPTER_PROTOCOL_VERSION,
                "behavior_projection_version": self.behavior_projection_version,
                "scope": scope_hash,
                "scope_hash": scope_hash, "capability_hash": capability_hash,
                "component_contract_hash": component_contract_hash,
                "task_behavior_contract_hash": task_contract["contract_hash"]}

    @property
    def applied_behavior_projection(self) -> Mapping[str, Any] | None:
        projection = self._session.behavior.external_projection
        return dict(projection) if projection else None

    @property
    def effective_behavior_projection(self) -> Mapping[str, Any] | None:
        """Expose CFW's surviving local lease values during DP outages."""
        projection = self._session.effective_external_behavior_projection()
        return dict(projection) if projection else None

    def peek_behavior_signals(self, *, after_cursor: int | None = None,
                              maximum: int = 32) -> Mapping[str, Any]:
        batch = self._session.peek_behavior_signals(
            after_cursor=after_cursor, maximum=maximum)
        return {"after_cursor": batch.after_cursor,
                "next_cursor": batch.next_cursor,
                "signals": [dict(envelope) for envelope in batch.signals]}

    def acknowledge_behavior_signals(self, *, through_cursor: int) -> None:
        self._session.acknowledge_behavior_signals(
            through_cursor=through_cursor)

    def collect_behavior_signals(self, observation: Any) -> tuple[dict[str, Any], ...]:
        """Compatibility view of the current unacknowledged batch."""
        batch = self.peek_behavior_signals(maximum=32)
        result = []
        goal_set = getattr(observation, "goal_set", None)
        objective_criteria = ({item.criterion_id for item in goal_set.items}
                              if goal_set is not None else set())
        for envelope in batch["signals"]:
            signal_type = str(envelope.get("signal_type") or "")
            criterion_id = envelope.get("criterion_id")
            # Canonical CFW signal envelopes use subject_ref for the affected
            # artifact. When it exactly names a host criterion, preserve that
            # scope for DP's multi-criterion trigger checks.
            subject_ref = envelope.get("subject_ref")
            if criterion_id is None and subject_ref in objective_criteria:
                criterion_id = subject_ref
            result.append({
                "source": str(envelope.get("source") or "cfw"),
                "code": signal_type,
                "active": True,
                "severity": str(envelope.get("severity") or "warning"),
                "criterion_id": criterion_id,
                "evidence_refs": list(envelope.get("evidence_refs") or ()),
                "signal_id": envelope.get("signal_id"),
                "cause_id": envelope.get("cause_id"),
                "required_action": envelope.get("required_action"),
                "scope": envelope.get("scope"),
                "subject_ref": subject_ref,
            })
        return tuple(result)

    def _gate_receipt(self, result: Any, stage: str) -> Mapping[str, Any]:
        identity = self.authority_identity()
        return {
            "authority_id": identity["authority_id"],
            "adapter_kind": identity["adapter_kind"],
            "adapter_protocol_version": identity["adapter_protocol_version"],
            "stage": stage,
            "action_digest": result.action_digest,
            "decision": result.decision.value,
            "reason_codes": list(result.reason_codes),
        }

    def preflight_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        """Apply CFW's hard gate before DP evaluates a sanitized candidate."""
        result = self._session.preflight_action(dict(action))
        return {"action_digest": result.action_digest,
                "decision": result.decision.value,
                "gate_receipt": self._gate_receipt(result, "preflight")}

    def authorize_action(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run CFW's final gate after DP directives have been materialized."""
        result = self._session.prepare_action(dict(action))
        return {"action_digest": result.action_digest,
                "action_request_id": result.action_request_id,
                "decision": result.decision.value,
                "gate_receipt": self._gate_receipt(result, "final")}

    def expire_local_behavior_leases(self, action_index: int) -> bool:
        """Expire only target-scoped DP leases while preserving live controls."""
        if type(action_index) is not int or action_index < 0:
            raise ValueError("local CFW lease expiry requires a non-negative action index")
        return self._session.expire_external_behavior_leases(action_index)

    def prepare_behavior_directives(
            self, directives: Sequence[Mapping[str, Any]]) -> PreparedBehaviorDirectiveBatch:
        if len(directives) > 32:
            raise ValueError("CFW directive batch exceeds its bound")
        normalized = tuple(self._validate_directive(item) for item in directives)
        if len({item["directive_id"] for item in normalized}) != len(normalized):
            raise ValueError("CFW directive IDs must be unique")
        return PreparedBehaviorDirectiveBatch(
            directives=normalized,
            prior_behavior_projection=(
                dict(self._session.behavior.external_projection)
                if self._session.behavior.external_projection else None),
        )

    def apply_behavior_projection(self, prepared: PreparedBehaviorDirectiveBatch,
                                  projection: Mapping[str, Any]) -> tuple[str, ...]:
        """Apply a versioned CFW actuation projection.

        This is the narrow contract CFW should consume: it carries only the
        control values CFW actuates, never DP's private bookkeeping. The
        projection's own ``protocol_version`` is checked before anything is
        applied, so an incompatible host fails at the boundary instead of
        midway through an intervention.
        """
        if not isinstance(prepared, PreparedBehaviorDirectiveBatch):
            raise TypeError("CFW directive batch was not prepared by this adapter")
        if not isinstance(projection, Mapping):
            raise TypeError("CFW behavior projection must be a mapping")
        version = projection.get("protocol_version")
        if version != self.behavior_projection_version:
            raise ValueError(
                f"unsupported CFW behavior projection version {version!r}")
        from runtime_contract.component_control import ExternalBehaviorProjection
        canonical = ExternalBehaviorProjection.from_mapping(projection).to_dict()
        if len(prepared.directives) > 32:
            raise ValueError("CFW directive batch exceeds its bound")
        self._validate_directive_projection(prepared.directives, canonical)
        self._session.apply_external_behavior_projection(canonical)
        return tuple(item["directive_id"] for item in prepared.directives)

    def synchronize_behavior_projection(self, projection: Mapping[str, Any]) -> None:
        if not isinstance(projection, Mapping):
            raise TypeError("CFW behavior projection must be a mapping")
        self._session.synchronize_external_behavior_projection(projection)

    def rollback_behavior_directives(self, prepared: PreparedBehaviorDirectiveBatch) -> None:
        if not isinstance(prepared, PreparedBehaviorDirectiveBatch):
            raise TypeError("CFW directive batch was not prepared by this adapter")
        if prepared.prior_behavior_projection is None:
            self._session.restore_external_behavior_startup()
        else:
            self._session.rollback_external_behavior_projection(
                prepared.prior_behavior_projection,
                expected_current_revision=self._session.external_behavior_revision)

    def restore_behavior_projection(self, projection: Mapping[str, Any], *,
                                    expected_current_revision: int) -> None:
        """Restore the last committed projection after host restart recovery."""
        self._session.rollback_external_behavior_projection(
            projection,
            expected_current_revision=expected_current_revision,
        )

    @staticmethod
    def _validate_directive_projection(directives: Sequence[Mapping[str, Any]],
                                       projection: Mapping[str, Any]) -> None:
        frameworks = projection["framework_modes"]
        final_values: dict[str, str] = {}
        for item in directives:
            operation = item.get("operation")
            target = str(item.get("target") or "")
            value = str(item.get("value") or "")
            if operation == "set_mode":
                key = "mode"
            elif operation == "set_framework":
                key = "framework:" + target.lower()
            elif operation == "set_control":
                key = "control:" + target
            else:
                raise ValueError("CFW received a non-CFW directive in its prepared batch")

            # A boundary may close one intervention and start another that
            # changes the same target. The projection represents the final
            # composed state, so validate the last directive for each target.
            final_values[key] = value

        for key, value in final_values.items():
            if key == "mode":
                actual = projection["cognitive_mode"]
            elif key.startswith("framework:"):
                actual = frameworks.get(key.split(":", 1)[1])
            else:
                target = key.removeprefix("control:")
                if target == "focus_criteria":
                    actual = json.dumps(projection["focus_criteria"], separators=(",", ":"))
                elif target == "recovery_level":
                    actual = str(projection["recovery_level"])
                else:
                    actual = projection.get(target)
            if actual is None or str(actual) != value:
                raise ValueError(
                    f"CFW projection does not materialize directive target {key!r}: "
                    f"projection value {actual!r}, directive value {value!r}")

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

    def _validate_directive(self, value: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"directive_id", "operation", "target", "value", "cause",
                  "expected_effect", "scope", "expires_after_actions", "intervention_id",
                  "expires_at_action"}
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
        absolute_expiry = item["expires_at_action"]
        if (absolute_expiry is not None
                and (type(absolute_expiry) is not int or absolute_expiry < 0)):
            raise ValueError("CFW directive absolute expiry is invalid")
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
            if raw_value in self.task_behavior_contract()["suppression_modes"].get(
                    component_id, ()):
                raise PermissionError(
                    f"DigitalPsychology cannot lower the {component_id.upper()} task baseline floor")
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

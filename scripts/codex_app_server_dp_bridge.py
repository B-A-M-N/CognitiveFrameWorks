"""Connect a generic Codex App Server task to DP's private host bridge.

This module does not construct or expose a provider-specific MCP server. The
trusted application host constructs DigitalPsychology's HostControlBridge
with its CSW authority and private control client, then supplies bounded
host-authored projections for App Server actions and evidence.
"""
from __future__ import annotations

import inspect
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from codex_app_server_adapter import CodexCognitiveHost


class DigitalPsychologyBridgeHost(CodexCognitiveHost):
    """Map Codex lifecycle callbacks onto a DP ``HostControlBridge``.

    By default, opening requires CFW ``combined_enforcing`` mode, pinned CFW
    CSW workflow identities, and a DP CSW port. Set ``require_shared_csw`` to
    false only for an explicitly CFW+DP task without CSW workflows.

    ``candidate_factory`` converts a CFW action request into DP's validated
    ActionCandidate. ``observation_factory`` converts host-observed results or
    validator evidence into DP RuntimeObservation values. Both callbacks stay
    application-owned because only the application knows which criteria and
    artifacts an action can affect.
    """

    def __init__(self, bridge: Any = None, *,
                 bridge_factory: Callable[..., Any] | None = None,
                 require_shared_csw: bool = True,
                 candidate_factory: Callable[..., Any],
                 observation_factory: Callable[..., Any]) -> None:
        if (bridge is None) == (bridge_factory is None):
            raise TypeError("provide exactly one bridge or bridge_factory")
        if not callable(candidate_factory) or not callable(observation_factory):
            raise TypeError("host action and evidence projection factories are required")
        if type(require_shared_csw) is not bool:
            raise TypeError("require_shared_csw must be a boolean")
        self.bridge = bridge
        self.bridge_factory = bridge_factory
        self.require_shared_csw = require_shared_csw
        self.candidate_factory = candidate_factory
        self.observation_factory = observation_factory
        self.request: Any = None
        self.task_handle: Any = None
        self._opened = False
        self._closed = False
        self._bridge_ready = False
        self._transport_identity: dict[str, str] | None = None
        self._runtime_mode: str | None = None
        self._behavior_adapter: Any = None
        self._workflow_ids: tuple[str, ...] = ()
        self._cfw_authority_verified = False
        self._shared_csw_authority_verified = False
        self._intervention_origins: Counter[str] = Counter()
        self._known_intervention_ids: set[str] = set()

    def _intervention_ids(self) -> set[str]:
        state = getattr(self.bridge, "behavior_state", None)
        if state is None:
            return set()
        ids = set()
        for field in ("active_interventions", "intervention_history"):
            for item in getattr(state, field, ()) or ():
                value = getattr(item, "intervention_id", None)
                if isinstance(value, str):
                    ids.add(value)
        return ids

    async def _call_tracked(self, method: str, *, origin: str,
                            **kwargs: Any) -> Any:
        result = await self._call(method, **kwargs)
        after = self._intervention_ids()
        newly_observed = after - self._known_intervention_ids
        self._intervention_origins[origin] += len(newly_observed)
        self._known_intervention_ids = after
        return result

    async def _call(self, method: str, **kwargs: Any) -> Any:
        callback = getattr(self.bridge, method, None)
        if not callable(callback):
            raise RuntimeError(  # noqa: TRY004 -- missing protocol method is configuration.
                f"DigitalPsychology bridge lacks {method}")
        result = callback(**kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _objective(request: Any) -> str:
        contract = getattr(request, "goal_contract", None)
        if isinstance(contract, Mapping):
            value = contract.get("objective")
        else:
            value = getattr(contract, "objective", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _criterion_ids(request: Any) -> tuple[str, ...]:
        contract = getattr(request, "goal_contract", None)
        if isinstance(contract, Mapping):
            criteria = contract.get("acceptance_criteria", ())
        else:
            criteria = getattr(contract, "acceptance_criteria", ())
        result = []
        for item in criteria or ():
            value = (item.get("criterion_id") if isinstance(item, Mapping)
                     else getattr(item, "criterion_id", None))
            if isinstance(value, str) and value:
                result.append(value)
        return tuple(result)

    @staticmethod
    def _require_ready(name: str, result: Any) -> Mapping[str, Any]:
        if not isinstance(result, Mapping):
            raise TypeError(f"DigitalPsychology {name} boundary returned no status")
        if result.get("status") not in {"ready", "reconciled", "ok"}:
            raise RuntimeError(f"DigitalPsychology {name} boundary is not ready")
        if result.get("action_authorized") is False:
            raise RuntimeError(f"DigitalPsychology blocked {name}")
        return result

    async def open_task(self, *, request: Any, runtime: Any, task_handle: Any,
                        host_handle: Any, session_id: str, session_nonce: str,
                        resume: bool, runtime_mode: str, thread_id: str | None,
                        app_server_session_id: str | None) -> Mapping[str, Any]:
        if self.bridge is None:
            created = self.bridge_factory(
                request=request, runtime=runtime, task_handle=task_handle,
                host_handle=host_handle, session_id=session_id,
                session_nonce=session_nonce, resume=resume,
                runtime_mode=runtime_mode, thread_id=thread_id,
                app_server_session_id=app_server_session_id)
            self.bridge = (await created if inspect.isawaitable(created) else created)
            if self.bridge is None:
                raise RuntimeError("DigitalPsychology bridge factory returned no bridge")
        identity = getattr(self.bridge, "identity", None)
        if (identity is None or getattr(identity, "session_id", None) != session_id
                or getattr(identity, "task_id", None) != request.task_id
                or getattr(identity, "agent_instance_id", None)
                != getattr(task_handle, "agent_instance_id", None)):
            raise RuntimeError(
                "DigitalPsychology bridge identity does not match the CFW task")

        objective = self._objective(request)
        if self.require_shared_csw and not objective:
            raise RuntimeError("CFW+CSW+DP requires a host-owned task objective")
        if objective and getattr(self.bridge, "objective", None) != objective:
            raise RuntimeError(
                "DigitalPsychology objective does not match the CFW task contract")
        criteria = self._criterion_ids(request)
        if self.require_shared_csw and not criteria:
            raise RuntimeError("CFW+CSW+DP requires host-owned acceptance criteria")
        dp_criteria = set(getattr(self.bridge, "objective_criteria", ()))
        if set(criteria) != dp_criteria:
            raise RuntimeError(
                "DigitalPsychology objective criteria do not match the CFW task")

        behavior_adapter = task_handle.digital_psychology_adapter()
        expected_authority = behavior_adapter.authority_identity()
        if self.require_shared_csw:
            if runtime_mode != "combined_enforcing":
                raise RuntimeError(
                    "CFW+CSW+DP requires combined_enforcing runtime mode")
            workflow_identities = behavior_adapter.workflow_authority_identities()
            if not workflow_identities:
                raise RuntimeError(
                    "CFW task has no shared CSW workflow authority bindings")
            self._workflow_ids = tuple(sorted(workflow_identities))
            if getattr(self.bridge, "csw", None) is None:
                raise RuntimeError("DigitalPsychology bridge has no CSW authority port")
        cfw_port = getattr(self.bridge, "cfw", None)
        actual_authority_provider = getattr(cfw_port, "authority_identity", None)
        actual_authority = (actual_authority_provider()
                            if callable(actual_authority_provider) else None)
        if not isinstance(expected_authority, Mapping) or actual_authority != expected_authority:
            raise RuntimeError(
                "DigitalPsychology bridge is connected to a different CFW task")

        self.request = request
        self.task_handle = task_handle
        self._runtime_mode = runtime_mode
        self._behavior_adapter = behavior_adapter
        self._cfw_authority_verified = True
        # The adapter calls close on every open failure. Mark ownership before
        # opening so a partially initialized private control client is released.
        self._opened = True
        opened = await self._call_tracked("open", origin="startup")
        self._require_ready("open", opened)
        if getattr(self.bridge, "_needs_reconciliation", False):
            raise RuntimeError("DigitalPsychology bridge requires reconciliation")
        self._bridge_ready = True
        self._shared_csw_authority_verified = bool(
            self.require_shared_csw and self._workflow_ids
            and getattr(self.bridge, "csw", None) is not None)
        return opened

    @property
    def transport_identity(self) -> Mapping[str, str]:
        """App Server IDs retained separately from stable CFW task identity."""
        return dict(self._transport_identity or {})

    def status_snapshot(self) -> Mapping[str, Any]:
        """Return bounded operator evidence for the private triad connection."""
        state = getattr(self.bridge, "behavior_state", None)
        behavior_revision = getattr(state, "revision", None)
        if type(behavior_revision) is not int or behavior_revision < 0:
            behavior_revision = None
        cfw_revision = getattr(self._behavior_adapter, "lifecycle_revision", None)
        if type(cfw_revision) is not int or cfw_revision < 0:
            cfw_revision = None

        intervention_count = None
        intervention_metrics = None
        active = getattr(state, "active_interventions", None)
        history = getattr(state, "intervention_history", None)
        if isinstance(active, (tuple, list)) and isinstance(history, (tuple, list)):
            intervention_count = len(self._intervention_ids())
            active_ids = {
                value for item in active
                if isinstance((value := getattr(item, "intervention_id", None)), str)}
            outcome_by_id = {
                value: getattr(item, "outcome", None) for item in history
                if isinstance((value := getattr(item, "intervention_id", None)), str)}
            outcomes = tuple(outcome_by_id.values())
            created_by_origin = {
                key: int(self._intervention_origins.get(key, 0))
                for key in ("startup", "runtime_behavior", "post_action", "completion")}
            intervention_metrics = {
                "unique_created": sum(created_by_origin.values()),
                "active": len(active_ids),
                "applications": len(active_ids | set(outcome_by_id)),
                "closed": len(outcome_by_id),
                "effective": sum(outcome == "effective" for outcome in outcomes),
                "ineffective": sum(outcome == "ineffective" for outcome in outcomes),
                "unknown": sum(outcome not in {"effective", "ineffective"}
                               for outcome in outcomes),
                "created_by_origin": created_by_origin,
                "semantics": (
                    "unique_created counts distinct IDs first observed by this bridge; "
                    "applications counts distinct IDs retained in active state or outcome "
                    "history; closed/outcome counts cover retained history only"),
            }

        metrics: dict[str, Any] = {}
        metrics_provider = getattr(self.bridge, "production_metrics_snapshot", None)
        if callable(metrics_provider):
            try:
                raw = metrics_provider()
            except Exception:  # noqa: BLE001 -- metrics are best-effort diagnostics.
                raw = None
            if isinstance(raw, Mapping):
                counters = raw.get("counters")
                timers = raw.get("timers")
                if isinstance(counters, Mapping):
                    metrics["counters"] = {
                        str(key): value for key, value in counters.items()
                        if isinstance(key, str) and type(value) is int and value >= 0
                    }
                if isinstance(timers, Mapping):
                    safe_timers: dict[str, dict[str, int | float]] = {}
                    for key, timer in timers.items():
                        if not isinstance(key, str) or not isinstance(timer, Mapping):
                            continue
                        fields = {name: timer[name] for name in
                                  ("count", "total_ms", "max_ms")
                                  if name in timer and type(timer[name]) in (int, float)
                                  and math.isfinite(float(timer[name]))
                                  and timer[name] >= 0}
                        if fields:
                            safe_timers[key] = fields
                    metrics["timers"] = safe_timers

        fault = getattr(self.bridge, "fault", None)
        fault_reason = (" ".join(fault.split())[:512]
                        if isinstance(fault, str) and fault else None)
        return {
            "status": ("ready" if self._bridge_ready and not self._closed
                       else "closed" if self._closed else "not_ready"),
            "private_dp_bridge_open": self._bridge_ready and not self._closed,
            "cfw_authority_verified": self._cfw_authority_verified,
            "runtime_mode": self._runtime_mode,
            "shared_csw_authority_verified": self._shared_csw_authority_verified,
            "shared_csw_workflow_count": len(self._workflow_ids),
            "shared_csw_workflow_ids": list(self._workflow_ids),
            "cfw_behavior_revision": cfw_revision,
            "dp_behavior_revision": behavior_revision,
            "dp_intervention_count": intervention_count,
            "dp_intervention_count_semantics": (
                "unique intervention IDs in current active state plus retained history"),
            "dp_intervention_creations_by_origin": {
                key: int(self._intervention_origins.get(key, 0))
                for key in ("startup", "runtime_behavior", "post_action",
                            "completion")},
            "dp_intervention_creations_semantics": (
                "unique intervention IDs first observed after bridge calls by origin; "
                "startup includes persisted IDs first visible when the bridge opens"),
            "dp_intervention_metrics": intervention_metrics,
            "dp_faulted": isinstance(fault, str) and bool(fault),
            "dp_fault_reason": fault_reason,
            "production_metrics": metrics,
        }

    async def task_started(self) -> Mapping[str, Any]:
        return self._require_ready("task start", await self._call_tracked(
            "task_started", origin="startup"))

    async def bind_transport_identity(self, *, thread_id: str,
                                      app_server_session_id: str) -> Mapping[str, Any]:
        self._transport_identity = {
            "thread_id": thread_id,
            "app_server_session_id": app_server_session_id,
        }
        return {"status": "ready"}

    async def before_model_call(self, *, context: str,
                                tool_capable: bool) -> str:
        if not self._opened or self._closed:
            raise RuntimeError("DigitalPsychology bridge is not open")
        result = await self._call_tracked(
            "before_model_call", origin="runtime_behavior", context=context,
            tool_capable=tool_capable)
        if not isinstance(result, str):
            raise TypeError("DigitalPsychology returned invalid model context")
        return result

    async def before_action(self, *, action: Mapping[str, Any], binding: str,
                            invocation_id: str) -> Mapping[str, Any]:
        candidate = self.candidate_factory(
            action=dict(action), binding=binding, invocation_id=invocation_id,
            request=self.request, task_handle=self.task_handle)
        result = await self._call_tracked(
            "before_action_candidate", origin="runtime_behavior", candidate=candidate,
            cfw_action_request=dict(action))
        if not isinstance(result, Mapping):
            raise TypeError("DigitalPsychology returned an invalid action decision")
        return result

    async def after_action(self, *, action: Mapping[str, Any], binding: str,
                           invocation_id: str, action_request_id: str,
                           actual_result: Mapping[str, Any], result_digest: str,
                           evidence_ref: str) -> Mapping[str, Any]:
        observation = self.observation_factory(
            boundary="post_action", action=dict(action), binding=binding,
            invocation_id=invocation_id, action_request_id=action_request_id,
            actual_result=dict(actual_result), result_digest=result_digest,
            evidence_ref=evidence_ref, request=self.request,
            task_handle=self.task_handle, bridge=self.bridge)
        return self._require_ready(
            "post-action", await self._call_tracked(
                "after_action", origin="post_action", observation=observation))

    async def observe_validator(self, *, validator_id: str,
                                result: Mapping[str, Any],
                                criterion_evidence_refs: Mapping[str, Sequence[str]]) -> Mapping[str, Any]:
        return await self._observe("validator", validator_id, result,
                                   criterion_evidence_refs)

    async def before_completion(self, *, validator_id: str,
                                result: Mapping[str, Any],
                                criterion_evidence_refs: Mapping[str, Sequence[str]]) -> Mapping[str, Any]:
        boundary = await self._observe("pre_completion", validator_id, result,
                                       criterion_evidence_refs)
        return {**boundary, "decision": boundary.get("decision", "allow")}

    async def _observe(self, boundary: str, validator_id: str,
                       result: Mapping[str, Any],
                       criterion_evidence_refs: Mapping[str, Sequence[str]]) -> Mapping[str, Any]:
        observation = self.observation_factory(
            boundary=boundary, validator_id=validator_id,
            validator_result=dict(result),
            criterion_evidence_refs={key: tuple(refs)
                                     for key, refs in criterion_evidence_refs.items()},
            request=self.request, task_handle=self.task_handle,
            bridge=self.bridge)
        response = await self._call_tracked(
            "observe_boundary", origin="completion", boundary_kind=boundary,
            observation=observation)
        return self._require_ready(boundary, response)

    async def reconcile(self) -> Mapping[str, Any]:
        result = self._require_ready("reconciliation", await self._call_tracked(
            "reconcile", origin="startup"))
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.bridge is not None:
            await self._call("close")
        self._opened = False
        self._bridge_ready = False


__all__ = ["DigitalPsychologyBridgeHost"]

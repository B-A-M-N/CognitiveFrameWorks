"""Inference backend preflight for the generic Codex App Server protocol."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from .launch import CodexLaunchProfile


class _AppServerClient(Protocol):
    async def request(self, method: str, params: Mapping[str, Any] | None = None,
                      *, timeout: float = 30.0) -> Any: ...


_WORKSPACE_ROUTING_ERRORS = (
    "workspace routing discovery failed",
    "workspace routing discovery timed out",
    "workspace routing is unavailable",
    "workspace routing unavailable",
)


@dataclass(frozen=True)
class CodexInferenceReadiness:
    ready: bool
    auth_mode: str
    workspace_routing_required: bool
    workspace_routing_ready: bool
    provider_id: str | None = None
    account_type: str | None = None
    requirements_loaded: bool = False
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodexInfrastructureUnavailable(RuntimeError):
    """Raised before CFW starts when App Server inference readiness is absent."""

    def __init__(self, readiness: CodexInferenceReadiness, *,
                 message: str = "Codex App Server inference backend is unavailable") -> None:
        self.readiness = readiness
        self.kind = readiness.failure_class or "inference_backend_unavailable"
        super().__init__(message)


def _failure_class(error: BaseException) -> str:
    message = str(error).lower()
    if any(phrase in message for phrase in _WORKSPACE_ROUTING_ERRORS):
        return "workspace_routing_unavailable"
    if "timed out" in message or "timeout" in message:
        return "inference_readiness_timeout"
    if "method not found" in message or "unknown method" in message:
        return "inference_readiness_unsupported"
    return "inference_readiness_failed"


def _safe_provider(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
        return value
    return None


async def inspect_inference_readiness(
        client: _AppServerClient, profile: CodexLaunchProfile | None = None, *,
        timeout: float = 15.0) -> CodexInferenceReadiness:
    """Read auth and managed requirements before any cognitive task startup.

    Saved ChatGPT workspaces are allowed to fail closed when routing discovery
    fails. Explicit API-key and external-auth profiles do not require workspace
    routing; they still require both App Server readiness reads to succeed.
    """
    auth_mode = profile.auth_mode if profile is not None else "detected"
    routing_required = auth_mode == "chatgpt"
    try:
        account_result = await client.request("account/read", {}, timeout=timeout)
        if not isinstance(account_result, Mapping):
            raise TypeError("account/read returned an invalid response")
        requirements_result = await client.request(
            "configRequirements/read", None, timeout=timeout)
        if not isinstance(requirements_result, Mapping):
            raise TypeError("configRequirements/read returned an invalid response")
    except Exception as exc:  # noqa: BLE001 - classify sanitized upstream readiness failures
        failure_class = _failure_class(exc)
        readiness = CodexInferenceReadiness(
            ready=False, auth_mode=auth_mode,
            workspace_routing_required=routing_required,
            workspace_routing_ready=False,
            provider_id=profile.provider_id if profile is not None else None,
            failure_class=failure_class)
        raise CodexInfrastructureUnavailable(readiness) from None

    account = account_result.get("account")
    account_type = _safe_provider(
        account.get("type") if isinstance(account, Mapping) else None)
    workspace_routing = account_result.get("workspaceRouting")
    workspace_routing_required = bool(
        profile is not None and profile.auth_mode == "chatgpt"
        and account_type == "chatgpt" and workspace_routing is not None)
    if workspace_routing is not None:
        origin = (workspace_routing.get("backendOrigin")
                  if isinstance(workspace_routing, Mapping) else None)
        parsed_origin = urlsplit(origin) if isinstance(origin, str) else None
        valid_routing = bool(
            isinstance(workspace_routing, Mapping)
            and isinstance(workspace_routing.get("chatgptAccountId"), str)
            and workspace_routing.get("chatgptAccountId")
            and parsed_origin is not None
            and parsed_origin.scheme == "https" and parsed_origin.hostname
            and parsed_origin.username is None and parsed_origin.password is None
            and workspace_routing.get("accountRoutingOverride") in {
                "us", "us_cr", "NO_CONSTRAINT"})
        if not valid_routing:
            readiness = CodexInferenceReadiness(
                ready=False, auth_mode=auth_mode,
                workspace_routing_required=True, workspace_routing_ready=False,
                provider_id=profile.provider_id if profile is not None else None,
                account_type=account_type, requirements_loaded=True,
                failure_class="workspace_routing_malformed")
            raise CodexInfrastructureUnavailable(readiness)
    requires_openai_auth = account_result.get("requiresOpenaiAuth")
    if (profile is not None and profile.auth_mode == "external_provider"
            and requires_openai_auth is True):
        readiness = CodexInferenceReadiness(
            ready=False, auth_mode=auth_mode,
            workspace_routing_required=False, workspace_routing_ready=True,
            provider_id=profile.provider_id, account_type=account_type,
            requirements_loaded=True, failure_class="provider_auth_mode_mismatch")
        raise CodexInfrastructureUnavailable(readiness)
    if (profile is not None and profile.auth_mode == "openai_api_key"
            and account_type not in {None, "apiKey"}):
        readiness = CodexInferenceReadiness(
            ready=False, auth_mode=auth_mode,
            workspace_routing_required=False, workspace_routing_ready=True,
            provider_id="openai", account_type=account_type,
            requirements_loaded=True, failure_class="provider_auth_mode_mismatch")
        raise CodexInfrastructureUnavailable(readiness)

    requirements = requirements_result.get("requirements")
    required_provider = (_safe_provider(requirements.get("modelProvider"))
                         if isinstance(requirements, Mapping) else None)
    expected_provider = profile.provider_id if profile is not None else None
    if (expected_provider and required_provider
            and required_provider != expected_provider):
        readiness = CodexInferenceReadiness(
            ready=False, auth_mode=auth_mode,
            workspace_routing_required=False,
            workspace_routing_ready=True, provider_id=expected_provider,
            account_type=account_type, requirements_loaded=True,
            failure_class="managed_provider_mismatch")
        raise CodexInfrastructureUnavailable(readiness)

    return CodexInferenceReadiness(
        ready=True, auth_mode=auth_mode,
        workspace_routing_required=workspace_routing_required,
        # account/read succeeds only after saved-workspace discovery is usable;
        # API and explicit external-auth providers do not require routing.
        workspace_routing_ready=True,
        provider_id=expected_provider or required_provider,
        account_type=account_type,
        requirements_loaded=True)

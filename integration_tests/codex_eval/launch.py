"""Direct Codex binary selection and inference preflight."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import tempfile
from typing import Any

from codex_app_server_adapter import JsonlAppServerClient
from codex_host.launch import (
    CodexLaunchConfigurationError,
    CodexLaunchProfile,
    load_named_secret,
    prepare_codex_home,
)
from codex_host.readiness import (
    CodexInfrastructureUnavailable,
    inspect_inference_readiness,
)
from .report import _failure_domain

DEFAULT_CODEX_BIN = (Path.home() / ".codex" / "packages" / "standalone" /
                     "releases" / "0.160.0-x86_64-unknown-linux-musl" /
                     "bin" / "codex")


def _direct_codex_binary() -> str:
    configured = os.environ.get("CODEX_APP_SERVER_BIN")
    candidate = Path(configured).expanduser() if configured else DEFAULT_CODEX_BIN
    try:
        binary = candidate.resolve(strict=True)
        releases = (Path.home() / ".codex" / "packages" / "standalone" /
                    "releases").resolve(strict=True)
    except OSError:
        raise CodexLaunchConfigurationError(
            "packaged Codex CLI binary is unavailable") from None
    if (not binary.is_file() or releases not in binary.parents
            or not os.access(binary, os.X_OK)):
        raise CodexLaunchConfigurationError(
            "CODEX_APP_SERVER_BIN must identify a packaged Codex CLI release")
    return str(binary)


def _launch_profile(args: argparse.Namespace, codex_home: Path
                    ) -> tuple[CodexLaunchProfile, dict[str, str]]:
    mode = args.auth_mode.replace("-", "_")
    environment: dict[str, str] = {}
    if mode == "external_provider":
        key_name = args.provider_env_key
        if not key_name:
            raise CodexLaunchConfigurationError(
                "--provider-env-key is required for external-provider mode")
        env_file = args.provider_env_file
        if env_file is None:
            configured_env_file = os.environ.get("CODEX_PROVIDER_ENV_FILE")
            env_file = Path(configured_env_file).expanduser() if configured_env_file else None
        key_value = None
        if env_file is not None:
            try:
                key_value = load_named_secret(Path(env_file).expanduser(), key_name)
            except CodexLaunchConfigurationError:
                # A direct environment key is the final configured source.
                # Do not include the path or any secret value in the error.
                key_value = None
        key_value = key_value or os.environ.get(key_name)
        if not key_value:
            raise CodexLaunchConfigurationError(
                f"provider credential is unavailable; pass --provider-env-file, "
                f"set CODEX_PROVIDER_ENV_FILE, or set {key_name}")
        environment[key_name] = key_value
        profile = CodexLaunchProfile(
            binary=_direct_codex_binary(), auth_mode="external_provider",
            codex_home=codex_home, provider_id=args.provider_id,
            provider_base_url=args.provider_base_url,
            provider_env_key=key_name, wire_api=args.provider_wire_api,
            secret_env_keys=(key_name,))
    elif mode == "openai_api_key":
        profile = CodexLaunchProfile(
            binary=_direct_codex_binary(), auth_mode="openai_api_key",
            codex_home=codex_home,
            secret_env_keys=("OPENAI_API_KEY",) if os.environ.get(
                "OPENAI_API_KEY") else ())
    else:
        profile = CodexLaunchProfile(
            binary=_direct_codex_binary(), auth_mode="chatgpt",
            codex_home=codex_home,
            auth_source_home=Path(args.auth_source_home).expanduser()
            if args.auth_source_home else Path.home() / ".codex")
    return profile, environment


async def _inference_preflight(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-inference-preflight-") as raw:
        root = Path(raw)
        profile, launch_environment = _launch_profile(args, root / "codex-home")
        prepare_codex_home(profile, model=args.model,
                           parent_environment=os.environ,
                           api_key=os.environ.get("OPENAI_API_KEY"))
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        client = await JsonlAppServerClient.launch(
            cwd=workspace, launch_profile=profile,
            launch_environment=launch_environment, readiness_only=True)
        readiness = None
        initialized = False
        try:
            await client.request("initialize", {
                "clientInfo": {"name": "cfw-inference-preflight",
                               "title": "Generic Codex Inference Preflight",
                               "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            initialized = True
            await client.notify("initialized")
            readiness = await inspect_inference_readiness(client, profile)
            return {
                "condition": args.condition, "trial_id": args.trial_id,
                "model": args.model, "inference_preflight_only": True,
                "host_initialized": False, "runtime_harness_lifecycle_initialized": False,
                "thread_started": False, "turn_started": False,
                "inference_ready": True,
                "inference_readiness": readiness.to_dict(),
                "workspace_routing_required": readiness.workspace_routing_required,
                "model_execution_observed": False, "efficacy_scored": False,
                "infrastructure_valid": True, "model_infrastructure_valid": False,
                "failure_domain": None,
                "failure_class": None,
            }
        except CodexInfrastructureUnavailable as exc:
            return {
                "condition": args.condition, "trial_id": args.trial_id,
                "model": args.model, "inference_preflight_only": True,
                "host_initialized": False, "runtime_harness_lifecycle_initialized": False,
                "thread_started": False, "turn_started": False,
                "inference_ready": False,
                "inference_readiness": exc.readiness.to_dict(),
                "workspace_routing_required": exc.readiness.workspace_routing_required,
                "model_execution_observed": False, "efficacy_scored": False,
                "infrastructure_valid": False,
                "model_infrastructure_valid": False,
                "failure_domain": _failure_domain(exc.kind),
                "failure_class": exc.kind,
            }
        except Exception as exc:
            return {
                "condition": args.condition, "trial_id": args.trial_id,
                "model": args.model, "inference_preflight_only": True,
                "host_initialized": False, "runtime_harness_lifecycle_initialized": False,
                "thread_started": False, "turn_started": False,
                "inference_ready": False,
                "inference_readiness": readiness.to_dict() if readiness else None,
                "model_execution_observed": False, "efficacy_scored": False,
                "infrastructure_valid": False,
                "model_infrastructure_valid": False,
                "failure_domain": "codex_app_server_inference",
                "failure_class": ("app_server_initialization_failed" if not initialized
                                  else type(exc).__name__),
            }
        finally:
            await client.close()

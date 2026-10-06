"""Explicit, credential-safe launch profiles for generic Codex App Server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

AuthMode = Literal["chatgpt", "openai_api_key", "external_provider"]
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CodexLaunchConfigurationError(ValueError):
    """A safe-to-report launch configuration or credential error."""


@dataclass(frozen=True)
class CodexLaunchProfile:
    binary: str
    auth_mode: AuthMode
    codex_home: Path
    provider_id: str | None = None
    provider_base_url: str | None = None
    provider_env_key: str | None = None
    wire_api: str = "responses"
    requires_openai_auth: bool = False
    secret_env_keys: tuple[str, ...] = ()
    auth_source_home: Path | None = None

    def __post_init__(self) -> None:
        if self.auth_mode not in {"chatgpt", "openai_api_key", "external_provider"}:
            raise CodexLaunchConfigurationError("unsupported Codex auth mode")
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise CodexLaunchConfigurationError("Codex CLI binary must be non-empty")
        if not isinstance(self.codex_home, Path):
            object.__setattr__(self, "codex_home", Path(self.codex_home))
        if self.auth_source_home is not None and not isinstance(
                self.auth_source_home, Path):
            object.__setattr__(self, "auth_source_home", Path(self.auth_source_home))
        keys = tuple(self.secret_env_keys)
        if len(keys) > 16 or any(not isinstance(key, str) or not _ENV_KEY.fullmatch(key)
                                  for key in keys):
            raise CodexLaunchConfigurationError("invalid secret environment key names")
        if len(set(keys)) != len(keys):
            raise CodexLaunchConfigurationError("secret environment key names must be unique")
        if self.auth_mode == "external_provider":
            if (not isinstance(self.provider_id, str)
                    or not _PROVIDER_ID.fullmatch(self.provider_id)):
                raise CodexLaunchConfigurationError("external provider id is invalid")
            if not isinstance(self.provider_env_key, str) or not _ENV_KEY.fullmatch(
                    self.provider_env_key):
                raise CodexLaunchConfigurationError("external provider env key is invalid")
            parsed = urlsplit(str(self.provider_base_url or ""))
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise CodexLaunchConfigurationError("external provider base URL is invalid")
            if parsed.scheme == "http" and parsed.hostname not in {
                    "127.0.0.1", "localhost", "::1"}:
                raise CodexLaunchConfigurationError(
                    "external provider HTTP URLs must use loopback")
            if self.wire_api not in {"responses", "chat_completions"}:
                raise CodexLaunchConfigurationError("external provider wire API is invalid")
            if self.requires_openai_auth:
                raise CodexLaunchConfigurationError(
                    "external auth providers must not require OpenAI auth")
            if self.provider_env_key not in keys:
                object.__setattr__(self, "secret_env_keys", (*keys, self.provider_env_key))
        elif any(value is not None for value in (
                self.provider_id, self.provider_base_url, self.provider_env_key)):
            raise CodexLaunchConfigurationError(
                "provider fields are only valid in external-provider mode")
        if self.auth_mode == "openai_api_key" and self.secret_env_keys not in (
                (), ("OPENAI_API_KEY",)):
            raise CodexLaunchConfigurationError(
                "OpenAI API-key mode only accepts OPENAI_API_KEY")


def provider_environment(profile: CodexLaunchProfile,
                         parent: Mapping[str, str]) -> dict[str, str]:
    """Copy only explicitly named credential variables; never log their values."""
    result: dict[str, str] = {}
    for key in profile.secret_env_keys:
        value = parent.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CodexLaunchConfigurationError(
                f"required provider environment variable is missing: {key}")
        if "\x00" in value:
            raise CodexLaunchConfigurationError(
                f"required provider environment variable is invalid: {key}")
        result[key] = value
    return result


def load_named_secret(path: Path, key: str) -> str:
    """Read one named env-file value without evaluating shell syntax."""
    if not _ENV_KEY.fullmatch(key):
        raise CodexLaunchConfigurationError("credential variable name is invalid")
    try:
        metadata = path.stat()
    except OSError:
        raise CodexLaunchConfigurationError(
            f"credential environment file is unavailable for {key}") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise CodexLaunchConfigurationError(
            "credential environment file must be a private regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise CodexLaunchConfigurationError(
            f"credential environment file is unreadable for {key}") from None
    found: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if not separator or name.strip() != key:
            continue
        try:
            parts = shlex.split(raw_value.strip(), comments=False, posix=True)
        except ValueError:
            raise CodexLaunchConfigurationError(
                f"credential value has invalid quoting for {key}") from None
        if len(parts) != 1 or not parts[0].strip():
            raise CodexLaunchConfigurationError(
                f"credential value is empty or invalid for {key}")
        found.append(parts[0])
    if len(found) != 1:
        raise CodexLaunchConfigurationError(
            f"credential environment file must define {key} exactly once")
    return found[0]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _base_config(model: str) -> str:
    return "\n".join((
        f"model = {_toml_string(model)}",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        "mcp_servers = {}",
        'web_search = "disabled"',
        "",
    ))


def prepare_codex_home(profile: CodexLaunchProfile, *, model: str,
                       parent_environment: Mapping[str, str],
                       api_key: str | None = None) -> None:
    """Create an isolated CODEX_HOME with no inherited config or MCP catalog."""
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise CodexLaunchConfigurationError("model name must be bounded non-empty text")
    home = profile.codex_home.expanduser().resolve()
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    if any(home.iterdir()):
        raise CodexLaunchConfigurationError(
            "temporary CODEX_HOME must be empty before preparation")

    config = _base_config(model)
    if profile.auth_mode == "external_provider":
        assert profile.provider_id is not None
        assert profile.provider_base_url is not None
        assert profile.provider_env_key is not None
        config += "\n".join((
            f"model_provider = {_toml_string(profile.provider_id)}",
            "",
            f"[model_providers.{profile.provider_id}]",
            f"name = {_toml_string(profile.provider_id)}",
            f"base_url = {_toml_string(profile.provider_base_url)}",
            f"env_key = {_toml_string(profile.provider_env_key)}",
            f"wire_api = {_toml_string(profile.wire_api)}",
            f"requires_openai_auth = {str(profile.requires_openai_auth).lower()}",
            "",
        ))
    config_path = home / "config.toml"
    descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(config)
        stream.flush()
        os.fsync(stream.fileno())

    if profile.auth_mode == "chatgpt":
        source_home = profile.auth_source_home
        if source_home is None:
            configured_home = parent_environment.get("CODEX_HOME")
            source_home = Path(configured_home).expanduser() if configured_home else (
                Path.home() / ".codex")
        source_auth = source_home.expanduser() / "auth.json"
        try:
            auth_stat = source_auth.stat()
        except OSError:
            raise CodexLaunchConfigurationError(
                "ChatGPT auth file is not available in the selected credential home") from None
        if not stat.S_ISREG(auth_stat.st_mode):
            raise CodexLaunchConfigurationError("ChatGPT auth file must be a regular file")
        target_auth = home / "auth.json"
        shutil.copyfile(source_auth, target_auth)
        os.chmod(target_auth, 0o600)
    elif profile.auth_mode == "openai_api_key":
        key = api_key or parent_environment.get("OPENAI_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise CodexLaunchConfigurationError(
                "OPENAI_API_KEY is required for temporary API-key login")
        login_env = {
            name: str(parent_environment[name])
            for name in ("PATH", "HOME", "TMPDIR", "TEMP", "LANG", "LC_ALL")
            if name in parent_environment
        }
        login_env["CODEX_HOME"] = str(home)
        completed = subprocess.run(
            [profile.binary, "login", "--with-api-key"], input=key + "\n",
            text=True, env=login_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
        if completed.returncode != 0:
            raise CodexLaunchConfigurationError(
                "Codex rejected the API key for temporary authentication")

_NATIVE_MODEL_TOOL_FEATURES = (
    # Native model-callable integrations and alternate execution surfaces.
    "agent_message_board", "apps", "auth_elicitation", "browser_use",
    "browser_use_external", "browser_use_full_cdp_access", "code_mode",
    "computer_use", "codex_apps_mcp_2026_07_28", "enable_mcp_apps",
    "exec_permission_approvals", "image_generation", "in_app_browser",
    "in_app_chat", "in_app_dictation", "in_app_local_automation",
    "in_app_updates", "multi_agent", "multi_agent_v2", "parallel",
    "plugin_sharing", "plugins", "remote_plugin", "request_permissions_tool",
    "rmcp_client", "skill_mcp_dependency_install", "skill_search", "skills",
    "sqlite", "tool_call_mcp_elicitation", "tool_suggest",
    "unified_exec_tty", "unified_exec_zsh_fork", "view_image",
    "shell_zsh_fork", "web_search_request", "worktrees",
    "write_stdin_approval",
)
# These features can start work or add persistent/local behavior without being
# a registered CFW dynamic tool. Keep App Server host infrastructure enabled.
_NATIVE_SIDE_EFFECT_FEATURES = (
    "daemon_auto_start", "goals", "hooks", "memories", "prevent_idle_sleep",
    "realtime_conversation", "shell_snapshot", "shell_snapshot_v2", "sleep_tool",
)
_NATIVE_EXEC_FEATURES = ("shell_tool", "unified_exec")
_NATIVE_WORKSPACE_FEATURES = ("workspace_dependencies",)
_HOST_ONLY_FEATURES = (set(_NATIVE_EXEC_FEATURES) | set(_NATIVE_MODEL_TOOL_FEATURES)
                       | set(_NATIVE_SIDE_EFFECT_FEATURES)
                       | set(_NATIVE_WORKSPACE_FEATURES))
NATIVE_FEATURE_CATALOG_VERSION = "codex-native-host-policy-v1"
NATIVE_FEATURE_CATALOG_SHA256 = "82b363a94a1372a9ef42cafe5b160c67f38bf2502583d34858faa97d8b4bada2"
REVIEWED_CODEX_CLI_VERSION = "codex-cli 0.160.0"
REVIEWED_CODEX_FEATURE_CATALOG_SHA256 = (
    "3310a31f8ec0d7a985fb07cf6043bc060dc5c438c4b23058e14b9b1144b86714")


def _native_feature_policy_identity() -> dict[str, str]:
    groups = {
        "model_callable_disabled": sorted(_NATIVE_MODEL_TOOL_FEATURES),
        "execution_disabled": sorted(_NATIVE_EXEC_FEATURES),
        "persistent_side_effects_disabled": sorted(_NATIVE_SIDE_EFFECT_FEATURES),
        "workspace_dependency_features_disabled": sorted(_NATIVE_WORKSPACE_FEATURES),
    }
    canonical = json.dumps({"version": NATIVE_FEATURE_CATALOG_VERSION,
                            "groups": groups}, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != NATIVE_FEATURE_CATALOG_SHA256:
        raise RuntimeError(
            "native feature policy catalog is unknown; update its version and pinned hash")
    return {"version": NATIVE_FEATURE_CATALOG_VERSION, "policy_sha256": digest}


def _native_feature_catalog_identity(supported_features: Sequence[str],
                                     cli_version: str) -> dict[str, object]:
    """Pin the installed CLI version and its complete discovered feature list."""
    policy = _native_feature_policy_identity()
    feature_names = sorted(set(supported_features))
    canonical = json.dumps({"codex_cli_version": cli_version,
                            "feature_names": feature_names}, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if (cli_version != REVIEWED_CODEX_CLI_VERSION
            or digest != REVIEWED_CODEX_FEATURE_CATALOG_SHA256):
        raise RuntimeError(
            "Codex CLI feature catalog is unknown; review and pin its version and full feature list")
    return {
        **policy,
        "codex_cli_version": cli_version,
        "feature_names": feature_names,
        "catalog_sha256": digest,
    }


def _host_only_feature_args(supported_features: set[str]) -> list[str]:
    _native_feature_policy_identity()
    native_exec = [feature for feature in _NATIVE_EXEC_FEATURES
                   if feature in supported_features]
    if not native_exec:
        raise RuntimeError(
            "Codex CLI exposes no recognized native execution feature flag; "
            "refusing to launch App Server")
    # shell_tool and unified_exec are distinct CLI generations. The CLI also
    # exposes a code_mode_host service, but that service is not the model-facing
    # tool; disabling code_mode closes that entrypoint while keeping App Server
    # infrastructure available for host-registered dynamicTools.
    disabled = list(dict.fromkeys((
        *native_exec, *(feature for feature in (
            *_NATIVE_MODEL_TOOL_FEATURES, *_NATIVE_SIDE_EFFECT_FEATURES,
            *_NATIVE_WORKSPACE_FEATURES)
                       if feature in supported_features))))
    return [argument for feature in disabled
            for argument in ("--disable", feature)]

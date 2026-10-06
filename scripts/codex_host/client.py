"""Bounded bidirectional JSON-RPC client for Codex App Server stdio."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .launch import (
    _HOST_ONLY_FEATURES,
    _NATIVE_EXEC_FEATURES,
    CodexLaunchProfile,
    _host_only_feature_args,
    _native_feature_catalog_identity,
    provider_environment,
)
from .protocol import (
    MAX_INFLIGHT_SERVER_REQUESTS,
    MAX_RPC_LINE_BYTES,
    AppServerProtocolError,
    AppServerRPCError,
    _json_bytes,
    _safe_text,
)


class JsonlAppServerClient:
    """Small bidirectional JSON-RPC client for the App Server stdio transport."""

    def __init__(self, process: asyncio.subprocess.Process, *,
                 max_line_bytes: int = MAX_RPC_LINE_BYTES,
                 host_policy_verified: bool = False,
                 supported_native_features: Sequence[str] = (),
                 disabled_native_features: Sequence[str] = (),
                 codex_feature_catalog: Mapping[str, Any] | None = None) -> None:
        if process.stdin is None or process.stdout is None:
            raise ValueError("App Server process must expose stdin and stdout")
        self._process = process
        self._max_line_bytes = max_line_bytes
        self.supported_native_features = tuple(sorted(set(supported_native_features)))
        self.disabled_native_features = tuple(sorted(set(disabled_native_features)))
        self.codex_feature_catalog = dict(codex_feature_catalog or {})
        self.host_policy_verified = bool(
            host_policy_verified
            and set(self.supported_native_features).issubset(
                set(self.disabled_native_features))
            and set(self.disabled_native_features).intersection(_NATIVE_EXEC_FEATURES))
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._server_tasks: set[asyncio.Task[Any]] = set()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._request_handler: Callable[[str, Mapping[str, Any]], Any] | None = None
        self._notification_handler: Callable[[str, Mapping[str, Any]], Any] | None = None
        self._closed = False

    @classmethod
    async def launch(cls, *, cwd: str | Path,
                     command: Sequence[str] | None = None,
                     env: Mapping[str, str] | None = None,
                     launch_profile: CodexLaunchProfile | None = None,
                     launch_environment: Mapping[str, str] | None = None,
                     pass_env: Sequence[str] = (),
                     extra_config: Sequence[str] = (),
                     readiness_only: bool = False,
                     max_line_bytes: int = MAX_RPC_LINE_BYTES) -> JsonlAppServerClient:
        """Launch App Server with all native model action tools disabled."""
        if env is None:
            parent = os.environ
            allowed = {
                "PATH", "HOME", "CODEX_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "TMPDIR", "TEMP", "LANG", "LC_ALL", "TERM",
                "NO_COLOR",
            }
            child_env = {key: parent[key] for key in allowed if key in parent}
        else:
            child_env = {str(key): str(value) for key, value in env.items()}
        if launch_profile is not None:
            supplied_secrets = dict(launch_environment or {})
            if set(supplied_secrets) - set(launch_profile.secret_env_keys):
                raise ValueError("launch_environment contains undeclared secret keys")
            if pass_env:
                raise ValueError("pass_env cannot be combined with an explicit launch profile")
            secret_source = dict(os.environ)
            secret_source.update(supplied_secrets)
            child_env.update(provider_environment(launch_profile, secret_source))
            child_env["CODEX_HOME"] = str(launch_profile.codex_home.expanduser().resolve())
        for key in pass_env:
            if key in os.environ:
                child_env[key] = os.environ[key]

        if command is None:
            app_server_bin = (launch_profile.binary if launch_profile is not None
                              else os.environ.get("CODEX_APP_SERVER_BIN", "codex"))
            launch_command = [app_server_bin, "app-server"]
        else:
            launch_command = [str(part) for part in command]
            if not launch_command:
                raise ValueError("App Server command must not be empty")
            app_server_bin = launch_command[0]

        supported_native_features: tuple[str, ...] = ()
        disabled_native_features: tuple[str, ...] = ()
        codex_feature_catalog: dict[str, Any] = {}
        if readiness_only:
            # No thread, turn, native tool, or host integration exists in this
            # mode. Avoid feature discovery because ChatGPT workspace routing
            # can block that CLI subcommand before account/read is reachable.
            launch_command.extend((
                "-c", 'mcp_servers={}',
                "-c", 'web_search="disabled"',
                "-c", 'sandbox_mode="read-only"',
                "-c", 'approval_policy="never"',
            ))
        else:
            # Feature names vary across Codex CLI releases. Discover the names
            # this installed CLI accepts, then disable every model-side action
            # surface the host does not delegate.
            feature_process = await asyncio.create_subprocess_exec(
                app_server_bin, "features", "list",
                cwd=str(Path(cwd).resolve()), env=child_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                feature_stdout, _ = await asyncio.wait_for(
                    feature_process.communicate(), timeout=15.0)
            except asyncio.TimeoutError:
                if feature_process.returncode is None:
                    try:
                        feature_process.kill()
                    except ProcessLookupError:
                        pass
                await feature_process.wait()
                raise RuntimeError("Codex CLI feature discovery timed out") from None
            if feature_process.returncode != 0:
                raise RuntimeError(
                    "Could not inspect Codex CLI features; refusing to launch App Server")
            supported_features = {
                line.split()[0] for line in feature_stdout.decode(
                    "utf-8", errors="replace").splitlines() if line.split()
            }
            cli_version = await cls._discover_cli_version(
                app_server_bin, cwd=Path(cwd), env=child_env)
            codex_feature_catalog = _native_feature_catalog_identity(
                supported_features, cli_version)
            feature_args = _host_only_feature_args(supported_features)
            disabled_native_features = tuple(
                feature_args[index + 1] for index in range(0, len(feature_args), 2))
            supported_native_features = tuple(sorted(
                supported_features & _HOST_ONLY_FEATURES))
            if not set(supported_native_features).issubset(disabled_native_features):
                raise RuntimeError(
                    "Codex CLI exposes an unclosed native action feature; refusing to launch")
            launch_command.extend(feature_args)
            launch_command.extend((
                "-c", 'mcp_servers={}',
                "-c", 'web_search="disabled"',
                "-c", 'sandbox_mode="read-only"',
                "-c", 'approval_policy="never"',
            ))
        for override in extra_config:
            if not isinstance(override, str) or "\n" in override:
                raise ValueError("App Server config overrides must be single-line strings")
            key = override.split("=", 1)[0].strip().lower()
            if key not in {"model_reasoning_effort", "model_verbosity",
                           "model_reasoning_summary", "personality"}:
                raise ValueError(
                    f"App Server override {key!r} is not a host-approved safe setting")
            launch_command.extend(("-c", override))

        process = await asyncio.create_subprocess_exec(
            *launch_command, cwd=str(Path(cwd).resolve()), env=child_env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=max_line_bytes)
        return cls(
            process, max_line_bytes=max_line_bytes,
            host_policy_verified=not readiness_only,
            supported_native_features=supported_native_features,
            disabled_native_features=disabled_native_features,
            codex_feature_catalog=codex_feature_catalog)

    @staticmethod
    async def _discover_cli_version(binary: str, *, cwd: Path,
                                    env: Mapping[str, str]) -> str:
        process = await asyncio.create_subprocess_exec(
            binary, "--version", cwd=str(cwd.resolve()), env=dict(env),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise RuntimeError("Codex CLI version discovery timed out") from None
        if process.returncode != 0:
            raise RuntimeError("Could not inspect Codex CLI version; refusing to launch App Server")
        version = stdout.decode("utf-8", errors="replace").strip().splitlines()
        if not version or len(version[0]) > 128:
            raise RuntimeError("Codex CLI version is malformed; refusing to launch App Server")
        return version[0]

    def set_handlers(self, *,
                     request_handler: Callable[[str, Mapping[str, Any]], Any],
                     notification_handler: Callable[[str, Mapping[str, Any]], Any]) -> None:
        self._request_handler = request_handler
        self._notification_handler = notification_handler

    async def _send(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise ConnectionError("Codex App Server connection is closed")
        data = _json_bytes(dict(message)) + b"\n"
        if len(data) > self._max_line_bytes:
            raise AppServerProtocolError("outbound App Server message exceeds size limit")
        async with self._write_lock:
            assert self._process.stdin is not None
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._send(message)

    async def request(self, method: str, params: Mapping[str, Any] | None = None,
                      *, timeout: float = 30.0) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            message: dict[str, Any] = {"method": method, "id": request_id}
            if params is not None:
                message["params"] = dict(params)
            await self._send(message)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    raise ConnectionError("Codex App Server closed its output stream")
                if len(line) > self._max_line_bytes:
                    raise AppServerProtocolError("inbound App Server message exceeds size limit")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AppServerProtocolError("Codex App Server sent invalid JSON") from exc
                if not isinstance(message, Mapping):
                    raise AppServerProtocolError("Codex App Server message must be an object")
                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and method is None:
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        if isinstance(message.get("error"), Mapping):
                            error = message["error"]
                            code = error.get("code", -32000)
                            detail = error.get("message", "request failed")
                            future.set_exception(AppServerRPCError(
                                code if type(code) is int else -32000,
                                detail if isinstance(detail, str) else "request failed"))
                        else:
                            future.set_result(message.get("result"))
                    continue
                if isinstance(method, str) and request_id is not None:
                    if len(self._server_tasks) >= MAX_INFLIGHT_SERVER_REQUESTS:
                        await self._send({"id": request_id, "error": {
                            "code": -32000,
                            "message": "Host server-request concurrency limit reached"}})
                        continue
                    task = asyncio.create_task(self._dispatch_server_request(
                        request_id, method, message.get("params")))
                    self._server_tasks.add(task)
                    task.add_done_callback(self._finish_server_task)
                    continue
                if isinstance(method, str) and request_id is None:
                    await self._dispatch_notification(method, message.get("params"))
                    continue
                raise AppServerProtocolError("Codex App Server message has no method or response id")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - fail every pending RPC on reader failure
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)

    def _finish_server_task(self, task: asyncio.Task[Any]) -> None:
        self._server_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _dispatch_server_request(self, request_id: int | str, method: str,
                                       raw_params: Any) -> None:
        if not isinstance(raw_params, Mapping) or self._request_handler is None:
            await self._send({"id": request_id, "error": {
                "code": -32601, "message": "Host request handler is unavailable"}})
            return
        try:
            result = self._request_handler(method, raw_params)
            if inspect.isawaitable(result):
                result = await result
            await self._send({"id": request_id, "result": result})
        except AppServerRPCError as exc:
            await self._send({"id": request_id, "error": {
                "code": exc.code, "message": _safe_text(str(exc), 256)}})
        except Exception:  # noqa: BLE001 - hide host action details from the model
            await self._send({"id": request_id, "error": {
                "code": -32603, "message": "Host tool request failed"}})

    async def _dispatch_notification(self, method: str, raw_params: Any) -> None:
        if self._notification_handler is None:
            return
        params = raw_params if isinstance(raw_params, Mapping) else {}
        result = self._notification_handler(method, params)
        if inspect.isawaitable(result):
            await result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            # The child can exit between the wait timeout and signal delivery.
            # Treat that race as a completed shutdown, then reap the process.
            if self._process.returncode is None:
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                if self._process.returncode is None:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                await self._process.wait()
        self._reader_task.cancel()
        for task in tuple(self._server_tasks):
            task.cancel()
        await asyncio.gather(self._reader_task, *tuple(self._server_tasks),
                             return_exceptions=True)

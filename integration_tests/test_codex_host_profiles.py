from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from codex_host.launch import (  # noqa: E402
    CodexLaunchProfile, load_named_secret, prepare_codex_home, provider_environment,
)
from codex_host.readiness import (  # noqa: E402
    CodexInfrastructureUnavailable, inspect_inference_readiness,
)


def test_external_profile_config_keeps_credential_value_out_of_codex_home(tmp_path):
    secret = "fixture-provider-secret-never-write-this"
    profile = CodexLaunchProfile(
        binary="/packaged/codex", auth_mode="external_provider",
        codex_home=tmp_path / "codex-home", provider_id="freeinference",
        provider_base_url="http://127.0.0.1:18769/v1",
        provider_env_key="FREEINFERENCE_API_KEY", secret_env_keys=(
            "FREEINFERENCE_API_KEY",))

    assert provider_environment(profile, {"FREEINFERENCE_API_KEY": secret}) == {
        "FREEINFERENCE_API_KEY": secret}
    prepare_codex_home(profile, model="probe-model",
                       parent_environment={"FREEINFERENCE_API_KEY": secret})

    config = (profile.codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'env_key = "FREEINFERENCE_API_KEY"' in config
    assert secret not in config
    assert secret not in repr(profile)
    assert not (profile.codex_home / "auth.json").exists()


def test_named_secret_reader_loads_only_the_requested_key(tmp_path):
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "FREEINFERENCE_API_KEY='provider-secret-value'\nIGNORED=not-selected\n",
        encoding="utf-8")
    env_file.chmod(0o600)

    assert load_named_secret(env_file, "FREEINFERENCE_API_KEY") == "provider-secret-value"


def test_external_provider_readiness_bypasses_chatgpt_routing():
    class Client:
        def __init__(self):
            self.methods = []

        async def request(self, method, params=None, *, timeout=30):
            self.methods.append((method, params))
            if method == "account/read":
                return {"account": None, "requiresOpenaiAuth": False}
            if method == "configRequirements/read":
                return {"requirements": None}
            raise AssertionError(method)

    async def run():
        profile = CodexLaunchProfile(
            binary="/packaged/codex", auth_mode="external_provider",
            codex_home=Path("/tmp/codex-test-home"), provider_id="freeinference",
            provider_base_url="http://127.0.0.1:18769/v1",
            provider_env_key="FREEINFERENCE_API_KEY")
        client = Client()
        result = await inspect_inference_readiness(client, profile)
        assert result.ready is True
        assert result.workspace_routing_required is False
        assert [item[0] for item in client.methods] == [
            "account/read", "configRequirements/read"]

    asyncio.run(run())


def test_chatgpt_readiness_accepts_unselected_and_resolved_workspace_states():
    class Client:
        def __init__(self, workspace_routing):
            self.workspace_routing = workspace_routing

        async def request(self, method, params=None, *, timeout=30):
            if method == "account/read":
                return {"account": {"type": "chatgpt"},
                        "workspaceRouting": self.workspace_routing}
            if method == "configRequirements/read":
                return {"requirements": None}
            raise AssertionError(method)

    async def run():
        profile = CodexLaunchProfile(
            binary="/packaged/codex", auth_mode="chatgpt",
            codex_home=Path("/tmp/codex-test-home"))
        unselected = await inspect_inference_readiness(Client(None), profile)
        assert unselected.ready is True
        assert unselected.workspace_routing_required is False

        selected = await inspect_inference_readiness(Client({
            "chatgptAccountId": "workspace-1",
            "backendOrigin": "https://chatgpt.com/backend-api/codex",
            "accountRoutingOverride": "NO_CONSTRAINT",
        }), profile)
        assert selected.ready is True
        assert selected.workspace_routing_required is True

    asyncio.run(run())


def test_chatgpt_readiness_rejects_malformed_selected_workspace_route():
    class Client:
        async def request(self, method, params=None, *, timeout=30):
            if method == "account/read":
                return {"account": {"type": "chatgpt"},
                        "workspaceRouting": {"backendOrigin": "http://invalid"}}
            if method == "configRequirements/read":
                return {"requirements": None}
            raise AssertionError(method)

    async def run():
        profile = CodexLaunchProfile(
            binary="/packaged/codex", auth_mode="chatgpt",
            codex_home=Path("/tmp/codex-test-home"))
        with pytest.raises(CodexInfrastructureUnavailable) as captured:
            await inspect_inference_readiness(Client(), profile)
        assert captured.value.kind == "workspace_routing_malformed"

    asyncio.run(run())


def test_workspace_routing_failure_is_classified_as_infrastructure():
    class Client:
        async def request(self, method, params=None, *, timeout=30):
            raise RuntimeError("workspace routing discovery failed")

    async def run():
        profile = CodexLaunchProfile(
            binary="/packaged/codex", auth_mode="chatgpt",
            codex_home=Path("/tmp/codex-test-home"))
        with pytest.raises(CodexInfrastructureUnavailable) as captured:
            await inspect_inference_readiness(Client(), profile)
        assert captured.value.kind == "workspace_routing_unavailable"
        assert captured.value.readiness.ready is False
        assert captured.value.readiness.workspace_routing_required is True

    asyncio.run(run())

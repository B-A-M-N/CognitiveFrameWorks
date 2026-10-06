from __future__ import annotations

from pathlib import Path

from resolver.api import resolve
from runtime_contract.statework_provider import (
    EmptyStateWorkProvider, statework_provider,
)


def test_resolver_and_provider_are_cfw_only_when_statework_registry_is_absent(
        tmp_path: Path, monkeypatch):
    monkeypatch.delenv("COGNITIVE_STATEWORK_ROOT", raising=False)
    missing = tmp_path / "no-statework-provider"
    provider = statework_provider(missing)
    assert isinstance(provider, EmptyStateWorkProvider)
    assert provider.load_stateworks() == []
    bundle = resolve("standalone-no-csw", "quick", (), cow_root=missing)
    assert bundle.stateworks == ()
    assert bundle.kernel == ("sispis",)

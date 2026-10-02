"""Host boundary for optional StateWork capabilities.

CFW owns composition policy. An installed provider supplies registry data and
the small set of validated runtime primitives; CFW does not import CSW's
implementation modules directly.
"""
from __future__ import annotations

import os
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol


_PROVIDER_GROUP = "cognitiveframeworks.statework_providers"


class StateWorkProviderError(ValueError):
    """A configured StateWork provider is present but invalid."""


def _provider_entrypoint() -> Optional[EntryPoint]:
    matches = [item for item in entry_points(group=_PROVIDER_GROUP)
               if item.name == "csw"]
    if len(matches) > 1:
        raise StateWorkProviderError(
            "multiple CognitiveStateWork provider plugins are installed")
    return matches[0] if matches else None


def default_statework_root() -> Path:
    """Resolve an explicitly configured or installed optional provider root."""
    cfw_root = Path(__file__).resolve().parents[2]
    bundled = cfw_root / "statework"
    configured = os.environ.get("COGNITIVE_STATEWORK_ROOT")
    if configured:
        return Path(configured)
    if (bundled / "registry.yaml").is_file():
        return bundled
    plugin = _provider_entrypoint()
    if plugin is not None:
        provider_type = plugin.load()
        default_root = getattr(provider_type, "default_root", None)
        if callable(default_root):
            return Path(default_root())
    # A sibling checkout alone does not activate CSW. The empty bundled path
    # keeps CFW usable on its own; integrated hosts install the CSW provider.
    return bundled


DEFAULT_STATEWORK_ROOT = default_statework_root()


class StateWorkProvider(Protocol):
    """Validated StateWork data and runtime operations exposed to the host."""

    root: Path

    def load_stateworks(self) -> List[Dict[str, Any]]: ...
    def packet_registry(self) -> Dict[str, Any]: ...
    def packet_store(self, packets: Optional[Iterable[Dict[str, Any]]]) -> Any: ...
    def bundle_root(self, statework: Any) -> Path: ...
    def validate_flow_contracts(self, statework_id: str, *,
                                owner_root: Optional[Path] = None) -> None: ...
    def flow_metadata(self, statework: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]: ...
    def flow_selector(self) -> Any: ...
    def runtime_hash_paths(self) -> Optional[List[Path]]: ...
    def behavior_hash_paths(self) -> List[Path]: ...
    def registry_document(self) -> Dict[str, Any]: ...
    def control_plane(self) -> Any: ...


class EmptyStateWorkProvider:
    """Provider used when the optional StateWork registry is absent."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def load_stateworks(self) -> List[Dict[str, Any]]:
        return []

    def packet_registry(self) -> Dict[str, Any]:
        return {}

    def packet_store(self, packets: Optional[Iterable[Dict[str, Any]]]) -> Any:
        raise StateWorkProviderError("StateWork packet store is unavailable without a provider")

    def bundle_root(self, statework: Any) -> Path:
        raise StateWorkProviderError("StateWork bundle is unavailable without a provider")

    def validate_flow_contracts(self, statework_id: str, *,
                                owner_root: Optional[Path] = None) -> None:
        return None

    def flow_metadata(self, statework: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {}

    def flow_selector(self) -> Any:
        raise StateWorkProviderError("StateWork flow selector is unavailable without a provider")

    def runtime_hash_paths(self) -> Optional[List[Path]]:
        return None

    def behavior_hash_paths(self) -> List[Path]:
        return []

    def registry_document(self) -> Dict[str, Any]:
        return {"version": "1.0.0", "stateworks": []}

    def control_plane(self) -> Any:
        raise StateWorkProviderError("StateWork control plane is unavailable without a provider")


@lru_cache(maxsize=8)
def _provider_for_root(resolved: Path) -> StateWorkProvider:
    plugin = _provider_entrypoint()
    if plugin is None:
        raise StateWorkProviderError(
            "CognitiveStateWork is configured but its optional provider plugin is not installed")
    try:
        provider_type = plugin.load()
        return provider_type(resolved)
    except StateWorkProviderError:
        raise
    except Exception as exc:
        raise StateWorkProviderError(
            f"CognitiveStateWork provider plugin could not be loaded: {exc}") from exc


def statework_provider(root: Path = DEFAULT_STATEWORK_ROOT) -> StateWorkProvider:
    """Select the provider for an explicit root; invalid present roots fail closed."""
    resolved = Path(root).resolve()
    if not (resolved / "registry.yaml").is_file():
        return EmptyStateWorkProvider(resolved)
    return _provider_for_root(resolved)

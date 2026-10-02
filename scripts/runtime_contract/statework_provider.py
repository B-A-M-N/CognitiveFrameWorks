"""Host boundary for optional StateWork capabilities.

CFW owns composition policy; a provider supplies registry data and the small
set of validated runtime primitives needed to compose it. Importing this
module never imports a StateWork checkout.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol


def default_statework_root() -> Path:
    """Resolve the conventional optional checkout location without loading it."""
    cfw_root = Path(__file__).resolve().parents[2]
    bundled = cfw_root / "statework"
    sibling = cfw_root.parent / "CognitiveStateWork"
    configured = os.environ.get("COGNITIVE_STATEWORK_ROOT")
    if configured:
        return Path(configured)
    return bundled if bundled.exists() else sibling


DEFAULT_STATEWORK_ROOT = default_statework_root()


class StateWorkProviderError(ValueError):
    """A configured StateWork provider is present but invalid."""


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


@lru_cache(maxsize=8)
def _provider_for_root(resolved: Path) -> StateWorkProvider:
    from runtime_contract.statework_csw_adapter import CognitiveStateWorkProvider
    return CognitiveStateWorkProvider(resolved)


def statework_provider(root: Path = DEFAULT_STATEWORK_ROOT) -> StateWorkProvider:
    """Select the provider for an explicit root; invalid present roots fail closed."""
    resolved = Path(root).resolve()
    if not (resolved / "registry.yaml").is_file():
        return EmptyStateWorkProvider(resolved)
    return _provider_for_root(resolved)

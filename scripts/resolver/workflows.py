"""Compatibility facade for the focused StateWork resolver modules."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from runtime_contract.statework_provider import DEFAULT_STATEWORK_ROOT, statework_provider
from resolver.errors import PacketDependencyCycle, PacketDependencyError, PacketValidationError
from resolver.packet_planning import (
    _consumed_inputs,
    _load_cow_packet_store,
    plan_statework_chain,
    plan_statework_dag,
)
from resolver.statework_matching import (
    match_statework,
    match_stateworks,
)
from resolver.statework_runtime import (
    _flow_selector,
    _statework_flow_metadata,
    _statework_root,
    _statework_runtime_pins,
    _validate_statework_flow_contracts,
    enumerate_eligible_routes,
    read_manifest_hash,
    select_statework_runtime,
)


def load_stateworks(cow_root: Path = DEFAULT_STATEWORK_ROOT) -> List[Dict[str, Any]]:
    """Load the provider's normalized StateWork registry."""
    try:
        return statework_provider(cow_root).load_stateworks()
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc


def parse_packet_registry(cow_root: Path = DEFAULT_STATEWORK_ROOT) -> Dict[str, Any]:
    """Load the provider's packet producer registry."""
    try:
        return statework_provider(cow_root).packet_registry()
    except Exception as exc:
        raise PacketDependencyError(str(exc)) from exc

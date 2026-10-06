"""Separated CFW policy and whole-runtime identity contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime_contract.policy import freeze_contracts, stable_hash


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class CFWPolicySnapshot:
    """Hashable CFW-owned component and enforcement policy."""

    contract: Mapping[str, Any]
    policy_hash: str = ""

    def __post_init__(self) -> None:
        frozen = freeze_contracts(self.contract)
        required = {
            "framework_version", "component_catalog_hash", "startup_preset",
            "components", "component_policy_hash", "hard_guards",
            "action_contract_hash", "capsule_hashes", "runtime_contract_hashes",
        }
        if not required.issubset(frozen):
            raise ValueError("CFW policy snapshot is missing required contract fields")
        computed = stable_hash(_plain(frozen))
        if self.policy_hash and self.policy_hash != computed:
            raise ValueError("CFW policy snapshot hash does not match its contract")
        object.__setattr__(self, "contract", frozen)
        object.__setattr__(self, "policy_hash", computed)

    def to_dict(self) -> dict[str, Any]:
        return {**_plain(self.contract), "cfw_policy_hash": self.policy_hash}


@dataclass(frozen=True)
class CompositeCognitiveRuntimeSnapshot:
    """Host-level identity spanning CFW, workflow planning, and task context."""

    cfw_policy_hash: str
    csw_registry_hash: str
    csw_workflow_plan_hash: str
    host_goal_contract_hash: str
    model_harness_toolset_hash: str
    cfw_task_behavior_contract_hash: str = ""
    dp_policy_contract_hash: str = ""
    laya_identity_hash: str = ""
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        required_hashes = ("cfw_policy_hash", "model_harness_toolset_hash")
        for name in required_hashes:
            value = getattr(self, name)
            if (not isinstance(value, str) or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)):
                raise ValueError(f"composite snapshot {name} must be a SHA-256 digest")
        for name in ("csw_registry_hash", "csw_workflow_plan_hash",
                     "host_goal_contract_hash", "cfw_task_behavior_contract_hash",
                     "dp_policy_contract_hash",
                     "laya_identity_hash"):
            value = getattr(self, name)
            if (not isinstance(value, str) or (value and (len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)))):
                raise ValueError(f"composite snapshot {name} must be empty or a SHA-256 digest")
        computed = stable_hash(self.canonical())
        if self.snapshot_hash and self.snapshot_hash != computed:
            raise ValueError("composite snapshot hash does not match its inputs")
        object.__setattr__(self, "snapshot_hash", computed)

    def canonical(self) -> dict[str, str]:
        return {
            "cfw_policy_hash": self.cfw_policy_hash,
            "csw_registry_hash": self.csw_registry_hash,
            "csw_workflow_plan_hash": self.csw_workflow_plan_hash,
            "cfw_task_behavior_contract_hash": self.cfw_task_behavior_contract_hash,
            "dp_policy_contract_hash": self.dp_policy_contract_hash,
            "laya_identity_hash": self.laya_identity_hash,
            "host_goal_contract_hash": self.host_goal_contract_hash,
            "model_harness_toolset_hash": self.model_harness_toolset_hash,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self.canonical(), "snapshot_hash": self.snapshot_hash}

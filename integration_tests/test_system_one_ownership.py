from __future__ import annotations

from dataclasses import fields

import pytest

from runtime_contract.task import TaskRequest
from runtime_start.validation import validate_start_contracts


@pytest.mark.parametrize(
    "runtime_mode",
    ("standalone_advisory", "standalone_enforcing", "combined_enforcing"),
)
def test_cfw_rejects_system_one_provider_in_every_mode(runtime_mode):
    with pytest.raises(ValueError, match="CFW no longer accepts a System-1 provider"):
        validate_start_contracts(
            None,
            runtime_mode=runtime_mode,
            behavioral_judge=object(),
            policy_epoch_record=None,
            action_registry=None,
        )


def test_cfw_task_request_has_no_provider_policy_field():
    assert "system_one_policy" not in {item.name for item in fields(TaskRequest)}
    with pytest.raises(TypeError, match="system_one_policy"):
        TaskRequest(task_id="provider-policy", application_id="test",
                    shape="implement", system_one_policy=object())

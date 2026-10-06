from __future__ import annotations

import pytest

from runtime_session.telemetry import HostTelemetry


def _telemetry(tmp_path):
    return HostTelemetry(
        "bounded-telemetry", tmp_path / "events.ndjson",
        namespace_id="tenant-a", application_id="test-app",
        application_instance_id="test-app-1", session_id="session-1",
    )


def test_runtime_telemetry_uses_cfw_neutral_contract(tmp_path):
    telemetry = _telemetry(tmp_path)

    telemetry.emit_event(category="decision", event_type="policy_composed",
                         payload={"summary": "bounded metadata"})

    assert telemetry.schema_version == "1.0.0"
    assert '"schema_version":"1.0.0"' in telemetry.store.read_text()


def test_runtime_telemetry_rejects_deep_metadata(tmp_path):
    payload = {}
    for _ in range(10):
        payload = {"nested": payload}

    with pytest.raises(ValueError, match="nesting bound"):
        _telemetry(tmp_path).emit_event(
            category="decision", event_type="bounded_metadata", payload=payload)


def test_runtime_telemetry_rejects_oversized_event(tmp_path):
    payload = {f"field-{index}": "x" * 4096 for index in range(20)}

    with pytest.raises(ValueError, match="64 KiB bound"):
        _telemetry(tmp_path).emit_event(
            category="decision", event_type="oversized_metadata", payload=payload)

import json
from pathlib import Path

from runtime_contract.telemetry import DurableTelemetryExporter


def test_exporter_is_idempotent_and_replays_after_failure(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text("\n".join(json.dumps({"event_id": f"e{i}"}) for i in range(3)) + "\n")
    delivered = []
    exporter = DurableTelemetryExporter(source, delivered.extend, state_dir=tmp_path / "state", batch_size=2)
    assert exporter.flush() == 2
    assert exporter.flush() == 1
    assert exporter.flush() == 0
    assert [item["event_id"] for item in delivered] == ["e0", "e1", "e2"]

    source.write_text(source.read_text() + json.dumps({"event_id": "e3"}) + "\n")
    exporter2 = DurableTelemetryExporter(source, delivered.extend, state_dir=tmp_path / "state", batch_size=2)
    assert exporter2.flush() == 1
    assert delivered[-1]["event_id"] == "e3"

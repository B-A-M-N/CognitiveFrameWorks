import json
from pathlib import Path

import pytest

from runtime_contract.telemetry import DurableTelemetryExporter


def test_exporter_is_idempotent_and_replays_after_failure(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text("\n".join(json.dumps({"event_id": f"e{i}"}) for i in range(3)) + "\n")
    delivered = []
    def sink(batch):
        delivered.extend(item["event_id"] for item in batch)
        return {item["event_id"] for item in batch}
    exporter = DurableTelemetryExporter(source, sink, state_dir=tmp_path / "state", batch_size=2)
    assert exporter.flush() == 2
    assert exporter.flush() == 1
    assert exporter.flush() == 0
    assert delivered == ["e0", "e1", "e2"]

    with source.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event_id": "e3"}) + "\n")
    exporter2 = DurableTelemetryExporter(source, sink, state_dir=tmp_path / "state", batch_size=2)
    assert exporter2.flush() == 1
    assert delivered[-1] == "e3"


def test_partial_trailing_line_is_not_skipped(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text(json.dumps({"event_id": "e0"}) + "\n" + '{"event_id":"e1"')
    acknowledged = []
    exporter = DurableTelemetryExporter(
        source, lambda batch: acknowledged.extend(item["event_id"] for item in batch) or {item["event_id"] for item in batch},
        state_dir=tmp_path / "state")
    assert exporter.flush() == 1
    with source.open("a", encoding="utf-8") as stream:
        stream.write("}\n")
    assert exporter.flush() == 1
    assert acknowledged == ["e0", "e1"]


def test_explicit_acknowledgement_is_required(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text(json.dumps({"event_id": "e0"}) + "\n")
    exporter = DurableTelemetryExporter(source, lambda batch: None, state_dir=tmp_path / "state")
    with pytest.raises(ValueError, match="explicitly acknowledge"):
        exporter.flush()


def test_delivered_id_store_is_bounded_and_offset_is_durable(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text("\n".join(json.dumps({"event_id": f"e{i}"}) for i in range(5)) + "\n")
    exporter = DurableTelemetryExporter(
        source,
        lambda batch: {item["event_id"] for item in batch},
        state_dir=tmp_path / "state", batch_size=1, max_delivered_ids=2)
    for _ in range(5):
        exporter.flush()
    assert len(exporter._delivered_ids()) == 2
    cursor = json.loads(exporter.cursor_path.read_text())
    assert cursor["byte_offset"] == source.stat().st_size
    assert cursor["last_event_id"] == "e4"


def test_exporter_does_not_wait_indefinitely_for_a_contended_sink(tmp_path):
    import fcntl
    source = tmp_path / "telemetry.ndjson"
    source.write_text(json.dumps({"event_id": "e0", "namespace_id": "n",
                                  "application_instance_id": "i", "agent_instance_id": "a"}) + "\n")
    exporter = DurableTelemetryExporter(
        source, lambda batch: {item["event_id"] for item in batch},
        state_dir=tmp_path / "state", batch_size=1)
    with exporter.lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # Non-blocking lock acquisition makes backpressure explicit instead
        # of letting a noisy producer monopolize the exporter.
        try:
            assert exporter.flush() == 0
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def test_exporter_enforces_per_origin_batch_budget(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    source.write_text("\n".join(json.dumps({
        "event_id": f"e{i}", "namespace_id": "n", "application_instance_id": "i",
        "agent_instance_id": "a"}) for i in range(4)) + "\n")
    exporter = DurableTelemetryExporter(
        source, lambda batch: {item["event_id"] for item in batch},
        state_dir=tmp_path / "state", batch_size=4,
        origin_budget={"n|i|a": 2})
    assert exporter.flush() == 0


def test_slow_sink_returns_within_flush_deadline(tmp_path):
    import threading
    source = tmp_path / "telemetry.ndjson"
    source.write_text(json.dumps({"event_id": "e0"}) + "\n")
    release = threading.Event()
    exporter = DurableTelemetryExporter(
        source, lambda batch: (release.wait(2), {"e0"})[1],
        state_dir=tmp_path / "state", batch_size=1)
    started = __import__("time").monotonic()
    assert exporter.flush() == 0
    assert __import__("time").monotonic() - started < 0.5
    release.set()

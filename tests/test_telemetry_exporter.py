import json
import time
from pathlib import Path

import pytest

from runtime_contract.telemetry import DurableTelemetryExporter, event_key


def key(value):
    return event_key(value)


def wait_close(exporter):
    exporter.close(timeout=3)


def test_exporter_is_idempotent_and_replays_after_failure(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    values = [{"event_id": f"e{i}", "namespace_id": "n", "application_instance_id": "i",
               "agent_instance_id": "a"} for i in range(3)]
    source.write_text("\n".join(json.dumps(value) for value in values) + "\n")
    delivered = []
    def sink(batch):
        delivered.extend(key(value) for value in batch)
        return {key(value) for value in batch}
    exporter = DurableTelemetryExporter(source, sink, state_dir=tmp_path / "state", batch_size=2)
    exporter.enqueue(); time.sleep(0.1); exporter.enqueue(); time.sleep(0.1)
    wait_close(exporter)
    assert delivered == [key(value) for value in values]
    with source.open("a", encoding="utf-8") as stream:
        extra = {"event_id": "e3", "namespace_id": "n", "application_instance_id": "i",
                 "agent_instance_id": "a"}
        stream.write(json.dumps(extra) + "\n")
    exporter2 = DurableTelemetryExporter(source, sink, state_dir=tmp_path / "state", batch_size=2)
    exporter2.enqueue(); time.sleep(0.1); wait_close(exporter2)
    assert delivered[-1] == key(extra)


def test_identical_event_ids_from_two_origins_are_distinct(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    left = {"event_id": "same", "namespace_id": "n1", "application_instance_id": "i1",
            "agent_instance_id": "a"}
    right = {"event_id": "same", "namespace_id": "n2", "application_instance_id": "i2",
             "agent_instance_id": "b"}
    source.write_text("\n".join(json.dumps(value) for value in (left, right)) + "\n")
    acked = []
    def sink(batch):
        acked.extend(key(value) for value in batch)
        return set(acked)
    exporter = DurableTelemetryExporter(source, sink, state_dir=tmp_path / "state", batch_size=2)
    exporter.enqueue(); time.sleep(0.1); wait_close(exporter)
    assert acked == [key(left), key(right)]


def test_partial_trailing_line_is_not_skipped(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    first = {"event_id": "e0", "namespace_id": "n", "application_instance_id": "i", "agent_instance_id": "a"}
    source.write_text(json.dumps(first) + "\n" + '{"event_id":"e1"')
    acknowledged = []
    exporter = DurableTelemetryExporter(
        source, lambda batch: acknowledged.extend(key(v) for v in batch) or {key(v) for v in batch},
        state_dir=tmp_path / "state")
    exporter.enqueue(); time.sleep(0.1); wait_close(exporter)
    with source.open("a", encoding="utf-8") as stream:
        stream.write("}\n")
    exporter2 = DurableTelemetryExporter(
        source, lambda batch: acknowledged.extend(key(v) for v in batch) or {key(v) for v in batch},
        state_dir=tmp_path / "state")
    exporter2.enqueue(); time.sleep(0.1); wait_close(exporter2)
    assert len(acknowledged) == 2


def test_acknowledgement_must_be_exact_event_keys(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    value = {"event_id": "e0", "namespace_id": "n", "application_instance_id": "i", "agent_instance_id": "a"}
    source.write_text(json.dumps(value) + "\n")
    exporter = DurableTelemetryExporter(source, lambda batch: {"e0"}, state_dir=tmp_path / "state")
    exporter.enqueue(); time.sleep(0.15); wait_close(exporter)
    cursor = exporter._cursor()
    assert not cursor["byte_offset"]


def test_delivered_key_store_is_bounded_and_cursor_durable(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    values = [{"event_id": f"e{i}", "namespace_id": "n", "application_instance_id": "i",
               "agent_instance_id": "a"} for i in range(5)]
    source.write_text("\n".join(json.dumps(value) for value in values) + "\n")
    exporter = DurableTelemetryExporter(
        source, lambda batch: {key(value) for value in batch}, state_dir=tmp_path / "state",
        batch_size=1, max_delivered_ids=2)
    for _ in values:
        exporter.enqueue(); time.sleep(0.08)
    wait_close(exporter)
    assert len(exporter._delivered_keys()) == 2
    cursor = json.loads(exporter.cursor_path.read_text())
    assert cursor["byte_offset"] == source.stat().st_size


def test_slow_sink_does_not_block_enqueue(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    value = {"event_id": "e0", "namespace_id": "n", "application_instance_id": "i", "agent_instance_id": "a"}
    source.write_text(json.dumps(value) + "\n")
    exporter = DurableTelemetryExporter(source, lambda batch: (_ for _ in ()).throw(TimeoutError()), state_dir=tmp_path / "state")
    started = time.monotonic(); assert exporter.enqueue(); assert time.monotonic() - started < 0.1
    time.sleep(0.1); wait_close(exporter)
    assert not exporter._delivered_keys()


def test_over_budget_origin_does_not_block_other_origin(tmp_path):
    source = tmp_path / "telemetry.ndjson"
    values = [{"event_id": f"a{i}", "namespace_id": "n1", "application_instance_id": "i",
               "agent_instance_id": "noisy"} for i in range(4)]
    values.append({"event_id": "b0", "namespace_id": "n2", "application_instance_id": "i",
                   "agent_instance_id": "healthy"})
    source.write_text("\n".join(json.dumps(value) for value in values) + "\n")
    delivered = []
    exporter = DurableTelemetryExporter(
        source, lambda batch: delivered.extend(key(v) for v in batch) or {key(v) for v in batch},
        state_dir=tmp_path / "state", batch_size=2, origin_budget={"n1|i|noisy": 1})
    exporter.enqueue(); time.sleep(0.15); wait_close(exporter)
    assert key(values[-1]) in delivered

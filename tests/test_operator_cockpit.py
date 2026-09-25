import json
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from operator_service import OperatorService
from operator_ui import browser
from runtime_contract.operator_events import OperatorEvent
from runtime_contract.operator_readmodel import ResyncRequired


def test_readonly_snapshot_and_event_stream(tmp_path):
    service=OperatorService(state_path=tmp_path/"operator.sqlite3")
    event=OperatorEvent.create(sequence=1,namespace_id="n",application_instance_id="i",event_type="state_blocked",summary="Fresh evidence required",source_system="cfw",correlation_id="t:1",severity="warning",reason_code="freshness")
    assert service.publish_runtime_event(event); assert service.wait_for_sequence(1)
    snapshot=service.snapshot()
    assert snapshot["high_watermark"]==1 and snapshot["events"][0]["summary"]=="Fresh evidence required"
    assert "token" not in json.dumps(snapshot).lower()
    with service.stream(0) as stream: assert next(stream)["sequence"]==1
    service.close()


def test_stream_requires_resync_after_retention_gap(tmp_path):
    service=OperatorService(state_path=tmp_path/"operator.sqlite3",)
    for seq in (1,2,3):
        service.publish_runtime_event(OperatorEvent.create(sequence=seq,namespace_id="n",application_instance_id="i",event_type="tick",summary="tick",source_system="cfw",correlation_id="c"))
    assert service.wait_for_sequence(3)
    service.read_model.retention=1
    service.close()
    # Reopen and publish after old retention is pruned.
    service=OperatorService(state_path=tmp_path/"operator.sqlite3"); service.read_model.retention=1
    for seq in (4,5):
        service.publish_runtime_event(OperatorEvent.create(sequence=seq,namespace_id="n",application_instance_id="i",event_type="tick",summary="tick",source_system="cfw",correlation_id="c"))
    assert service.wait_for_sequence(5)
    try: next(service.stream(0))
    except ResyncRequired: pass
    else: raise AssertionError("expired operator timeline did not request resync")
    service.close()


def test_browser_surface_is_local_only_and_sanitized(tmp_path):
    service=OperatorService(state_path=tmp_path/"operator.sqlite3")
    try:
        browser.run(service,host="0.0.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("operator browser allowed non-local bind")
    handler=browser.make_handler(service)
    assert handler.__name__ == "Handler"
    assert not any(name in dir(service) for name in ("session_token","authority_key","private_key"))

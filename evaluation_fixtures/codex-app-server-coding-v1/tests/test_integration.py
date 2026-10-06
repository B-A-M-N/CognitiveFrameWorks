from src.repair import MemoryIndex, apply_event_stream


def test_event_stream_updates_and_deletes_in_order():
    index = MemoryIndex({"keep": "stable", "stale": "old"})
    result = apply_event_stream(index, ["DELETE stale", "fresh=https://x.test/?a=b"])
    assert result is index
    assert index.rows == {"keep": "stable", "fresh": "https://x.test/?a=b"}

from src.repair import MemoryIndex


def test_remove_existing_key_reports_and_applies_removal():
    index = MemoryIndex({"keep": "one", "remove": "two"})
    assert index.remove("remove") is True
    assert index.rows == {"keep": "one"}


def test_remove_missing_key_does_not_mutate_other_rows():
    index = MemoryIndex({"keep": "one", "later": "two"})
    assert index.remove("missing") is False
    assert index.rows == {"keep": "one", "later": "two"}

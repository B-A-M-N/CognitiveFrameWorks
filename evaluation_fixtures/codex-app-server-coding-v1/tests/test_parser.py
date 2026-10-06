from src.repair import parse_record


def test_parser_preserves_equals_inside_value():
    assert parse_record("endpoint=https://example.test/a?x=1") == (
        "endpoint", "https://example.test/a?x=1")


def test_parser_trims_outer_whitespace():
    assert parse_record("  mode = ready  ") == ("mode", "ready")

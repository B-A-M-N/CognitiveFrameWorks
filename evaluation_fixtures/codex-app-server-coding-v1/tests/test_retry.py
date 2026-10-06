import pytest

from src.repair import retry_delay


@pytest.mark.parametrize("attempt,expected", [
    (0, 0.25), (1, 0.5), (2, 1.0), (3, 2.0), (8, 2.0),
])
def test_retry_delay_starts_at_base_and_caps(attempt, expected):
    assert retry_delay(attempt) == expected


def test_retry_delay_rejects_negative_attempt():
    with pytest.raises(ValueError):
        retry_delay(-1)

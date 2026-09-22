from __future__ import annotations

from datetime import timedelta

import pytest

from artefaktum.cli.durations import parse_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("45", timedelta(seconds=45)),
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("30d", timedelta(days=30)),
        ("2w", timedelta(weeks=2)),
        (" 5d ", timedelta(days=5)),
    ],
)
def test_parses(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "d", "1y", "-5d", "1.5h", "5 d", "0"])
def test_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)

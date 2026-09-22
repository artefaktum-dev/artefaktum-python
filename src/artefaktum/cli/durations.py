from __future__ import annotations

import re
from datetime import timedelta

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PATTERN = re.compile(r"^(\d+)([smhdw]?)$")


def parse_duration(text: str) -> timedelta:
    """`90s`, `30m`, `12h`, `30d`, `2w`; a bare integer is seconds. Zero is rejected."""
    match = _PATTERN.match(text.strip())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"not a duration: {text!r} (use e.g. 90s, 30m, 12h, 30d, 2w)")
    return timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])

"""Pure rendering: dataclass -> JSON, plain-text tables, output-mode detection, errors.

No click import here (see `tests/test_cli_entry.py::test_no_core_sdk_module_imports_click`,
which exempts this whole package, and the design note in the task brief that this module in
particular has no click dependency and no I/O of its own) - everything below is a pure
function over values it is handed, so it is trivially testable without a CLI runner.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

import httpx

from ..errors import ArtefaktumError, MissingApiKey
from ..models import Artifact, Relation, SearchHit, Version

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_TITLE_MAX = 48

# Matches an http(s) URL's query string so it can be dropped from error messages: a
# signed URL (e.g. object storage GET/PUT targets) is a bearer credential, and
# `httpx.HTTPStatusError`/`RequestError.__str__` embed the full request URL, query
# string included. Mirrors `models._redacted_url`'s intent for the one place a URL can
# still reach a message rather than a repr.
_URL_QUERY_RE = re.compile(r'(https?://[^\s?"\']*)\?[^\s"\']*')


def _redact_url_queries(message: str) -> str:
    """Replace `?...` in every http(s) URL in `message` with `?…`, up to the next
    whitespace or quote character.
    """
    return _URL_QUERY_RE.sub("\\1?…", message)


def to_jsonable(obj: Any) -> Any:
    """Recursively turn SDK values into plain JSON-safe data.

    Reads dataclass fields by name via `dataclasses.fields`, never `repr` - some SDK
    models (`Download`, `UploadInstructions`, `CreatedKey`) redact secrets/URLs in their
    `__repr__`, but callers like `link` must still see the real value.
    """
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, frozenset | set):
        # Sorting requires the jsonable elements to be comparable; every current SDK
        # model puts only strings in sets/frozensets, so this holds in practice.
        return sorted(to_jsonable(v) for v in obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    return str(obj)


def json_mode(choice: str | None, stream: TextIO) -> bool:
    """`--json`/`--table` force a mode; otherwise JSON iff `stream` is not a TTY."""
    if choice == "json":
        return True
    if choice == "table":
        return False
    return not stream.isatty()


def human_size(n: int | None) -> str:
    """1024-based size: `"0 B"`, `"1.5 KB"`, `"5.0 MB"`; `None` (no version yet) -> `"—"`."""
    if n is None:
        return "—"
    size = float(n)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        if size < 1024 or unit == _SIZE_UNITS[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}"


def short_time(dt: datetime | None) -> str:
    """`"2026-09-20 10:00"` in UTC; `None` -> `"—"`."""
    if dt is None:
        return "—"
    utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M")


def flags(a: Artifact) -> str:
    """`""`, `"superseded"`, `"stale"`, or `"superseded,stale"`."""
    parts = []
    if a.superseded:
        parts.append("superseded")
    if a.stale_upstream:
        parts.append("stale")
    return ",".join(parts)


def artifact_row(a: Artifact) -> dict[str, str]:
    """One artifact as a table row: id, title, size, status, flags, updated."""
    size = a.latest_version.size_bytes if a.latest_version else None
    return {
        "id": a.id,
        "title": a.title,
        "size": human_size(size),
        "status": a.status,
        "flags": flags(a),
        "updated": short_time(a.updated_at),
    }


#: Column order for `artifact_row` (used by `ls` and, extended, `search`).
ARTIFACT_COLUMNS = ("id", "title", "size", "status", "flags", "updated")

#: Column order for `relation_row`. Header text is `col.upper()` (see `render_table`),
#: so these keys are spelled to match the brief's `DIRECTION TYPE FROM TO ORIGIN` header
#: exactly - "type"/"from"/"to" rather than the model's `relation_type`/`*_artifact_id`.
RELATION_COLUMNS = ("direction", "type", "from", "to", "origin")

#: Column order for `version_row`; `"sha256(12)"` survives `.upper()` unchanged in the
#: digits/parens, giving the brief's `SHA256(12)` header for free.
VERSION_COLUMNS = ("n", "filename", "size", "status", "sha256(12)", "created")

#: Column order for `search_hit_row`: every artifact column plus the relevance score.
SEARCH_COLUMNS = (*ARTIFACT_COLUMNS, "score")


def relation_row(r: Relation) -> dict[str, str]:
    """One relation as a table row: direction, type, from, to, origin."""
    return {
        "direction": r.direction,
        "type": r.relation_type,
        "from": r.from_artifact_id,
        "to": r.to_artifact_id,
        "origin": r.origin,
    }


def version_row(v: Version) -> dict[str, str]:
    """One version as a table row: n, filename, size, status, sha256(12), created.

    `sha256` is only absent for a version whose upload has not completed yet, in which
    case the artifact wouldn't be `ready` - included for robustness, as `"—"` like every
    other missing value in this module.
    """
    return {
        "n": str(v.version_number),
        "filename": v.filename,
        "size": human_size(v.size_bytes),
        "status": v.status,
        "sha256(12)": v.sha256[:12] if v.sha256 else "—",
        "created": short_time(v.created_at),
    }


def search_hit_row(hit: SearchHit) -> dict[str, str]:
    """A `search` hit as a table row: every `artifact_row` column plus a 3-decimal score."""
    return {**artifact_row(hit.artifact), "score": f"{hit.score:.3f}"}


def render_table(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> str:
    """A plain-text table: uppercase headers, columns padded to their widest cell,
    two spaces between columns, trailing spaces stripped per line. `title` cells over
    48 characters are cut to 48 characters with a trailing `…`. No rows -> `"(none)"`.
    """
    if not rows:
        return "(none)"

    def cell(row: Mapping[str, str], col: str) -> str:
        value = row.get(col, "")
        if col == "title" and len(value) > _TITLE_MAX:
            value = value[:_TITLE_MAX] + "…"
        return value

    body = [[cell(row, col) for col in columns] for row in rows]
    headers = [col.upper() for col in columns]
    widths = [max(len(headers[i]), *(len(r[i]) for r in body)) for i in range(len(columns))]
    lines = [
        "  ".join(value.ljust(width) for value, width in zip(line, widths, strict=True)).rstrip()
        for line in [headers, *body]
    ]
    return "\n".join(lines)


def render_fields(pairs: Sequence[tuple[str, str]]) -> str:
    """One `key: value` line per pair, in the order given - a colon and one space, no
    column padding, no case change (design spec: "One thing prints as `key: value`
    lines"). No pairs -> `"(none)"`.
    """
    if not pairs:
        return "(none)"
    return "\n".join(f"{key}: {value}" for key, value in pairs)


def artifact_fields(a: Artifact) -> list[tuple[str, str]]:
    """The fields shown for a single artifact (e.g. `artifact show`), lowercase
    snake_case keys matching the JSON field names. `external_key` and `expires` are
    included only when the artifact actually has one.
    """
    size = a.latest_version.size_bytes if a.latest_version else None
    pairs: list[tuple[str, str]] = [("id", a.id)]
    if a.external_key:
        pairs.append(("external_key", a.external_key))
    pairs += [
        ("title", a.title),
        ("status", a.status),
        ("flags", flags(a)),
        ("size", human_size(size)),
        ("created", short_time(a.created_at)),
        ("updated", short_time(a.updated_at)),
    ]
    if a.expires_at:
        pairs.append(("expires", short_time(a.expires_at)))
    pairs += [
        ("tags", ", ".join(a.tags)),
        ("description", a.description),
    ]
    return pairs


def error_payload(err: BaseException) -> dict[str, Any]:
    """`{"code", "message", "request_id"}` for any exception the CLI might surface.

    `MissingApiKey` is checked before the generic `ValueError` branch: it is a
    `ValueError` subclass, not an `ArtefaktumError`, and gets its own CLI-specific
    message pointing at `artefaktum login` (the SDK's own message names only the
    `api_key=`/env-var route, which isn't what a CLI user should be told to do).

    Every branch's message goes through `_redact_url_queries` - cheapest applied
    uniformly rather than reasoned about per exception type, and `httpx.HTTPError`'s
    `str()` in particular can embed the full request URL of a signed object-storage
    link, query string and all.
    """
    if isinstance(err, ArtefaktumError):
        return {
            "code": err.code,
            "message": _redact_url_queries(err.message),
            "request_id": err.request_id,
        }
    if isinstance(err, MissingApiKey):
        return {
            "code": "missing_api_key",
            "message": _redact_url_queries(
                'no API key: run "artefaktum login" or set ARTEFAKTUM_API_KEY'
            ),
            "request_id": None,
        }
    if isinstance(err, httpx.HTTPError):
        return {
            "code": "network_error",
            "message": _redact_url_queries(str(err) or type(err).__name__),
            "request_id": None,
        }
    if isinstance(err, ValueError):
        return {
            "code": "invalid_input",
            "message": _redact_url_queries(str(err)),
            "request_id": None,
        }
    if isinstance(err, OSError):
        return {"code": "io_error", "message": _redact_url_queries(str(err)), "request_id": None}
    # Anything else (e.g. an `AttributeError`/`TypeError` from a malformed response body)
    # is a bug, not a recognized failure mode - prefix the class name so the message is
    # still useful for a bug report even though there's no specific `code` for it.
    name = type(err).__name__
    text = str(err)
    return {
        "code": "error",
        "message": _redact_url_queries(f"{name}: {text}" if text else name),
        "request_id": None,
    }


def render_error(err: BaseException, as_json: bool) -> str:
    """`{"error": {...}}` as JSON, or `"error: <code>: <message>"` with an optional
    ` (request_id=…)` suffix as text.
    """
    payload = error_payload(err)
    if as_json:
        return json.dumps({"error": payload})
    text = f"error: {payload['code']}: {payload['message']}"
    request_id = payload.get("request_id")
    if request_id:
        text += f" (request_id={request_id})"
    return text

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from artefaktum.cli.output import (
    ARTIFACT_COLUMNS,
    RELATION_COLUMNS,
    SEARCH_COLUMNS,
    VERSION_COLUMNS,
    artifact_fields,
    artifact_row,
    error_payload,
    flags,
    human_size,
    json_mode,
    relation_row,
    render_error,
    render_fields,
    render_table,
    search_hit_row,
    short_time,
    to_jsonable,
    version_row,
)
from artefaktum.errors import MissingApiKey, from_problem
from artefaktum.models import Artifact, Download, Relation, SearchHit, Version

from .fixtures import ARTIFACT, DOWNLOAD, PROBLEM, RELATION, VERSION


class _FakeStream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


# --- to_jsonable ---------------------------------------------------------------


def test_to_jsonable_round_trips_nested_artifact_through_json():
    a = Artifact.from_json(ARTIFACT)
    data = json.loads(json.dumps(to_jsonable(a)))
    assert data["created_at"] == "2026-09-20T10:00:00+00:00"
    assert data["latest_version"]["filename"] == "report.pdf"
    assert data["superseded"] is False
    assert data["tags"] == ["churn"]


def test_to_jsonable_keeps_the_real_download_url_not_the_redacted_repr():
    d = Download.from_json(DOWNLOAD)
    assert "sig=" not in repr(d)  # the SDK's own repr redacts it
    data = to_jsonable(d)
    assert "sig=" in data["url"]
    assert data["url"] == DOWNLOAD["url"]


def test_to_jsonable_scalars_and_none():
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(3) == 3
    assert to_jsonable(3.5) == 3.5
    assert to_jsonable("x") == "x"


def test_to_jsonable_dict_recurses_and_stringifies_keys():
    assert to_jsonable({1: "a", "b": [1, 2]}) == {"1": "a", "b": [1, 2]}


def test_to_jsonable_list_tuple_set_frozenset():
    assert to_jsonable([1, (2, 3)]) == [1, [2, 3]]
    assert to_jsonable({"b", "a"}) == ["a", "b"]
    assert to_jsonable(frozenset({"y", "x"})) == ["x", "y"]


def test_to_jsonable_datetime_date_path_timedelta():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert to_jsonable(dt) == dt.isoformat()
    assert to_jsonable(date(2026, 1, 2)) == "2026-01-02"
    assert to_jsonable(Path("some/x")) == "some/x"
    assert to_jsonable(timedelta(seconds=90)) == 90.0


def test_to_jsonable_falls_back_to_str_for_anything_else():
    class Opaque:
        def __str__(self) -> str:
            return "opaque!"

    assert to_jsonable(Opaque()) == "opaque!"


# --- json_mode -------------------------------------------------------------


@pytest.mark.parametrize(
    "choice,is_tty,expected",
    [
        ("json", True, True),
        ("json", False, True),
        ("table", True, False),
        ("table", False, False),
        (None, True, False),
        (None, False, True),
    ],
)
def test_json_mode(choice, is_tty, expected):
    assert json_mode(choice, _FakeStream(is_tty)) is expected


# --- human_size / short_time -------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [
        (None, "—"),
        (0, "0 B"),
        (1023, "1023 B"),
        (1536, "1.5 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
    ],
)
def test_human_size(n, expected):
    assert human_size(n) == expected


def test_short_time_formats_utc_and_none():
    dt = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
    assert short_time(dt) == "2026-09-20 10:00"
    assert short_time(None) == "—"


def test_short_time_converts_non_utc_to_utc():
    dt = datetime(2026, 9, 20, 5, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert short_time(dt) == "2026-09-20 10:00"


# --- flags / artifact_row ------------------------------------------------------


@pytest.mark.parametrize(
    "superseded,stale,expected",
    [
        (False, False, ""),
        (True, False, "superseded"),
        (False, True, "stale"),
        (True, True, "superseded,stale"),
    ],
)
def test_flags(superseded, stale, expected):
    a = Artifact.from_json({**ARTIFACT, "superseded": superseded, "stale_upstream": stale})
    assert flags(a) == expected


def test_artifact_row_has_expected_keys_and_values():
    a = Artifact.from_json(ARTIFACT)
    row = artifact_row(a)
    assert row == {
        "id": "0199-a1",
        "title": "Q3 churn",
        "size": "1.2 KB",
        "status": "ready",
        "flags": "",
        "updated": "2026-09-20 10:00",
    }


def test_artifact_row_size_is_dash_without_a_version():
    a = Artifact.from_json({**ARTIFACT, "latest_version": None})
    assert artifact_row(a)["size"] == "—"


# --- render_table ------------------------------------------------------------


def test_render_table_alignment_exact_string():
    rows = [
        {"id": "0199-a1", "title": "Q3 churn", "status": "ready"},
        {"id": "0199-a222", "title": "x", "status": "processing"},
    ]
    out = render_table(rows, ["id", "title", "status"])
    assert out == (
        "ID         TITLE     STATUS\n0199-a1    Q3 churn  ready\n0199-a222  x         processing"
    )


def test_render_table_truncates_long_titles():
    long_title = "x" * 60
    rows = [{"title": long_title}]
    out = render_table(rows, ["title"])
    lines = out.splitlines()
    assert lines[1] == "x" * 48 + "…"


def test_render_table_does_not_truncate_exactly_48_char_title():
    title48 = "x" * 48
    rows = [{"title": title48}]
    out = render_table(rows, ["title"])
    lines = out.splitlines()
    assert lines[1] == title48
    assert "…" not in lines[1]


def test_render_table_no_rows_is_none_marker():
    assert render_table([], ["id", "title"]) == "(none)"


# --- render_fields / artifact_fields -------------------------------------------


def test_render_fields_is_key_colon_space_value_lines():
    out = render_fields([("id", "0199-a1"), ("title", "Q3 churn")])
    assert out == "id: 0199-a1\ntitle: Q3 churn"


def test_render_fields_keys_are_printed_exactly_as_passed():
    # No uppercasing, no padding - a single pair with an already-short key, matching
    # the design spec's `tenant_id: t1` example verbatim.
    assert render_fields([("tenant_id", "t1")]) == "tenant_id: t1"


def test_render_fields_empty_is_none_marker():
    assert render_fields([]) == "(none)"


def test_artifact_fields_includes_id_title_and_flags():
    a = Artifact.from_json(ARTIFACT)
    pairs = dict(artifact_fields(a))
    assert pairs["id"] == "0199-a1"
    assert pairs["title"] == "Q3 churn"
    assert pairs["flags"] == ""
    assert pairs["size"] == "1.2 KB"


def test_artifact_fields_includes_external_key_and_expires_when_set():
    a = Artifact.from_json(
        {**ARTIFACT, "external_key": "ext-1", "expires_at": "2026-10-01T00:00:00Z"}
    )
    pairs = dict(artifact_fields(a))
    assert pairs["external_key"] == "ext-1"
    assert pairs["expires"] == "2026-10-01 00:00"


def test_artifact_fields_omits_external_key_and_expires_when_unset():
    a = Artifact.from_json(ARTIFACT)  # external_key and expires_at are None in the fixture
    pairs = dict(artifact_fields(a))
    assert "external_key" not in pairs
    assert "expires" not in pairs


# --- error_payload / render_error ---------------------------------------------


def test_error_payload_for_artefaktum_error_with_request_id():
    err = from_problem(404, PROBLEM, "Not Found")
    payload = error_payload(err)
    assert payload == {
        "code": "artifact_not_found",
        "message": "no such artifact",
        "request_id": "req-1",
    }


def test_error_payload_for_missing_api_key_uses_the_cli_specific_message():
    err = MissingApiKey("pass api_key= or set ARTEFAKTUM_API_KEY")
    payload = error_payload(err)
    assert payload == {
        "code": "missing_api_key",
        "message": 'no API key: run "artefaktum login" or set ARTEFAKTUM_API_KEY',
        "request_id": None,
    }


def test_error_payload_for_httpx_connect_error():
    err = httpx.ConnectError("boom")
    assert error_payload(err) == {
        "code": "network_error",
        "message": "boom",
        "request_id": None,
    }


def test_error_payload_for_plain_value_error():
    err = ValueError("bad duration")
    assert error_payload(err) == {
        "code": "invalid_input",
        "message": "bad duration",
        "request_id": None,
    }


def test_error_payload_for_unlisted_exception_prefixes_the_class_name():
    # `error_payload` only special-cases `ArtefaktumError`/`MissingApiKey`/`httpx.HTTPError`/
    # `ValueError`/`OSError`; anything else (e.g. an `AttributeError` from a malformed
    # response body) falls into the generic "error" branch, whose message is prefixed with
    # the exception's class name so a user can report it usefully.
    err = TypeError("string indices must be integers, not 'str'")
    payload = error_payload(err)
    assert payload["code"] == "error"
    assert payload["message"] == "TypeError: string indices must be integers, not 'str'"
    assert payload["request_id"] is None


def test_render_error_json_shape():
    err = from_problem(404, PROBLEM, "Not Found")
    out = json.loads(render_error(err, as_json=True))
    assert out == {
        "error": {
            "code": "artifact_not_found",
            "message": "no such artifact",
            "request_id": "req-1",
        }
    }


def test_render_error_text_includes_request_id_when_present():
    err = from_problem(404, PROBLEM, "Not Found")
    assert render_error(err, as_json=False) == (
        "error: artifact_not_found: no such artifact (request_id=req-1)"
    )


def test_render_error_text_omits_request_id_when_absent():
    err = ValueError("bad duration")
    assert render_error(err, as_json=False) == "error: invalid_input: bad duration"


def test_error_payload_redacts_signed_url_query_in_http_status_error():
    request = httpx.Request("GET", "https://storage.test/o?sig=SECRET123")
    response = httpx.Response(403, request=request)
    err = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'https://storage.test/o?sig=SECRET123'",
        request=request,
        response=response,
    )
    payload = error_payload(err)
    assert payload["code"] == "network_error"
    assert "SECRET123" not in payload["message"]
    assert "https://storage.test/o" in payload["message"]

    for as_json in (True, False):
        rendered = render_error(err, as_json=as_json)
        assert "SECRET123" not in rendered
        assert "https://storage.test/o" in rendered


def test_error_payload_message_without_a_url_is_unchanged():
    err = ValueError("bad duration")
    assert error_payload(err)["message"] == "bad duration"


def test_error_payload_redacts_two_urls_in_one_message():
    err = ValueError("see https://a.test/x?sig=1 and https://b.test/y?sig=2 for details")
    assert error_payload(err)["message"] == (
        "see https://a.test/x?… and https://b.test/y?… for details"
    )


def test_version_fixture_is_used_directly_too():
    v = Version.from_json(VERSION)
    assert to_jsonable(v)["filename"] == "report.pdf"


# --- relation_row / version_row / search_hit_row ------------------------------


def test_relation_row_has_expected_keys_and_values():
    r = Relation.from_json(RELATION)
    assert relation_row(r) == {
        "direction": "outgoing",
        "type": "derived_from",
        "from": "0199-a1",
        "to": "0199-a0",
        "origin": "explicit",
    }


def test_version_row_has_expected_keys_and_truncated_sha():
    v = Version.from_json(VERSION)
    row = version_row(v)
    assert row == {
        "n": "1",
        "filename": "report.pdf",
        "size": "1.2 KB",
        "status": "ready",
        "sha256(12)": VERSION["sha256"][:12],
        "created": "2026-09-20 10:00",
    }


def test_version_row_sha256_is_dash_when_missing():
    v = Version.from_json({**VERSION, "sha256": None})
    assert version_row(v)["sha256(12)"] == "—"


def test_search_hit_row_extends_artifact_row_with_a_3_decimal_score():
    hit = SearchHit.from_json({"artifact": ARTIFACT, "score": 0.876, "match_mode": "hybrid"})
    row = search_hit_row(hit)
    assert row["id"] == "0199-a1"
    assert row["title"] == "Q3 churn"
    assert row["score"] == "0.876"


def test_search_hit_row_score_is_formatted_to_three_decimals_even_when_round():
    hit = SearchHit.from_json({"artifact": ARTIFACT, "score": 1.0, "match_mode": "hybrid"})
    assert search_hit_row(hit)["score"] == "1.000"


def test_column_constants_match_the_brief_headers():
    assert ARTIFACT_COLUMNS == ("id", "title", "size", "status", "flags", "updated")
    assert RELATION_COLUMNS == ("direction", "type", "from", "to", "origin")
    assert VERSION_COLUMNS == ("n", "filename", "size", "status", "sha256(12)", "created")
    assert SEARCH_COLUMNS == (*ARTIFACT_COLUMNS, "score")

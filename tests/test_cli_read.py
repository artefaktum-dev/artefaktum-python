"""End-to-end tests for the read commands: get, ls, search, link, relations, versions."""

from __future__ import annotations

import json

import httpx

from .cli_helpers import run_cli
from .fixtures import ARTIFACT, DOWNLOAD, PROBLEM, RELATION, VERSION

_WITH_KEY = {"ARTEFAKTUM_API_KEY": "ak_test"}

# A real UUID so `_project_id` uses it as-is instead of resolving a slug through an
# extra `GET /v1/projects` call that these handlers don't serve.
UUID_PROJECT = "01a0be89-1cde-74c1-ab17-8814cc9d9141"


# --- get -------------------------------------------------------------------------


def test_get_by_id_hits_the_get_route_and_prints_fields_table():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=ARTIFACT)

    result = run_cli(["--table", "get", "0199-a1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/artifacts/0199-a1"
    assert "id: 0199-a1" in result.stdout
    assert "title: Q3 churn" in result.stdout


def test_get_by_id_json():
    result = run_cli(
        ["get", "0199-a1"], lambda r: httpx.Response(200, json=ARTIFACT), env=_WITH_KEY
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["id"] == "0199-a1"


def test_get_by_key_sends_project_id_and_hits_the_by_external_key_route():
    # The slash in the key is percent-encoded on the wire (see
    # test_ops.py::test_get_by_external_key_percent_encodes_slashes for that guarantee);
    # httpx decodes `.path` back for us, so this only checks routing and query params.
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=ARTIFACT)

    result = run_cli(
        ["--project", UUID_PROJECT, "get", "--key", "vendor/x"], handler, env=_WITH_KEY
    )
    assert result.exit_code == 0
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/artifacts/by-external-key/vendor/x"
    assert "vendor%2Fx" in str(requests[0].url)
    assert requests[0].url.params["project_id"] == UUID_PROJECT


def test_get_neither_id_nor_key_is_a_usage_error():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    result = run_cli(["get"], handler, env=_WITH_KEY)
    assert result.exit_code == 2


def test_get_both_id_and_key_is_a_usage_error():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    result = run_cli(["get", "0199-a1", "--key", "ext-1"], handler, env=_WITH_KEY)
    assert result.exit_code == 2


def test_get_by_key_missing_exits_3():
    result = run_cli(
        ["--project", UUID_PROJECT, "get", "--key", "nope"],
        lambda r: httpx.Response(404, json=PROBLEM),
        env=_WITH_KEY,
    )
    assert result.exit_code == 3


# --- ls --------------------------------------------------------------------------


def test_ls_json_default_prints_the_page_shape():
    result = run_cli(
        ["--project", UUID_PROJECT, "ls"],
        lambda r: httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": "c1"}),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert set(data) == {"items", "next_cursor"}
    assert data["next_cursor"] == "c1"
    assert data["items"][0]["id"] == "0199-a1"


def test_ls_table_shows_header_and_flags_for_superseded_and_stale():
    flagged = {**ARTIFACT, "superseded": True, "stale_upstream": True}
    result = run_cli(
        ["--project", UUID_PROJECT, "--table", "ls"],
        lambda r: httpx.Response(200, json={"items": [flagged], "next_cursor": None}),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ID", "TITLE", "SIZE", "STATUS", "FLAGS", "UPDATED"]
    assert "superseded,stale" in lines[1]


def test_ls_sends_repeated_status_tag_and_limit_as_query_params():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": None})

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "ls",
            "--status",
            "ready",
            "--status",
            "processing",
            "--tag",
            "churn",
            "--limit",
            "5",
        ],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    req = requests[0]
    assert req.method == "GET"
    assert req.url.path == "/v1/artifacts"
    assert req.url.params.get_list("status") == ["ready", "processing"]
    assert req.url.params["tag"] == "churn"
    assert req.url.params["limit"] == "5"
    assert req.url.params["project_id"] == UUID_PROJECT


def test_ls_all_follows_two_pages():
    requests: list[httpx.Request] = []
    second_artifact = {**ARTIFACT, "id": "0199-a2"}

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        cursor = r.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": "c2"})
        assert cursor == "c2"
        return httpx.Response(200, json={"items": [second_artifact], "next_cursor": None})

    result = run_cli(["--project", UUID_PROJECT, "ls", "--all"], handler, env=_WITH_KEY)
    assert result.exit_code == 0
    assert len(requests) == 2
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert [a["id"] for a in data] == ["0199-a1", "0199-a2"]


# --- search ------------------------------------------------------------------------


def test_search_posts_query_mode_limit_and_tags_all_filter():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(
            200,
            json={
                "items": [{"artifact": ARTIFACT, "score": 0.75, "match_mode": "hybrid"}],
                "next_cursor": None,
                "mode": "hybrid",
            },
        )

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "search",
            "churn",
            "--mode",
            "hybrid",
            "--tag",
            "a",
            "--tag",
            "b",
            "--limit",
            "3",
        ],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    req = requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/artifacts/search"
    body = json.loads(req.content)
    assert body["project_id"] == UUID_PROJECT
    assert body["query"] == "churn"
    assert body["mode"] == "hybrid"
    assert body["limit"] == 3
    assert body["filters"]["tags_all"] == ["a", "b"]
    assert body["filters"]["exclude_superseded"] is True


def test_search_history_sends_exclude_superseded_false():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json={"items": [], "next_cursor": None, "mode": "hybrid"})

    result = run_cli(
        ["--project", UUID_PROJECT, "search", "x", "--history"], handler, env=_WITH_KEY
    )
    assert result.exit_code == 0
    body = json.loads(requests[0].content)
    assert body["filters"]["exclude_superseded"] is False


def test_search_json_shape():
    result = run_cli(
        ["--project", UUID_PROJECT, "search", "churn"],
        lambda r: httpx.Response(
            200,
            json={
                "items": [{"artifact": ARTIFACT, "score": 0.5, "match_mode": "hybrid"}],
                "next_cursor": "c1",
                "mode": "hybrid",
            },
        ),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["mode"] == "hybrid"
    assert data["next_cursor"] == "c1"
    assert data["items"][0]["score"] == 0.5
    assert data["items"][0]["match_mode"] == "hybrid"
    assert data["items"][0]["artifact"]["id"] == "0199-a1"


def test_search_table_includes_score_column():
    result = run_cli(
        ["--project", UUID_PROJECT, "--table", "search", "churn"],
        lambda r: httpx.Response(
            200,
            json={
                "items": [{"artifact": ARTIFACT, "score": 0.75, "match_mode": "hybrid"}],
                "next_cursor": None,
                "mode": "hybrid",
            },
        ),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split()[-1] == "SCORE"
    assert lines[1].split()[-1] == "0.750"


# --- link ----------------------------------------------------------------------------


def test_link_hits_the_download_route():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=DOWNLOAD)

    result = run_cli(["link", "0199-a1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/artifacts/0199-a1/download"


def test_link_json_shape():
    result = run_cli(
        ["link", "0199-a1"], lambda r: httpx.Response(200, json=DOWNLOAD), env=_WITH_KEY
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data == {
        "url": DOWNLOAD["url"],
        "method": "GET",
        "expires_at": "2026-09-20T10:15:00+00:00",
        "version_id": "0199-v1",
    }


def test_link_table_prints_exactly_the_url_and_nothing_else():
    result = run_cli(
        ["--table", "link", "0199-a1"], lambda r: httpx.Response(200, json=DOWNLOAD), env=_WITH_KEY
    )
    assert result.exit_code == 0
    assert result.stdout == DOWNLOAD["url"] + "\n"
    assert "sig=" in result.stdout
    assert result.stderr == ""


# --- relations -------------------------------------------------------------------------


def test_relations_hits_the_relations_route():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=[RELATION])

    result = run_cli(["relations", "0199-a1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/artifacts/0199-a1/relations"


def test_relations_json():
    result = run_cli(
        ["relations", "0199-a1"], lambda r: httpx.Response(200, json=[RELATION]), env=_WITH_KEY
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["relation_type"] == "derived_from"


def test_relations_table_header_and_row():
    result = run_cli(
        ["--table", "relations", "0199-a1"],
        lambda r: httpx.Response(200, json=[RELATION]),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["DIRECTION", "TYPE", "FROM", "TO", "ORIGIN"]
    assert lines[1].split() == ["outgoing", "derived_from", "0199-a1", "0199-a0", "explicit"]


# --- versions --------------------------------------------------------------------------


def test_versions_hits_the_versions_route():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=[VERSION])

    result = run_cli(["versions", "0199-a1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/artifacts/0199-a1/versions"


def test_versions_json():
    result = run_cli(
        ["versions", "0199-a1"], lambda r: httpx.Response(200, json=[VERSION]), env=_WITH_KEY
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["filename"] == "report.pdf"


def test_versions_table_header_and_row():
    result = run_cli(
        ["--table", "versions", "0199-a1"],
        lambda r: httpx.Response(200, json=[VERSION]),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["N", "FILENAME", "SIZE", "STATUS", "SHA256(12)", "CREATED"]
    assert VERSION["sha256"][:12] in lines[1]

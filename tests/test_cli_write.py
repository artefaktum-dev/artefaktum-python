"""End-to-end tests for the write commands: push, pull, rm, relate, resolve, run."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from artefaktum.cli import app as app_module

from .cli_helpers import run_cli
from .fixtures import ARTIFACT, PROBLEM, RELATION, RUN, UPLOAD

_WITH_KEY = {"ARTEFAKTUM_API_KEY": "ak_test"}

# A real UUID so `_project_id` uses it as-is instead of resolving a slug through an
# extra `GET /v1/projects` call that these handlers don't serve.
UUID_PROJECT = "01a0be89-1cde-74c1-ab17-8814cc9d9141"

_READY = ARTIFACT
_PROCESSING = {**ARTIFACT, "status": "processing"}


def _artifact_ref(status: str) -> dict[str, object]:
    return {"id": ARTIFACT["id"], "version_id": "0199-v1", "status": status}


# --- push ----------------------------------------------------------------------------


def test_push_sends_create_upload_body_puts_completes_and_polls_to_ready(tmp_path):
    src = tmp_path / "hello.txt"
    src.write_bytes(b"hello")

    requests: list[httpx.Request] = []
    storage_requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        if r.url.path == "/v1/artifacts/uploads":
            return httpx.Response(200, json=UPLOAD)
        if r.url.path.endswith("/complete"):
            return httpx.Response(200, json=_artifact_ref("processing"))
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}":
            return httpx.Response(200, json=_READY)
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    def storage(r: httpx.Request) -> httpx.Response:
        storage_requests.append(r)
        return httpx.Response(200)

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "push",
            str(src),
            "--title",
            "Q3 churn",
            "--tag",
            "churn",
            "--key",
            "ext-1",
            "--meta",
            "a=1",
            "--meta",
            "b=x",
            "--expires",
            "30d",
        ],
        handler,
        storage=storage,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["id"] == ARTIFACT["id"]
    assert data["status"] == "ready"

    upload_reqs = [r for r in requests if r.url.path == "/v1/artifacts/uploads"]
    assert len(upload_reqs) == 1
    body = json.loads(upload_reqs[0].content)
    assert body["title"] == "Q3 churn"
    assert body["tags"] == ["churn"]
    assert body["metadata"] == {"a": "1", "b": "x"}
    assert body["external_key"] == "ext-1"
    expires_at = datetime.fromisoformat(body["expires_at"])
    expected = datetime.now(timezone.utc) + timedelta(days=30)
    assert abs((expires_at - expected).total_seconds()) < 60

    assert len(storage_requests) == 1


def test_push_summary_and_run_reach_the_create_upload_body(tmp_path):
    # `_ops._create_upload` names these fields `summary` and `run_id` in the request
    # body; the CLI's own flags are `--summary` and `--run` (`run_id` is a Python
    # keyword-adjacent name that isn't a great flag name, and `client.artifacts.push`
    # itself takes `run=`, not `run_id=` - see `push_cmd`'s call site).
    src = tmp_path / "hello.txt"
    src.write_bytes(b"hello")
    upload_requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/v1/artifacts/uploads":
            upload_requests.append(r)
            return httpx.Response(200, json=UPLOAD)
        if r.url.path.endswith("/complete"):
            return httpx.Response(200, json=_artifact_ref("processing"))
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}":
            return httpx.Response(200, json=_READY)
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "push",
            str(src),
            "--title",
            "t",
            "--summary",
            "fixed the churn calc",
            "--run",
            "run-42",
        ],
        handler,
        storage=lambda r: httpx.Response(200),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    assert len(upload_requests) == 1
    body = json.loads(upload_requests[0].content)
    assert body["summary"] == "fixed the churn calc"
    assert body["run_id"] == "run-42"


def test_push_no_wait_does_one_get(tmp_path):
    src = tmp_path / "hello.txt"
    src.write_bytes(b"hello")

    get_requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/v1/artifacts/uploads":
            return httpx.Response(200, json=UPLOAD)
        if r.url.path.endswith("/complete"):
            return httpx.Response(200, json=_artifact_ref("processing"))
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}":
            get_requests.append(r)
            return httpx.Response(200, json=_PROCESSING)
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    result = run_cli(
        ["--project", UUID_PROJECT, "push", str(src), "--title", "t", "--no-wait"],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    assert len(get_requests) == 1
    data = json.loads(result.stdout)
    assert data["status"] == "processing"


def test_push_missing_file_exits_2():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    result = run_cli(["push", "/no/such/file", "--title", "t"], handler, env=_WITH_KEY)
    assert result.exit_code == 2


def test_push_broken_meta_exits_2(tmp_path):
    src = tmp_path / "hello.txt"
    src.write_bytes(b"hello")

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    result = run_cli(["push", str(src), "--title", "t", "--meta", "broken"], handler, env=_WITH_KEY)
    assert result.exit_code == 2


# --- pull ----------------------------------------------------------------------------


def test_pull_writes_the_file_under_dest_and_prints_its_path(tmp_path):
    body = b"hello from pull\n"
    version = {**ARTIFACT["latest_version"], "sha256": hashlib.sha256(body).hexdigest()}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/download":
            return httpx.Response(
                200,
                json={
                    "url": "https://storage.test/o?sig=1",
                    "method": "GET",
                    "expires_at": "2026-09-20T10:15:00Z",
                    "version_id": "0199-v1",
                },
            )
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/versions":
            return httpx.Response(200, json=[version])
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    def storage(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    # Default verify=True: the digest above matches, so this also covers the checked path.
    result = run_cli(
        ["pull", ARTIFACT["id"], str(dest_dir)], handler, storage=storage, env=_WITH_KEY
    )
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    expected_path = dest_dir / "report.pdf"
    assert data["path"] == str(expected_path)
    assert expected_path.read_bytes() == body


def _download_handler(version: dict[str, object]):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/download":
            return httpx.Response(
                200,
                json={
                    "url": "https://storage.test/o?sig=1",
                    "method": "GET",
                    "expires_at": "2026-09-20T10:15:00Z",
                    "version_id": "0199-v1",
                },
            )
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/versions":
            return httpx.Response(200, json=[version])
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    return handler


def test_pull_trailing_slash_creates_missing_directory_and_writes_into_it(tmp_path):
    # The SDK's `_dest_path` decides "this is a directory" from a trailing separator on
    # the *string* it is given (`out/` -> create `out/` and write inside it), not from
    # whether the path already exists. `click.Path(path_type=pathlib.Path)` would
    # normalise `out/` to `Path("out")` before the SDK ever sees the trailing slash,
    # silently turning this into "write a file literally named `out`" instead - this
    # test is the regression check for keeping `dest` a plain, unconverted string.
    body = b"hello"
    handler = _download_handler({**ARTIFACT["latest_version"], "sha256": None})
    missing_dir = tmp_path / "out"
    assert not missing_dir.exists()

    result = run_cli(
        ["pull", ARTIFACT["id"], f"{missing_dir}/", "--no-verify"],
        handler,
        storage=lambda r: httpx.Response(200, content=body),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    assert missing_dir.is_dir()
    written = missing_dir / "report.pdf"
    assert written.read_bytes() == body
    assert json.loads(result.stdout)["path"] == str(written)


def test_pull_exact_file_path_writes_exactly_that_file(tmp_path):
    body = b"hello"
    handler = _download_handler({**ARTIFACT["latest_version"], "sha256": None})
    exact = tmp_path / "exact.bin"

    result = run_cli(
        ["pull", ARTIFACT["id"], str(exact), "--no-verify"],
        handler,
        storage=lambda r: httpx.Response(200, content=body),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    assert exact.read_bytes() == body
    assert not (tmp_path / "exact.bin" / "report.pdf").exists()
    assert json.loads(result.stdout)["path"] == str(exact)


def test_pull_default_dest_writes_into_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = b"hello"
    handler = _download_handler({**ARTIFACT["latest_version"], "sha256": None})

    result = run_cli(
        ["pull", ARTIFACT["id"], "--no-verify"],
        handler,
        storage=lambda r: httpx.Response(200, content=body),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    written = tmp_path / "report.pdf"
    assert written.read_bytes() == body
    # `dest` defaults to "." (relative); the SDK returns `Path(".") / filename`, which
    # pathlib simplifies to a bare relative path rather than resolving it against cwd.
    assert json.loads(result.stdout)["path"] == "report.pdf"


def test_pull_verified_matching_sha256_succeeds(tmp_path):
    # Default verify=True, exercised with a version whose recorded digest *matches* the
    # downloaded body (the existing `test_pull_writes_the_file_under_dest_and_prints_its_path`
    # also covers this implicitly, but not by an explicit, named "verification on" test).
    body = b"the exact bytes the server will serve\n"
    version = {**ARTIFACT["latest_version"], "sha256": hashlib.sha256(body).hexdigest()}
    handler = _download_handler(version)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    result = run_cli(
        ["pull", ARTIFACT["id"], str(dest_dir)],
        handler,
        storage=lambda r: httpx.Response(200, content=body),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    written = dest_dir / "report.pdf"
    assert written.read_bytes() == body
    assert json.loads(result.stdout)["path"] == str(written)


def test_pull_verified_mismatching_sha256_fails_and_leaves_no_partial_file(tmp_path):
    # `_files.download_to` writes to a `.part` file and only `os.replace`s it into place
    # once the digest checks out; on a mismatch it raises `IntegrityError` and removes the
    # `.part` file (`_finish_download`) - neither the final name nor the `.part` name
    # should exist afterwards.
    body = b"whatever the server actually sends"
    wrong_digest = hashlib.sha256(b"something else").hexdigest()
    version = {**ARTIFACT["latest_version"], "sha256": wrong_digest}
    handler = _download_handler(version)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    result = run_cli(
        ["pull", ARTIFACT["id"], str(dest_dir)],
        handler,
        storage=lambda r: httpx.Response(200, content=body),
        env=_WITH_KEY,
    )
    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "integrity_error"
    final = dest_dir / "report.pdf"
    assert not final.exists()
    assert not final.with_name(final.name + ".part").exists()


def test_pull_table_prints_the_path(tmp_path):
    body = b"x"

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/download":
            return httpx.Response(
                200,
                json={
                    "url": "https://storage.test/o?sig=1",
                    "method": "GET",
                    "expires_at": "2026-09-20T10:15:00Z",
                    "version_id": "0199-v1",
                },
            )
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}/versions":
            return httpx.Response(200, json=[ARTIFACT["latest_version"]])
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    def storage(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    dest_dir = tmp_path / "out2"
    dest_dir.mkdir()
    result = run_cli(
        ["--table", "pull", ARTIFACT["id"], str(dest_dir), "--no-verify"],
        handler,
        storage=storage,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    expected_path = dest_dir / "report.pdf"
    assert result.stdout == str(expected_path) + "\n"


# --- rm ------------------------------------------------------------------------------


def test_rm_hits_delete_route_and_prints_deleted_json():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(204)

    result = run_cli(["rm", "0199-a1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/v1/artifacts/0199-a1"
    assert json.loads(result.stdout) == {"deleted": "0199-a1"}


def test_rm_table_prints_deleted_line():
    result = run_cli(["--table", "rm", "0199-a1"], lambda r: httpx.Response(204), env=_WITH_KEY)
    assert result.exit_code == 0
    assert result.stdout == "deleted 0199-a1\n"


def test_rm_not_found_exits_3():
    result = run_cli(["rm", "0199-a1"], lambda r: httpx.Response(404, json=PROBLEM), env=_WITH_KEY)
    assert result.exit_code == 3


# --- relate --------------------------------------------------------------------------


def test_relate_default_type_is_derived_from():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=RELATION)

    result = run_cli(["relate", "0199-a1", "0199-a0"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/artifacts/0199-a1/relations"
    body = json.loads(requests[0].content)
    assert body["relation_type"] == "derived_from"
    assert body["to_artifact_id"] == "0199-a0"


def test_relate_bad_type_exits_2():
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made")

    result = run_cli(
        ["relate", "0199-a1", "0199-a0", "--type", "friend_of"], handler, env=_WITH_KEY
    )
    assert result.exit_code == 2


# --- resolve ---------------------------------------------------------------------------


def test_resolve_hit_does_not_touch_storage(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    storage_calls = []

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/v1/artifacts/resolve"
        return httpx.Response(200, json={"status": "hit", "artifact": ARTIFACT})

    def storage(r: httpx.Request) -> httpx.Response:
        storage_calls.append(r)
        return httpx.Response(200)

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
        ],
        handler,
        storage=storage,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "hit"
    assert data["artifact"]["id"] == ARTIFACT["id"]
    assert storage_calls == []


def test_resolve_create_uploads_and_reports_created(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    storage_calls = []

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/v1/artifacts/resolve":
            return httpx.Response(
                200,
                json={
                    "status": "create",
                    "reservation": _artifact_ref("pending_upload"),
                    "upload": UPLOAD["upload"],
                    "retry_after_seconds": None,
                },
            )
        if r.url.path.endswith("/complete"):
            return httpx.Response(200, json=_artifact_ref("processing"))
        if r.url.path == f"/v1/artifacts/{ARTIFACT['id']}":
            return httpx.Response(200, json=_READY)
        raise AssertionError(f"unexpected request {r.method} {r.url.path}")

    def storage(r: httpx.Request) -> httpx.Response:
        storage_calls.append(r)
        return httpx.Response(200)

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
        ],
        handler,
        storage=storage,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "created"
    assert data["artifact"]["status"] == "ready"
    assert len(storage_calls) == 1


def test_resolve_pending_then_hit_sleeps_once(tmp_path, monkeypatch):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(app_module, "_sleep", lambda s: sleeps.append(s))

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/v1/artifacts/resolve"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"status": "pending", "retry_after_seconds": 1})
        return httpx.Response(200, json={"status": "hit", "artifact": ARTIFACT})

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
            "--wait",
            "5",
        ],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "hit"
    assert len(sleeps) == 1
    assert calls["n"] == 2


def test_resolve_unexpected_status_is_a_value_error_exits_1(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/v1/artifacts/resolve"
        return httpx.Response(200, json={"status": "some_future_status"})

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
        ],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert "some_future_status" in err["error"]["message"]


def test_resolve_hit_table_prints_resolution_line_once_not_a_duplicate_status_line(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "--table",
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
        ],
        lambda r: httpx.Response(200, json={"status": "hit", "artifact": ARTIFACT}),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "resolution: hit"
    assert lines.count("status: ready") == 1
    assert not any(line.startswith("status: hit") for line in lines)


def test_resolve_help_is_user_facing_not_implementation_detail():
    # `--help` used to say "sleeping `retry_after_seconds` between tries" - a field name
    # from the response body, not something a user needs to know to use the command.
    result = run_cli(["resolve", "--help"], lambda r: httpx.Response(200))
    assert result.exit_code == 0
    assert "retry_after_seconds" not in result.stdout
    assert "hit" in result.stdout
    assert "create" in result.stdout
    assert "pending" in result.stdout
    assert "4" in result.stdout


def test_resolve_pending_past_deadline_exits_4(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/v1/artifacts/resolve"
        return httpx.Response(200, json={"status": "pending", "retry_after_seconds": 2})

    result = run_cli(
        [
            "--project",
            UUID_PROJECT,
            "resolve",
            "ext-1",
            "--file",
            str(src),
            "--title",
            "t",
            "--wait",
            "0",
        ],
        handler,
        env=_WITH_KEY,
    )
    assert result.exit_code == 4
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "resolve_pending"


# --- run -------------------------------------------------------------------------------


def test_run_new_creates_a_run():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json=RUN)

    result = run_cli(["--project", UUID_PROJECT, "run", "new"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/runs"
    data = json.loads(result.stdout)
    assert data["id"] == RUN["id"]


def test_run_new_with_explicit_run_id_sends_it():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json={**RUN, "id": "my-run-id"})

    result = run_cli(["--project", UUID_PROJECT, "run", "new", "my-run-id"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "POST"
    body = json.loads(requests[0].content)
    assert body["run_id"] == "my-run-id"
    data = json.loads(result.stdout)
    assert data["id"] == "my-run-id"


def test_run_seal_hits_seal_route():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json={**RUN, "sealed_at": "2026-09-20T10:05:00Z"})

    result = run_cli(["run", "seal", "run-1"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/runs/run-1/seal"
    data = json.loads(result.stdout)
    assert data["sealed_at"] is not None


def test_run_seal_not_found_exits_3():
    result = run_cli(
        ["run", "seal", "run-x"],
        lambda r: httpx.Response(404, json=PROBLEM),
        env=_WITH_KEY,
    )
    assert result.exit_code == 3
    err = json.loads(result.stderr)
    assert "error" in err


def test_run_ls_lists_artifacts_in_a_run():
    requests: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        requests.append(r)
        return httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": None})

    result = run_cli(["run", "ls", "run-1", "--limit", "5"], handler, env=_WITH_KEY)
    assert result.exit_code == 0, result.stderr
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/runs/run-1/artifacts"
    assert requests[0].url.params["limit"] == "5"
    data = json.loads(result.stdout)
    assert data["items"][0]["id"] == ARTIFACT["id"]


# --- ls hint (task 4 carry-over fixes) --------------------------------------------------


def test_ls_table_with_more_pages_prints_hint_to_stderr():
    result = run_cli(
        ["--project", UUID_PROJECT, "--table", "ls"],
        lambda r: httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": "c1"}),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    assert "more results available: use --all" in result.stderr


def test_ls_json_with_more_pages_has_no_hint_anywhere():
    result = run_cli(
        ["--project", UUID_PROJECT, "ls"],
        lambda r: httpx.Response(200, json={"items": [ARTIFACT], "next_cursor": "c1"}),
        env=_WITH_KEY,
    )
    assert result.exit_code == 0
    assert "more results available" not in result.stderr
    assert "more results available" not in result.stdout


def test_search_mode_option_has_help_text():
    from artefaktum.cli.app import app as cli_app

    search_cmd = cli_app.commands["search"]
    mode_param = next(p for p in search_cmd.params if p.name == "mode")
    assert mode_param.help

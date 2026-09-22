"""`Artefaktum` / `AsyncArtefaktum`: namespaces, project resolution, push and pull (§5, §7).

Two notes on where these tests depart from the task brief's snippet:

* The brief's examples pass `project=` to `artifacts.get`. `_ops.get_artifact` takes only
  `artifact_id` -- the route is not project-scoped -- so `get` has no `project` parameter
  and would never trigger slug resolution. The resolution tests below drive
  `artifacts.list` instead, which *is* project-scoped (`GET /v1/artifacts?project_id=...`).
* `test_unknown_slug_names_the_available_ones` likewise calls `artifacts.list()`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from artefaktum import Artefaktum, ArtefaktumError, AsyncArtefaktum, IntegrityError, NotFound
from artefaktum.models import Resolution

from .fixtures import ARTIFACT, DOWNLOAD, KEY, PROJECT, RUN, UPLOAD, VERSION

UUID_PROJECT = "01a0be89-1cde-74c1-ab17-8814cc9d9141"
PAGE = {"items": [ARTIFACT], "next_cursor": None}


def _ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200)


def _client(handler, storage=None, **kw) -> Artefaktum:
    return Artefaktum(
        api_key="ak",
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
        storage_transport=httpx.MockTransport(storage or _ok),
        **kw,
    )


def _aclient(handler, storage=None, **kw) -> AsyncArtefaktum:
    return AsyncArtefaktum(
        api_key="ak",
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
        storage_transport=httpx.MockTransport(storage or _ok),
        **kw,
    )


# -- project resolution ----------------------------------------------------------------


def test_default_project_slug_is_resolved_once_and_cached():
    calls: list[str] = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        assert req.url.params["project_id"] == "0199-p1"
        return httpx.Response(200, json=PAGE)

    c = _client(handler)
    c.artifacts.list()
    c.artifacts.list()
    assert calls.count("/v1/projects") == 1


def test_uuid_project_skips_resolution_and_override_is_not_cached():
    calls: list[str] = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/v1/projects":
            return httpx.Response(
                200, json={"items": [PROJECT, {**PROJECT, "id": "0199-p2", "slug": "other"}]}
            )
        return httpx.Response(200, json=PAGE)

    c = _client(handler, project=UUID_PROJECT)
    c.artifacts.list()
    assert "/v1/projects" not in calls

    c.artifacts.list(project="other")
    c.artifacts.list(project="other")
    assert calls.count("/v1/projects") == 2


def test_uuid_override_skips_resolution_too():
    calls: list[str] = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=PAGE)

    c = _client(handler)
    c.artifacts.list(project=UUID_PROJECT)
    assert calls == ["/v1/artifacts"]


def test_unknown_slug_names_the_available_ones():
    c = _client(lambda req: httpx.Response(200, json={"items": [PROJECT]}), project="nope")
    with pytest.raises(NotFound) as e:
        c.artifacts.list()
    assert "nope" in str(e.value) and "default" in str(e.value)
    assert e.value.code == "project_not_found"


def test_unscoped_operations_never_resolve_the_project():
    calls: list[str] = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json=ARTIFACT)

    c = _client(handler)
    assert c.artifacts.get("0199-a1").id == "0199-a1"
    assert calls == ["/v1/artifacts/0199-a1"]


# -- namespaces ------------------------------------------------------------------------


def test_add_relation_rejects_unknown_kind():
    c = _client(lambda req: httpx.Response(200, json={"items": [PROJECT]}))
    with pytest.raises(ValueError):
        c.artifacts.add_relation("a", "b", "friend_of")


def test_iter_all_follows_cursors():
    pages = {
        None: {"items": [ARTIFACT], "next_cursor": "c2"},
        "c2": {"items": [{**ARTIFACT, "id": "0199-a2"}], "next_cursor": None},
    }

    def handler(req):
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    assert [a.id for a in _client(handler).artifacts.iter_all()] == ["0199-a1", "0199-a2"]


def test_search_defaults_are_sent_as_filters():
    seen: dict = {}

    def handler(req):
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"items": [], "next_cursor": None, "mode": "hybrid"})

    page = _client(handler).artifacts.search("q3 churn")
    assert page.mode == "hybrid"
    body = seen["body"]
    assert body["project_id"] == "0199-p1"
    assert body["query"] == "q3 churn" and body["mode"] == "hybrid" and body["limit"] == 20
    assert body["filters"] == {"status": ["ready"], "exclude_superseded": True}


def test_resolve_sends_whole_seconds_for_max_age():
    from datetime import timedelta

    seen: dict = {}

    def handler(req):
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "hit", "artifact": ARTIFACT})

    c = _client(handler)
    c.artifacts.resolve(
        "k",
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=10,
        title="t",
        max_age=timedelta(hours=1, seconds=30),
    )
    assert seen["body"]["max_age_seconds"] == 3630

    c.artifacts.resolve(
        "k", filename="r.pdf", content_type="application/pdf", size_bytes=10, title="t", max_age=90
    )
    assert seen["body"]["max_age_seconds"] == 90


def test_fulfil_rejects_a_non_create_resolution():
    c = _client(lambda req: httpx.Response(200, json={"items": [PROJECT]}))
    res = Resolution.from_json({"status": "hit", "artifact": ARTIFACT})
    with pytest.raises(ValueError):
        c.artifacts.fulfil(res, b"x")


def test_runs_create_is_project_scoped_and_seal_is_not():
    bodies: list = []
    paths: list[str] = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        bodies.append(json.loads(req.content) if req.content else None)
        return httpx.Response(200, json=RUN)

    c = _client(handler)
    assert c.runs.create().id == "run-1"
    assert bodies[0] == {"project_id": "0199-p1"}
    c.runs.seal("run-1")
    assert paths == ["/v1/projects", "/v1/runs", "/v1/runs/run-1/seal"]


def test_keys_create_only_scopes_when_a_project_is_given():
    bodies: list = []

    def handler(req):
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"key": dict(KEY), "secret": "s"})

    c = _client(handler)
    c.keys.create("agent", ["artifacts:read"])
    assert "project_id" not in bodies[0]
    c.keys.create("agent", ["artifacts:read"], project="default")
    assert bodies[1]["project_id"] == "0199-p1"


def test_whoami_and_delete():
    paths: list[str] = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path == "/health/whoami":
            return httpx.Response(200, json={"tenant_id": "t1", "project_id": None})
        return httpx.Response(204)

    c = _client(handler)
    assert c.whoami().tenant_id == "t1"
    assert c.artifacts.delete("0199-a1") is None
    assert paths == ["/health/whoami", "/v1/artifacts/0199-a1"]


def test_context_manager_closes_both_http_clients():
    c = _client(lambda req: httpx.Response(200, json={"items": []}))
    with c:
        pass
    assert c._http.is_closed and c._storage.is_closed


def test_storage_client_carries_no_api_key():
    c = _client(lambda req: httpx.Response(200, json={"items": []}))
    assert "authorization" not in {k.lower() for k in c._storage.headers}
    assert c._http.headers["authorization"] == "Bearer ak"


async def test_async_client_same_surface():
    async with _aclient(
        lambda req: (
            httpx.Response(200, json={"items": [PROJECT]})
            if req.url.path == "/v1/projects"
            else httpx.Response(200, json=ARTIFACT)
        )
    ) as c:
        assert (await c.artifacts.get("a1")).id == "0199-a1"
        assert [p.slug for p in await c.projects.list()] == ["default"]


async def test_async_context_manager_closes_both_clients():
    c = _aclient(lambda req: httpx.Response(200, json={"items": []}))
    async with c:
        pass
    assert c._http.is_closed and c._storage.is_closed


async def test_async_iter_all_follows_cursors():
    pages = {
        None: {"items": [ARTIFACT], "next_cursor": "c2"},
        "c2": {"items": [{**ARTIFACT, "id": "0199-a2"}], "next_cursor": None},
    }

    def handler(req):
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    async with _aclient(handler) as c:
        assert [a.id async for a in c.artifacts.iter_all()] == ["0199-a1", "0199-a2"]


# -- push ------------------------------------------------------------------------------

PDF = b"%PDF-1.4 hello"
PDF_SHA = hashlib.sha256(PDF).hexdigest()
COMPLETE_PATH = "/v1/artifacts/0199-a1/versions/0199-v1/complete"


def _push_handlers(statuses: list[str]):
    """API + storage handlers for a push; returns them with the recording dicts."""
    paths: list[str] = []
    seen: dict = {}
    remaining = list(statuses)

    def api(req):
        paths.append(req.url.path)
        if req.url.path == "/v1/projects":
            return httpx.Response(200, json={"items": [PROJECT]})
        if req.url.path == "/v1/artifacts/uploads":
            seen["create"] = json.loads(req.content)
            return httpx.Response(200, json=UPLOAD)
        if req.url.path == COMPLETE_PATH:
            seen["complete"] = json.loads(req.content)
            return httpx.Response(200, json=UPLOAD["artifact"])
        return httpx.Response(200, json={**ARTIFACT, "status": remaining.pop(0)})

    def storage(req):
        seen["put_body"] = req.content
        seen["put_headers"] = {k.lower(): v for k, v in req.headers.items()}
        return httpx.Response(200)

    return api, storage, paths, seen


def _assert_push(paths, seen, *, gets: int):
    assert paths == [
        "/v1/projects",
        "/v1/artifacts/uploads",
        COMPLETE_PATH,
        *["/v1/artifacts/0199-a1"] * gets,
    ]
    assert seen["create"]["project_id"] == "0199-p1"
    assert seen["create"]["filename"] == "report.pdf"
    assert seen["create"]["content_type"] == "application/pdf"
    assert seen["create"]["size_bytes"] == len(PDF)
    assert seen["put_body"] == PDF
    assert seen["put_headers"]["content-type"] == "application/pdf"
    # S3 rejects a chunked PUT to a presigned URL with 501; both flavours must frame the
    # body with an explicit length and never fall back to Transfer-Encoding.
    assert seen["put_headers"]["content-length"] == str(len(PDF))
    assert "transfer-encoding" not in seen["put_headers"]
    assert "authorization" not in seen["put_headers"]
    assert seen["complete"] == {"sha256": PDF_SHA, "size_bytes": len(PDF)}


def test_push_uploads_completes_and_waits(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, storage, paths, seen = _push_handlers(["processing", "ready"])
    c = _client(api, storage)
    c.artifacts._interval = 0.0  # do not sleep for real between the two polls
    a = c.artifacts.push(src, title="Q3 churn", tags=["churn"], timeout=5.0)
    assert a.status == "ready"
    _assert_push(paths, seen, gets=2)


def test_push_without_wait_returns_after_one_get(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, storage, paths, seen = _push_handlers(["processing"])
    a = _client(api, storage).artifacts.push(src, title="Q3 churn", wait=False)
    assert a.status == "processing"
    _assert_push(paths, seen, gets=1)


async def test_async_push_uploads_completes_and_waits(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, storage, paths, seen = _push_handlers(["processing", "ready"])
    async with _aclient(api, storage) as c:
        c.artifacts._interval = 0.0
        a = await c.artifacts.push(src, title="Q3 churn", timeout=5.0)
    assert a.status == "ready"
    _assert_push(paths, seen, gets=2)


def test_push_from_bytes_needs_a_filename():
    c = _client(lambda req: httpx.Response(200, json={"items": [PROJECT]}))
    with pytest.raises(ValueError):
        c.artifacts.push(b"x", title="t")


VERSION_UPLOADS_PATH = "/v1/artifacts/0199-a1/uploads"
VERSION_PATHS = [VERSION_UPLOADS_PATH, COMPLETE_PATH, "/v1/artifacts/0199-a1"]
VERSION_BODY = {
    "filename": "report.pdf",
    "content_type": "application/pdf",
    "size_bytes": len(PDF),
    "summary": "v2",
    "infer_lineage": True,
}


def _version_handlers():
    paths: list[str] = []
    seen: dict = {}

    def api(req):
        paths.append(req.url.path)
        if req.url.path == VERSION_UPLOADS_PATH:
            seen["create"] = json.loads(req.content)
            return httpx.Response(200, json=UPLOAD)
        if req.url.path == COMPLETE_PATH:
            return httpx.Response(200, json=UPLOAD["artifact"])
        return httpx.Response(200, json=ARTIFACT)

    return api, paths, seen


def test_create_version_posts_to_the_artifact_uploads_route(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, paths, seen = _version_handlers()
    a = _client(api).artifacts.create_version("0199-a1", src, summary="v2", wait=False)
    assert a.id == "0199-a1"
    assert paths == VERSION_PATHS
    assert seen["create"] == VERSION_BODY


async def test_async_create_version_posts_to_the_artifact_uploads_route(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, paths, seen = _version_handlers()
    async with _aclient(api) as c:
        a = await c.artifacts.create_version("0199-a1", src, summary="v2", wait=False)
    assert a.id == "0199-a1"
    assert paths == VERSION_PATHS
    assert seen["create"] == VERSION_BODY


CREATE_RESOLUTION = {
    "status": "create",
    "reservation": UPLOAD["artifact"],
    "upload": UPLOAD["upload"],
}


def _fulfil_handlers():
    paths: list[str] = []

    def api(req):
        paths.append(req.url.path)
        if req.url.path == COMPLETE_PATH:
            return httpx.Response(200, json=UPLOAD["artifact"])
        return httpx.Response(200, json=ARTIFACT)

    return api, paths


def test_fulfil_puts_to_the_reservation_upload(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, paths = _fulfil_handlers()
    a = _client(api).artifacts.fulfil(Resolution.from_json(CREATE_RESOLUTION), src, wait=False)
    assert a.id == "0199-a1"
    assert paths == [COMPLETE_PATH, "/v1/artifacts/0199-a1"]


async def test_async_fulfil_puts_to_the_reservation_upload(tmp_path: Path):
    src = tmp_path / "report.pdf"
    src.write_bytes(PDF)
    api, paths = _fulfil_handlers()
    async with _aclient(api) as c:
        a = await c.artifacts.fulfil(Resolution.from_json(CREATE_RESOLUTION), src, wait=False)
    assert a.id == "0199-a1"
    assert paths == [COMPLETE_PATH, "/v1/artifacts/0199-a1"]


def test_fulfil_accepts_a_bytes_source_with_a_filename():
    api, paths = _fulfil_handlers()
    a = _client(api).artifacts.fulfil(
        Resolution.from_json(CREATE_RESOLUTION), b'{"k": 1}', filename="x.json", wait=False
    )
    assert a.id == "0199-a1"
    assert paths == [COMPLETE_PATH, "/v1/artifacts/0199-a1"]


async def test_async_fulfil_accepts_a_bytes_source_with_a_filename():
    api, paths = _fulfil_handlers()
    async with _aclient(api) as c:
        a = await c.artifacts.fulfil(
            Resolution.from_json(CREATE_RESOLUTION), b'{"k": 1}', filename="x.json", wait=False
        )
    assert a.id == "0199-a1"
    assert paths == [COMPLETE_PATH, "/v1/artifacts/0199-a1"]


# -- pull ------------------------------------------------------------------------------

BODY = b"the real bytes"
BODY_SHA = hashlib.sha256(BODY).hexdigest()


def _pull_handlers(sha: str | None, body: bytes = BODY):
    paths: list[str] = []
    version = {**VERSION, "sha256": sha}

    def api(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/download"):
            return httpx.Response(200, json=DOWNLOAD)
        if req.url.path.endswith("/versions"):
            return httpx.Response(200, json=[version])
        return httpx.Response(200, json={**ARTIFACT, "latest_version": version})

    def storage(req):
        paths.append("STORAGE " + req.method)
        return httpx.Response(200, content=body)

    return api, storage, paths


def test_pull_writes_the_file_and_verifies_the_digest(tmp_path: Path):
    api, storage, paths = _pull_handlers(BODY_SHA)
    out = _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert out == tmp_path / "report.pdf"
    assert out.read_bytes() == BODY
    assert paths == [
        "/v1/artifacts/0199-a1/download",
        "/v1/artifacts/0199-a1/versions",
        "STORAGE GET",
    ]


def test_pull_raises_on_a_digest_mismatch(tmp_path: Path):
    api, storage, _ = _pull_handlers("0" * 64)
    with pytest.raises(IntegrityError):
        _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_pull_with_verify_false_skips_the_check(tmp_path: Path):
    api, storage, _ = _pull_handlers("0" * 64)
    out = _client(api, storage).artifacts.pull("0199-a1", tmp_path, verify=False)
    assert out.read_bytes() == BODY


def test_pull_to_an_explicit_file_path(tmp_path: Path):
    api, storage, _ = _pull_handlers(BODY_SHA)
    dest = tmp_path / "saved.pdf"
    out = _client(api, storage).artifacts.pull("0199-a1", dest)
    assert out == dest and dest.read_bytes() == BODY


async def test_async_pull_writes_and_verifies(tmp_path: Path):
    api, storage, paths = _pull_handlers(BODY_SHA)
    async with _aclient(api, storage) as c:
        out = await c.artifacts.pull("0199-a1", tmp_path)
    assert out.read_bytes() == BODY
    assert paths == [
        "/v1/artifacts/0199-a1/download",
        "/v1/artifacts/0199-a1/versions",
        "STORAGE GET",
    ]


def test_pull_never_sends_the_api_key_to_storage(tmp_path: Path):
    seen: dict = {}

    def api(req):
        if req.url.path.endswith("/download"):
            return httpx.Response(200, json=DOWNLOAD)
        return httpx.Response(200, json=[{**VERSION, "sha256": None}])

    def storage(req):
        seen["headers"] = {k.lower() for k in req.headers}
        return httpx.Response(200, content=BODY)

    _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert "authorization" not in seen["headers"]


def test_pull_creates_a_destination_written_with_a_trailing_separator(tmp_path: Path):
    # `Path("out/")` drops the separator and `is_dir()` is false for a directory that
    # does not exist yet, so the bytes would otherwise land in a *file* called `out`.
    api, storage, _ = _pull_handlers(BODY_SHA)
    dest = str(tmp_path / "out") + "/"
    out = _client(api, storage).artifacts.pull("0199-a1", dest)
    assert (tmp_path / "out").is_dir()
    assert out == tmp_path / "out" / "report.pdf"
    assert out.read_bytes() == BODY


def test_pull_creates_intermediate_directories_for_a_trailing_separator(tmp_path: Path):
    api, storage, _ = _pull_handlers(BODY_SHA)
    dest = str(tmp_path / "a" / "b") + "/"
    out = _client(api, storage).artifacts.pull("0199-a1", dest)
    assert out == tmp_path / "a" / "b" / "report.pdf"


async def test_async_pull_creates_a_destination_written_with_a_trailing_separator(tmp_path: Path):
    api, storage, _ = _pull_handlers(BODY_SHA)
    dest = str(tmp_path / "out") + "/"
    async with _aclient(api, storage) as c:
        out = await c.artifacts.pull("0199-a1", dest)
    assert out == tmp_path / "out" / "report.pdf"
    assert out.read_bytes() == BODY


def test_pull_without_a_trailing_separator_still_means_a_file(tmp_path: Path):
    api, storage, _ = _pull_handlers(BODY_SHA)
    out = _client(api, storage).artifacts.pull("0199-a1", tmp_path / "exact.bin")
    assert out == tmp_path / "exact.bin" and out.read_bytes() == BODY


# -- pull names the version it signed, not whatever is latest by the time it looks ------

OLD_VERSION = {
    **VERSION,
    "id": "0199-v1",
    "version_number": 1,
    "original_filename": "old.pdf",
    "sha256": BODY_SHA,
}
NEW_VERSION = {
    **VERSION,
    "id": "0199-v2",
    "version_number": 2,
    "original_filename": "new.pdf",
    "sha256": "0" * 64,
}


def _racing_handlers(versions: list[dict], latest: dict | None):
    """`download_url` signs v1; a new version lands before `pull` reads the metadata."""

    def api(req):
        if req.url.path.endswith("/download"):
            return httpx.Response(200, json=DOWNLOAD)  # version_id "0199-v1"
        if req.url.path.endswith("/versions"):
            return httpx.Response(200, json=versions)
        return httpx.Response(200, json={**ARTIFACT, "latest_version": latest})

    def storage(req):
        return httpx.Response(200, content=BODY)

    return api, storage


def test_pull_uses_the_signed_version_not_the_newest_one(tmp_path: Path):
    api, storage = _racing_handlers([NEW_VERSION, OLD_VERSION], NEW_VERSION)
    out = _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert out == tmp_path / "old.pdf"  # the bytes are v1's, so the name must be too
    assert out.read_bytes() == BODY  # and v1's digest is what was checked


async def test_async_pull_uses_the_signed_version_not_the_newest_one(tmp_path: Path):
    api, storage = _racing_handlers([NEW_VERSION, OLD_VERSION], NEW_VERSION)
    async with _aclient(api, storage) as c:
        out = await c.artifacts.pull("0199-a1", tmp_path)
    assert out == tmp_path / "old.pdf" and out.read_bytes() == BODY


def test_pull_falls_back_to_latest_version_when_the_list_omits_the_signed_one(tmp_path: Path):
    api, storage = _racing_handlers([NEW_VERSION], OLD_VERSION)
    out = _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert out == tmp_path / "old.pdf"


def test_pull_refuses_when_no_version_matches_the_signed_download(tmp_path: Path):
    api, storage = _racing_handlers([NEW_VERSION], NEW_VERSION)
    with pytest.raises(ArtefaktumError) as e:
        _client(api, storage).artifacts.pull("0199-a1", tmp_path)
    assert e.value.code == "version_mismatch"
    assert list(tmp_path.iterdir()) == []


async def test_async_pull_refuses_when_no_version_matches_the_signed_download(tmp_path: Path):
    api, storage = _racing_handlers([NEW_VERSION], None)
    async with _aclient(api, storage) as c:
        with pytest.raises(ArtefaktumError) as e:
            await c.artifacts.pull("0199-a1", tmp_path)
    assert e.value.code == "version_mismatch"


def test_quota():
    def handler(req):
        assert req.url.path == "/v1/quota"
        return httpx.Response(
            200,
            json={
                "plan": "pro",
                "storage": {"used_bytes": 1, "limit_bytes": 2},
                "calls": {"used": 3, "limit": 4, "resets_at": "2026-10-01T00:00:00Z"},
                "max_file_bytes": 5,
            },
        )

    q = _client(handler).quota()
    assert q.plan == "pro" and q.storage.limit_bytes == 2 and q.calls.limit == 4

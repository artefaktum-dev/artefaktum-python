"""Every operation: request built right, response parsed right (design spec §4, §10)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from artefaktum import _ops as ops
from artefaktum.models import (
    ApiKey,
    Artifact,
    ArtifactRef,
    CreatedKey,
    Download,
    Identity,
    Page,
    Project,
    Relation,
    Run,
    SearchPage,
    UploadTicket,
    Version,
)

from .fixtures import ARTIFACT, DOWNLOAD, KEY, PROJECT, RELATION, RUN, UPLOAD, USAGE


def test_create_upload_builds_body_and_parses_ticket():
    r = ops.create_upload.build(
        project_id="p1",
        filename="a.json",
        content_type="application/json",
        size_bytes=3,
        title="t",
        description="d",
        tags=["x"],
        metadata={"k": 1},
        external_key="ek",
        expires_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        summary=None,
        run_id=None,
        infer_lineage=True,
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/uploads")
    assert r.json == {
        "project_id": "p1",
        "filename": "a.json",
        "content_type": "application/json",
        "size_bytes": 3,
        "title": "t",
        "description": "d",
        "tags": ["x"],
        "metadata": {"k": 1},
        "external_key": "ek",
        "expires_at": "2026-10-01T00:00:00+00:00",
        "infer_lineage": True,
    }  # None values (summary, run_id) omitted
    assert ops.create_upload.parse(UPLOAD).upload.method == "PUT"
    assert ops.create_upload.idempotent is False


def test_get_by_external_key_percent_encodes_slashes():
    r = ops.get_by_external_key.build(project_id="p1", external_key="vendor/x/2026-09")
    assert r.method == "GET"
    assert r.path == "/v1/artifacts/by-external-key/" + quote("vendor/x/2026-09", safe="")
    assert r.params == {"project_id": "p1"}
    assert ops.get_by_external_key.idempotent is True
    assert isinstance(ops.get_by_external_key.parse(ARTIFACT), Artifact)


def test_list_artifacts_sends_only_given_filters_and_repeats_status():
    r = ops.list_artifacts.build(
        project_id="p1",
        status=["ready", "processing"],
        tag="x",
        limit=10,
        cursor=None,
        content_type=None,
        external_key=None,
        created_before=None,
        created_after=None,
        expires_before=None,
    )
    assert r.method == "GET" and r.path == "/v1/artifacts"
    assert r.params == {
        "project_id": "p1",
        "status": ["ready", "processing"],
        "tag": "x",
        "limit": 10,
    }
    page = ops.list_artifacts.parse({"items": [ARTIFACT], "next_cursor": None})
    assert isinstance(page, Page) and isinstance(page.items[0], Artifact)
    assert ops.list_artifacts.idempotent is True


def test_search_nests_filters():
    r = ops.search.build(
        project_id="p1",
        query="q",
        mode="hybrid",
        limit=5,
        cursor=None,
        content_types=[],
        tags_all=["a"],
        status=["ready"],
        external_key=None,
        created_after=None,
        created_before=None,
        exclude_superseded=False,
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/search")
    assert r.json == {
        "project_id": "p1",
        "query": "q",
        "mode": "hybrid",
        "limit": 5,
        "filters": {
            "content_types": [],
            "tags_all": ["a"],
            "status": ["ready"],
            "exclude_superseded": False,
        },
    }
    sp = ops.search.parse(
        {
            "items": [{"artifact": ARTIFACT, "score": 0.9, "match_mode": "hybrid"}],
            "next_cursor": None,
            "mode": "hybrid",
        }
    )
    assert isinstance(sp, SearchPage) and sp.items[0].score == 0.9
    assert ops.search.idempotent is True


def test_resolve_and_usage_and_delete():
    r = ops.resolve.build(
        project_id="p1",
        external_key="k",
        max_age_seconds=60,
        filename="f",
        content_type="c",
        size_bytes=1,
        title="t",
        description="",
        tags=[],
        metadata={},
        run_id=None,
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/resolve")
    assert r.json == {
        "project_id": "p1",
        "external_key": "k",
        "max_age_seconds": 60,
        "filename": "f",
        "content_type": "c",
        "size_bytes": 1,
        "title": "t",
        "description": "",
        "tags": [],
        "metadata": {},
    }  # run_id=None omitted
    assert (
        ops.resolve.parse({"status": "pending", "retry_after_seconds": 2}).retry_after_seconds == 2
    )
    assert ops.resolve.idempotent is False

    u = ops.get_usage.build(
        start=None,
        end=None,
        granularity="day",
        metric="requests",
        project_id=None,
        principal_id=None,
    )
    assert (u.method, u.path) == ("GET", "/v1/usage")
    assert u.params == {"granularity": "day", "metric": "requests"}
    assert ops.get_usage.idempotent is True
    assert [p.value for p in ops.get_usage.parse(USAGE)] == [40]

    # NOTE: the brief's snippet expects delete_artifact.build(project_id=..., artifact_id=...)
    # with params == {"project_id": "p1"}. The actual route
    # (src/artifact/api/routes/artifacts.py delete_artifact, line 213-220) takes only
    # artifact_id -- no project_id query parameter exists. Following the route: delete_artifact
    # takes only artifact_id and sends no params, matching design spec §5 `delete(artifact_id)`.
    d = ops.delete_artifact.build(artifact_id="a1")
    assert (d.method, d.path, d.params) == ("DELETE", "/v1/artifacts/a1", None)
    assert ops.delete_artifact.parse(None) is None
    assert ops.delete_artifact.idempotent is False


def test_only_reads_are_marked_idempotent():
    reads = {o.name for o in ops.ALL if o.idempotent}
    assert reads == {
        "whoami",
        "list_projects",
        "get_artifact",
        "get_by_external_key",
        "list_artifacts",
        "search",
        "download_url",
        "list_relations",
        "list_versions",
        "run_artifacts",
        "list_keys",
        "get_usage",
        "get_quota",
    }


def test_all_lists_every_operation_exactly_once():
    names = [o.name for o in ops.ALL]
    assert len(names) == len(set(names)) == 24


def test_complete_upload_omits_none_body_fields():
    r = ops.complete_upload.build(
        artifact_id="a1", version_id="v1", sha256="abc", size_bytes=None, etag=None
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/a1/versions/v1/complete")
    assert r.json == {"sha256": "abc"}
    assert isinstance(
        ops.complete_upload.parse({"id": "a1", "version_id": "v1", "status": "processing"}),
        ArtifactRef,
    )
    assert ops.complete_upload.idempotent is False


def test_get_artifact_has_no_project_query():
    r = ops.get_artifact.build(artifact_id="a1")
    assert (r.method, r.path, r.params) == ("GET", "/v1/artifacts/a1", None)
    assert isinstance(ops.get_artifact.parse(ARTIFACT), Artifact)
    assert ops.get_artifact.idempotent is True


def test_update_artifact_sends_only_given_fields_and_clear_flag_only_when_true():
    r = ops.update_artifact.build(
        artifact_id="a1",
        title="new",
        description=None,
        tags=None,
        metadata=None,
        expires_at=None,
        clear_expires_at=False,
    )
    assert (r.method, r.path) == ("PATCH", "/v1/artifacts/a1")
    assert r.json == {"title": "new"}

    r2 = ops.update_artifact.build(
        artifact_id="a1",
        title=None,
        description=None,
        tags=None,
        metadata=None,
        expires_at=None,
        clear_expires_at=True,
    )
    assert r2.json == {"clear_expires_at": True}
    assert isinstance(ops.update_artifact.parse(ARTIFACT), Artifact)
    assert ops.update_artifact.idempotent is False


def test_create_version_upload_posts_to_artifact_uploads():
    r = ops.create_version_upload.build(
        artifact_id="a1",
        filename="b.json",
        content_type="application/json",
        size_bytes=4,
        summary=None,
        run_id=None,
        infer_lineage=True,
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/a1/uploads")
    assert r.json == {
        "filename": "b.json",
        "content_type": "application/json",
        "size_bytes": 4,
        "infer_lineage": True,
    }
    assert isinstance(ops.create_version_upload.parse(UPLOAD), UploadTicket)
    assert ops.create_version_upload.idempotent is False


def test_add_relation_body_and_optional_fields():
    r = ops.add_relation.build(
        artifact_id="a1",
        to_artifact_id="a0",
        relation_type="derived_from",
        to_version_id=None,
        metadata=None,
    )
    assert (r.method, r.path) == ("POST", "/v1/artifacts/a1/relations")
    assert r.json == {"to_artifact_id": "a0", "relation_type": "derived_from"}
    assert isinstance(ops.add_relation.parse(RELATION), Relation)
    assert ops.add_relation.idempotent is False


def test_list_relations_and_list_versions_take_only_artifact_id():
    r = ops.list_relations.build(artifact_id="a1")
    assert (r.method, r.path, r.params) == ("GET", "/v1/artifacts/a1/relations", None)
    assert ops.list_relations.parse([RELATION]) == [Relation.from_json(RELATION)]
    assert ops.list_relations.idempotent is True

    v = ops.list_versions.build(artifact_id="a1")
    assert (v.method, v.path, v.params) == ("GET", "/v1/artifacts/a1/versions", None)
    version_json = ARTIFACT["latest_version"]
    assert ops.list_versions.parse([version_json]) == [Version.from_json(version_json)]
    assert ops.list_versions.idempotent is True


def test_download_url_has_no_project_query_but_has_version_and_run():
    r = ops.download_url.build(artifact_id="a1", version_id="v1", run_id="run1")
    assert (r.method, r.path) == ("GET", "/v1/artifacts/a1/download")
    assert r.params == {"version_id": "v1", "run_id": "run1"}
    assert isinstance(ops.download_url.parse(DOWNLOAD), Download)
    assert ops.download_url.idempotent is True


def test_create_run_body():
    r = ops.create_run.build(project_id="p1", run_id=None)
    assert (r.method, r.path) == ("POST", "/v1/runs")
    assert r.json == {"project_id": "p1"}
    assert isinstance(ops.create_run.parse(RUN), Run)
    assert ops.create_run.idempotent is False


def test_seal_run_posts_with_no_body():
    r = ops.seal_run.build(run_id="run-1")
    assert (r.method, r.path, r.json) == ("POST", "/v1/runs/run-1/seal", None)
    assert isinstance(ops.seal_run.parse(RUN), Run)
    assert ops.seal_run.idempotent is False


def test_run_artifacts_has_no_project_query():
    # NOTE: the brief lists run_artifacts as taking project_id, limit, cursor. The actual
    # route (src/artifact/api/routes/runs.py list_run_artifacts, line 32-40) takes only
    # limit and cursor -- no project_id parameter. Design spec §5 `runs.artifacts(run_id,
    # limit=50, cursor=None)` agrees with the route, so project_id is dropped here.
    r = ops.run_artifacts.build(run_id="run-1", limit=10, cursor="c1")
    assert (r.method, r.path) == ("GET", "/v1/runs/run-1/artifacts")
    assert r.params == {"limit": 10, "cursor": "c1"}
    page = ops.run_artifacts.parse({"items": [ARTIFACT], "next_cursor": None})
    assert isinstance(page, Page) and isinstance(page.items[0], Artifact)
    assert ops.run_artifacts.idempotent is True


def test_create_key_list_keys_revoke_key():
    r = ops.create_key.build(
        name="agent", scopes=["artifacts:read"], project_id=None, expires_at=None
    )
    assert (r.method, r.path) == ("POST", "/v1/api-keys")
    assert r.json == {"name": "agent", "scopes": ["artifacts:read"]}
    assert isinstance(ops.create_key.parse({"key": KEY, "secret": "s3cr3t"}), CreatedKey)
    assert ops.create_key.idempotent is False

    lr = ops.list_keys.build()
    assert (lr.method, lr.path, lr.params) == ("GET", "/v1/api-keys", None)
    assert ops.list_keys.parse([KEY]) == [ApiKey.from_json(KEY)]
    assert ops.list_keys.idempotent is True

    rr = ops.revoke_key.build(key_id="k2")
    assert (rr.method, rr.path) == ("DELETE", "/v1/api-keys/k2")
    assert isinstance(ops.revoke_key.parse(KEY), ApiKey)
    assert ops.revoke_key.idempotent is False


def test_whoami():
    r = ops.whoami.build()
    assert (r.method, r.path, r.params, r.json) == ("GET", "/health/whoami", None, None)
    identity = ops.whoami.parse({"tenant_id": "t1", "project_id": "p1"})
    assert isinstance(identity, Identity) and identity.tenant_id == "t1"
    assert ops.whoami.idempotent is True


def test_list_projects():
    r = ops.list_projects.build()
    assert (r.method, r.path, r.params) == ("GET", "/v1/projects", None)
    projects = ops.list_projects.parse({"items": [PROJECT]})
    assert projects == [Project.from_json(PROJECT)]
    assert ops.list_projects.idempotent is True


def test_get_quota():
    r = ops.get_quota.build()
    assert (r.method, r.path, r.params, r.json) == ("GET", "/v1/quota", None, None)
    q = ops.get_quota.parse(
        {
            "plan": "free",
            "storage": {"used_bytes": 5, "limit_bytes": 1073741824},
            "calls": {"used": 8421, "limit": 10000, "resets_at": "2026-10-01T00:00:00Z"},
            "max_file_bytes": 104857600,
        }
    )
    assert q.plan == "free" and q.max_file_bytes == 104857600
    assert (q.storage.used_bytes, q.storage.limit_bytes) == (5, 1073741824)
    assert (q.calls.used, q.calls.limit) == (8421, 10000)
    assert q.calls.resets_at.isoformat() == "2026-10-01T00:00:00+00:00"

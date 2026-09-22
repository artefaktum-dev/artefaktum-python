"""The sans-I/O operation table (design spec §4): one `Operation` per API call.

`build(**kwargs) -> Request` turns keyword arguments into a method/path/params/json
triple; `parse(json) -> T` turns the decoded response body into a model. No I/O happens
here -- `_transport.py` (a later task) is the only place that touches httpx, and both the
sync and async client shells share this table so they cannot drift from each other.

Every path here was checked against its route in `src/artifact/api/routes/*.py` and its
request/response schema in `src/artifact/services/models.py`; see the task report for the
line-by-line mapping and the two discrepancies against the task brief (`delete_artifact`
and `run_artifacts` are not project-scoped -- their routes take no `project_id`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar
from urllib.parse import quote

from . import models as m

T = TypeVar("T")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: dict[str, Any] | None = None


@dataclass(frozen=True)
class Operation(Generic[T]):
    name: str
    build: Callable[..., Request]
    parse: Callable[[Any], T]
    idempotent: bool


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so the server applies its own defaults."""
    return {k: v for k, v in d.items() if v is not None}


def _q(value: str) -> str:
    """Percent-encode a path segment; `external_key` and similar may contain '/'."""
    return quote(str(value), safe="")


def _none(_: Any) -> None:
    return None


def _parse_projects(d: Any) -> list[m.Project]:
    return [m.Project.from_json(x) for x in d["items"]]


def _parse_artifact_page(d: Any) -> m.Page[m.Artifact]:
    return m.Page.from_json(d, m.Artifact.from_json)


def _parse_relations(d: Any) -> list[m.Relation]:
    return [m.Relation.from_json(x) for x in d]


def _parse_versions(d: Any) -> list[m.Version]:
    return [m.Version.from_json(x) for x in d]


def _parse_keys(d: Any) -> list[m.ApiKey]:
    return [m.ApiKey.from_json(x) for x in d]


def _parse_usage(d: Any) -> list[m.UsagePoint]:
    return [m.UsagePoint.from_json(x) for x in d["items"]]


# -- health -------------------------------------------------------------------------
# src/artifact/api/routes/health.py:30 GET /health/whoami


def _whoami() -> Request:
    return Request("GET", "/health/whoami")


whoami: Operation[m.Identity] = Operation("whoami", _whoami, m.Identity.from_json, True)


# -- projects -----------------------------------------------------------------------
# src/artifact/api/routes/projects.py:16 GET /v1/projects (prefix line 11)


def _list_projects() -> Request:
    return Request("GET", "/v1/projects")


list_projects: Operation[list[m.Project]] = Operation(
    "list_projects", _list_projects, _parse_projects, True
)


# -- artifacts ------------------------------------------------------------------------
# src/artifact/api/routes/artifacts.py:40 prefix /v1/artifacts


def _create_upload(
    *,
    project_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    external_key: str | None = None,
    expires_at: datetime | None = None,
    summary: str | None = None,
    run_id: str | None = None,
    infer_lineage: bool = True,
) -> Request:
    # routes/artifacts.py:49 POST /v1/artifacts/uploads; body CreateUploadRequest
    # (services/models.py:12-25)
    return Request(
        "POST",
        "/v1/artifacts/uploads",
        json=_clean(
            {
                "project_id": project_id,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "title": title,
                "description": description,
                "tags": list(tags or []),
                "metadata": dict(metadata or {}),
                "external_key": external_key,
                "expires_at": _iso(expires_at),
                "summary": summary,
                "run_id": run_id,
                "infer_lineage": infer_lineage,
            }
        ),
    )


create_upload: Operation[m.UploadTicket] = Operation(
    "create_upload", _create_upload, m.UploadTicket.from_json, False
)


def _complete_upload(
    *,
    artifact_id: str,
    version_id: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
    etag: str | None = None,
) -> Request:
    # routes/artifacts.py:61 POST /v1/artifacts/{artifact_id}/versions/{version_id}/complete;
    # body CompleteUploadRequest (services/models.py:58-62), all fields optional
    return Request(
        "POST",
        f"/v1/artifacts/{_q(artifact_id)}/versions/{_q(version_id)}/complete",
        json=_clean({"sha256": sha256, "size_bytes": size_bytes, "etag": etag}),
    )


complete_upload: Operation[m.ArtifactRef] = Operation(
    "complete_upload", _complete_upload, m.ArtifactRef.from_json, False
)


def _get_artifact(*, artifact_id: str) -> Request:
    # routes/artifacts.py:116 GET /v1/artifacts/{artifact_id} -- no project_id query param
    return Request("GET", f"/v1/artifacts/{_q(artifact_id)}")


get_artifact: Operation[m.Artifact] = Operation(
    "get_artifact", _get_artifact, m.Artifact.from_json, True
)


def _get_by_external_key(*, project_id: str, external_key: str) -> Request:
    # routes/artifacts.py:106 GET /v1/artifacts/by-external-key/{external_key:path};
    # project_id IS a required query param here
    return Request(
        "GET",
        f"/v1/artifacts/by-external-key/{_q(external_key)}",
        params={"project_id": project_id},
    )


get_by_external_key: Operation[m.Artifact] = Operation(
    "get_by_external_key", _get_by_external_key, m.Artifact.from_json, True
)


def _list_artifacts(
    *,
    project_id: str,
    status: list[str] | None = None,
    content_type: str | None = None,
    tag: str | None = None,
    external_key: str | None = None,
    created_before: datetime | None = None,
    created_after: datetime | None = None,
    expires_before: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Request:
    # routes/artifacts.py:74 GET /v1/artifacts; project_id required query param;
    # `status` is Query(alias="status") accepting a repeated list -- passed through as-is
    return Request(
        "GET",
        "/v1/artifacts",
        params=_clean(
            {
                "project_id": project_id,
                "status": status,
                "content_type": content_type,
                "tag": tag,
                "external_key": external_key,
                "created_before": _iso(created_before),
                "created_after": _iso(created_after),
                "expires_before": _iso(expires_before),
                "limit": limit,
                "cursor": cursor,
            }
        ),
    )


list_artifacts: Operation[m.Page[m.Artifact]] = Operation(
    "list_artifacts", _list_artifacts, _parse_artifact_page, True
)


def _search(
    *,
    project_id: str,
    query: str = "",
    mode: str = "hybrid",
    limit: int = 20,
    cursor: str | None = None,
    content_types: list[str] | None = None,
    tags_all: list[str] | None = None,
    status: list[str] | None = None,
    external_key: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    exclude_superseded: bool | None = None,
) -> Request:
    # routes/artifacts.py:192 POST /v1/artifacts/search; body SearchRequest nests
    # SearchFilters (services/models.py:167-193)
    filters = _clean(
        {
            "content_types": content_types,
            "tags_all": tags_all,
            "status": status,
            "external_key": external_key,
            "created_after": _iso(created_after),
            "created_before": _iso(created_before),
            "exclude_superseded": exclude_superseded,
        }
    )
    return Request(
        "POST",
        "/v1/artifacts/search",
        json=_clean(
            {
                "project_id": project_id,
                "query": query,
                "mode": mode,
                "limit": limit,
                "cursor": cursor,
                "filters": filters,
            }
        ),
    )


# POST only because the filter body is too large for a query string; it has no side
# effects, so it is retried like any other read.
search: Operation[m.SearchPage] = Operation("search", _search, m.SearchPage.from_json, True)


def _resolve(
    *,
    project_id: str,
    external_key: str,
    max_age_seconds: int | None = None,
    filename: str,
    content_type: str,
    size_bytes: int,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> Request:
    # routes/artifacts.py:202 POST /v1/artifacts/resolve; body ResolveRequest
    # (services/models.py:196-208)
    return Request(
        "POST",
        "/v1/artifacts/resolve",
        json=_clean(
            {
                "project_id": project_id,
                "external_key": external_key,
                "max_age_seconds": max_age_seconds,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "title": title,
                "description": description,
                "tags": list(tags or []),
                "metadata": dict(metadata or {}),
                "run_id": run_id,
            }
        ),
    )


resolve: Operation[m.Resolution] = Operation("resolve", _resolve, m.Resolution.from_json, False)


def _download_url(
    *, artifact_id: str, version_id: str | None = None, run_id: str | None = None
) -> Request:
    # routes/artifacts.py:130 GET /v1/artifacts/{artifact_id}/download -- no project_id
    # query param; version_id and run_id are optional query params
    return Request(
        "GET",
        f"/v1/artifacts/{_q(artifact_id)}/download",
        params=_clean({"version_id": version_id, "run_id": run_id}),
    )


download_url: Operation[m.Download] = Operation(
    "download_url", _download_url, m.Download.from_json, True
)


def _update_artifact(
    *,
    artifact_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    clear_expires_at: bool = False,
) -> Request:
    # routes/artifacts.py:145 PATCH /v1/artifacts/{artifact_id}; body UpdateMetadataRequest
    # (services/models.py:94-101); clear_expires_at only sent when True
    body = _clean(
        {
            "title": title,
            "description": description,
            "tags": tags,
            "metadata": metadata,
            "expires_at": _iso(expires_at),
        }
    )
    if clear_expires_at:
        body["clear_expires_at"] = True
    return Request("PATCH", f"/v1/artifacts/{_q(artifact_id)}", json=body)


update_artifact: Operation[m.Artifact] = Operation(
    "update_artifact", _update_artifact, m.Artifact.from_json, False
)


def _create_version_upload(
    *,
    artifact_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    summary: str | None = None,
    run_id: str | None = None,
    infer_lineage: bool = True,
) -> Request:
    # routes/artifacts.py:155 POST /v1/artifacts/{artifact_id}/uploads; body
    # CreateVersionRequest (services/models.py:28-34) -- no project_id, no title/tags/
    # metadata/external_key (those belong only to a new artifact, not a new version)
    return Request(
        "POST",
        f"/v1/artifacts/{_q(artifact_id)}/uploads",
        json=_clean(
            {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "summary": summary,
                "run_id": run_id,
                "infer_lineage": infer_lineage,
            }
        ),
    )


create_version_upload: Operation[m.UploadTicket] = Operation(
    "create_version_upload", _create_version_upload, m.UploadTicket.from_json, False
)


def _add_relation(
    *,
    artifact_id: str,
    to_artifact_id: str,
    relation_type: str,
    to_version_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Request:
    # routes/artifacts.py:171 POST /v1/artifacts/{artifact_id}/relations; body
    # CreateRelationRequest (services/models.py:139-144)
    return Request(
        "POST",
        f"/v1/artifacts/{_q(artifact_id)}/relations",
        json=_clean(
            {
                "to_artifact_id": to_artifact_id,
                "relation_type": relation_type,
                "to_version_id": to_version_id,
                "metadata": metadata,
            }
        ),
    )


add_relation: Operation[m.Relation] = Operation(
    "add_relation", _add_relation, m.Relation.from_json, False
)


def _list_relations(*, artifact_id: str) -> Request:
    # routes/artifacts.py:185 GET /v1/artifacts/{artifact_id}/relations; response is a
    # plain list[RelationView], not wrapped in "items"
    return Request("GET", f"/v1/artifacts/{_q(artifact_id)}/relations")


list_relations: Operation[list[m.Relation]] = Operation(
    "list_relations", _list_relations, _parse_relations, True
)


def _list_versions(*, artifact_id: str) -> Request:
    # routes/artifacts.py:123 GET /v1/artifacts/{artifact_id}/versions; response is a
    # plain list[VersionView]
    return Request("GET", f"/v1/artifacts/{_q(artifact_id)}/versions")


list_versions: Operation[list[m.Version]] = Operation(
    "list_versions", _list_versions, _parse_versions, True
)


def _delete_artifact(*, artifact_id: str) -> Request:
    # routes/artifacts.py:213 DELETE /v1/artifacts/{artifact_id} -- no project_id query
    # param on this route (differs from the task brief's example, which expected
    # project_id in params; the route is the source of truth here, matching design
    # spec §5 `delete(artifact_id) -> None`)
    return Request("DELETE", f"/v1/artifacts/{_q(artifact_id)}")


delete_artifact: Operation[None] = Operation("delete_artifact", _delete_artifact, _none, False)


# -- runs -----------------------------------------------------------------------------
# src/artifact/api/routes/runs.py prefix /v1/runs (line 11)


def _create_run(*, project_id: str, run_id: str | None = None) -> Request:
    # routes/runs.py:16 POST /v1/runs; body CreateRunRequest (services/models.py:125-128)
    return Request("POST", "/v1/runs", json=_clean({"project_id": project_id, "run_id": run_id}))


create_run: Operation[m.Run] = Operation("create_run", _create_run, m.Run.from_json, False)


def _seal_run(*, run_id: str) -> Request:
    # routes/runs.py:25 POST /v1/runs/{run_id}/seal; no request body
    return Request("POST", f"/v1/runs/{_q(run_id)}/seal")


seal_run: Operation[m.Run] = Operation("seal_run", _seal_run, m.Run.from_json, False)


def _run_artifacts(*, run_id: str, limit: int = 50, cursor: str | None = None) -> Request:
    # routes/runs.py:32 GET /v1/runs/{run_id}/artifacts -- takes only limit and cursor;
    # no project_id query param (differs from the task brief; design spec §5
    # `runs.artifacts(run_id, limit=50, cursor=None)` agrees with the route)
    return Request(
        "GET",
        f"/v1/runs/{_q(run_id)}/artifacts",
        params=_clean({"limit": limit, "cursor": cursor}),
    )


run_artifacts: Operation[m.Page[m.Artifact]] = Operation(
    "run_artifacts", _run_artifacts, _parse_artifact_page, True
)


# -- api keys ---------------------------------------------------------------------------
# src/artifact/api/routes/keys.py prefix /v1/api-keys (line 13)


def _create_key(
    *,
    name: str,
    scopes: list[str],
    project_id: str | None = None,
    expires_at: datetime | None = None,
) -> Request:
    # routes/keys.py:19 POST /v1/api-keys; body CreateKeyRequest (services/models.py:225-229)
    return Request(
        "POST",
        "/v1/api-keys",
        json=_clean(
            {
                "name": name,
                "scopes": list(scopes),
                "project_id": project_id,
                "expires_at": _iso(expires_at),
            }
        ),
    )


create_key: Operation[m.CreatedKey] = Operation(
    "create_key", _create_key, m.CreatedKey.from_json, False
)


def _list_keys() -> Request:
    # routes/keys.py:29 GET /v1/api-keys; response is a plain list[KeyView]
    return Request("GET", "/v1/api-keys")


list_keys: Operation[list[m.ApiKey]] = Operation("list_keys", _list_keys, _parse_keys, True)


def _revoke_key(*, key_id: str) -> Request:
    # routes/keys.py:36 DELETE /v1/api-keys/{key_id}; returns the revoked KeyView
    return Request("DELETE", f"/v1/api-keys/{_q(key_id)}")


revoke_key: Operation[m.ApiKey] = Operation("revoke_key", _revoke_key, m.ApiKey.from_json, False)


# -- usage ------------------------------------------------------------------------------
# src/artifact/api/routes/usage.py:18 GET /v1/usage (prefix line 13)


def _get_usage(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    granularity: str = "day",
    metric: str | None = None,
    project_id: str | None = None,
    principal_id: str | None = None,
) -> Request:
    return Request(
        "GET",
        "/v1/usage",
        params=_clean(
            {
                "start": _iso(start),
                "end": _iso(end),
                "granularity": granularity,
                "metric": metric,
                "project_id": project_id,
                "principal_id": principal_id,
            }
        ),
    )


get_usage: Operation[list[m.UsagePoint]] = Operation("get_usage", _get_usage, _parse_usage, True)


# src/artifact/api/routes/quota.py GET /v1/quota
def _get_quota() -> Request:
    return Request("GET", "/v1/quota")


get_quota: Operation[m.Quota] = Operation("get_quota", _get_quota, m.Quota.from_json, True)


ALL: tuple[Operation[Any], ...] = (
    whoami,
    list_projects,
    create_upload,
    complete_upload,
    get_artifact,
    get_by_external_key,
    list_artifacts,
    search,
    resolve,
    download_url,
    update_artifact,
    create_version_upload,
    add_relation,
    list_relations,
    list_versions,
    delete_artifact,
    create_run,
    seal_run,
    run_artifacts,
    create_key,
    list_keys,
    revoke_key,
    get_usage,
    get_quota,
)

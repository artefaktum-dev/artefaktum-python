"""The two client shells and their namespaces (design spec §5, §7).

`Artefaktum` and `AsyncArtefaktum` are thin: they own two httpx clients -- `_http` for the
API (carrying the API key) and `_storage` for the object-storage host (**no** default
headers, so a signed URL never sees the key) -- plus a transport (`_t`) that drives the
sans-I/O operation table in `_ops.py`. Every namespace method is one line over that
transport; the multi-step flows (`push`, `create_version`, `fulfil`, `pull`, `iter_all`)
compose the helpers in `_files.py`.

Project scoping: only the operations whose routes take a `project_id` call
`_project_id()`. That is `create_upload`, `get_by_external_key`, `list_artifacts`,
`search`, `resolve` and `create_run` (required), plus `create_key` and `get_usage`
(optional, and only when the caller names a project). Everything addressed by an
artifact, run or key id is already unambiguous, so those methods take no `project`
argument at all -- the signature tells the truth about what the call does.

The namespaces are written twice, sync and async, rather than generated: duplication of a
one-line method body is cheaper to read than a metaclass, and `_ops.py` is the single
place where a route can change.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import _ops as ops
from ._config import Config, is_uuid, load_config
from ._files import (
    Prepared,
    Source,
    adownload_to,
    aput_to_storage,
    await_ready,
    download_to,
    prepare,
    put_to_storage,
    wait_ready,
)
from ._transport import AsyncTransport, SyncTransport, make_headers
from .errors import RELATION_TYPES, ArtefaktumError, NotFound
from .models import (
    ApiKey,
    Artifact,
    ArtifactRef,
    CreatedKey,
    Download,
    Identity,
    Page,
    Project,
    Quota,
    Relation,
    Resolution,
    Run,
    SearchPage,
    UploadTicket,
    UsagePoint,
    Version,
)

Dest = str | os.PathLike[str]
MaxAge = int | float | timedelta | None

# `Artifacts.list` shadows the builtin inside that class body, so annotations written
# after it cannot spell `list[...]`. These aliases keep those return types readable.
RelationList = list[Relation]
VersionList = list[Version]


def _seconds(max_age: MaxAge) -> int | None:
    """`max_age` as whole seconds; the API's `max_age_seconds` is an int."""
    if max_age is None:
        return None
    if isinstance(max_age, timedelta):
        return int(max_age.total_seconds())
    return int(max_age)


def _str_list(value: str | Sequence[str] | None) -> list[str] | None:
    """Normalise a filter that may be one string, a sequence, or nothing.

    An empty sequence becomes `None` so `_ops._clean` drops the key entirely and the
    server applies its own default, rather than sending `[]` (which some filters would
    read as "match nothing").
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    items = list(value)
    return items or None


def _dict(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return dict(value) if value is not None else None


def _unknown_project(slug: str, projects: Sequence[Project]) -> NotFound:
    available = sorted(p.slug for p in projects)
    return NotFound(
        f"no project with slug {slug!r}; available: {available}", code="project_not_found"
    )


def _find_project(slug: str, projects: Sequence[Project]) -> str:
    for project in projects:
        if project.slug == slug:
            return project.id
    raise _unknown_project(slug, projects)


def _dest_path(dest: Dest) -> Path:
    """`Path(dest)`, creating it first when the caller spelled it as a directory.

    `Path("out/")` drops the trailing separator and `is_dir()` is false for a directory
    that does not exist yet, so `pull(id, "out/")` would write the bytes to a *file*
    named `out`. The raw string still carries the intent, so it is read before `Path`
    normalises it away. (A `Path` argument cannot carry it: `Path("out") / ""` is already
    `Path("out")`.)
    """
    raw = os.fspath(dest)
    path = Path(raw)
    if raw.endswith(("/", os.sep)):
        path.mkdir(parents=True, exist_ok=True)
    return path


def _signed_version(versions: Sequence[Version], download: Download) -> Version | None:
    return next((v for v in versions if v.id == download.version_id), None)


def _latest(artifact: Artifact) -> Sequence[Version]:
    """`get()`'s single version, as a list, for the fallback in `pull`.

    A server old enough not to list versions still reports `latest_version`; running it
    through the same id match keeps the guarantee that the bytes and the name agree.
    """
    return [artifact.latest_version] if artifact.latest_version is not None else []


def _pull_plan(version: Version | None, download: Download, verify: bool) -> tuple[str, str | None]:
    """The filename to write under and the digest to check, for the *signed* version.

    The signed URL names one version; a `create_version` landing between signing it and
    reading the metadata would otherwise pair version N's bytes with version N+1's name
    and digest -- which `verify=False` would write out silently under the wrong name.
    """
    if version is None:
        raise ArtefaktumError(
            f"the signed download names version {download.version_id}, which this artifact "
            "no longer lists; retry the pull",
            code="version_mismatch",
        )
    return version.filename, (version.sha256 if verify else None)


def _check_relation_type(relation_type: str) -> None:
    if relation_type not in RELATION_TYPES:
        raise ValueError(
            f"relation_type must be one of {sorted(RELATION_TYPES)}, got {relation_type!r}"
        )


def _fulfilment(resolution: Resolution) -> UploadTicket:
    """The reservation and signed PUT from a `create` resolution, or `ValueError`."""
    if resolution.status != "create" or resolution.reservation is None or resolution.upload is None:
        raise ValueError(
            "fulfil expects a resolution with status 'create' carrying a reservation and an "
            f"upload; got status {resolution.status!r}"
        )
    return UploadTicket(artifact=resolution.reservation, upload=resolution.upload)


# == sync ================================================================================


class Projects:
    def __init__(self, client: Artefaktum) -> None:
        self._c = client

    def list(self) -> list[Project]:
        return self._c._t.run(ops.list_projects)


class Artifacts:
    #: Seconds between `get` polls while `push` and friends wait for `ready`. Private and
    #: deliberately absent from the public surface (spec §5 fixes the 0.5 s cadence); the
    #: tests shrink it so they do not sleep for real.
    _interval: float = 0.5

    def __init__(self, client: Artefaktum) -> None:
        self._c = client

    # -- the three upload steps ---------------------------------------------------------

    def create_upload(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        external_key: str | None = None,
        expires_at: datetime | None = None,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        *,
        project: str | None = None,
    ) -> UploadTicket:
        return self._c._t.run(
            ops.create_upload,
            project_id=self._c._project_id(project),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            title=title,
            description=description,
            tags=list(tags),
            metadata=_dict(metadata),
            external_key=external_key,
            expires_at=expires_at,
            summary=summary,
            run_id=run,
            infer_lineage=infer_lineage,
        )

    def complete_upload(
        self,
        artifact_id: str,
        version_id: str,
        sha256: str | None = None,
        size_bytes: int | None = None,
        etag: str | None = None,
    ) -> ArtifactRef:
        return self._c._t.run(
            ops.complete_upload,
            artifact_id=artifact_id,
            version_id=version_id,
            sha256=sha256,
            size_bytes=size_bytes,
            etag=etag,
        )

    def _upload(
        self, ticket: UploadTicket, prepared: Prepared, *, wait: bool, timeout: float
    ) -> Artifact:
        """PUT the bytes, complete the version, then read the artifact back (§7 steps 3-5).

        If the PUT raises `StorageError` the version stays `pending_upload` server-side:
        nothing is completed, and the caller may retry with a fresh ticket.
        """
        put_to_storage(self._c._storage, ticket.upload, prepared)
        self.complete_upload(
            ticket.artifact.id,
            ticket.artifact.version_id,
            sha256=prepared.sha256,
            size_bytes=prepared.size,
        )
        if not wait:
            return self.get(ticket.artifact.id)
        return wait_ready(
            lambda: self.get(ticket.artifact.id), timeout=timeout, interval=self._interval
        )

    def push(
        self,
        source: Source,
        *,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        external_key: str | None = None,
        expires_at: datetime | None = None,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        content_type: str | None = None,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
        project: str | None = None,
    ) -> Artifact:
        """Hash, reserve, upload, complete and (by default) wait for `ready` (§7).

        `source` is a path (streamed in 1 MiB chunks, never held whole) or `bytes`, which
        needs an explicit `filename`. The bytes go straight to object storage over a
        client with no Authorization header, so the API key never reaches that host.

        Failure modes, in the order they can happen: `create_upload` failing leaves
        nothing behind; a failing PUT raises `StorageError` and leaves the new artifact
        (or version) `pending_upload` server-side, never `ready`, so it is invisible to
        `search` and safe to abandon or retry; `complete_upload` failing leaves the same
        state, with the bytes already in storage. `wait=True` polls `get` every 0.5 s and
        raises `ProcessingFailed` or `ProcessingTimeout`; `wait=False` returns after a
        single `get`, with status `processing` or `ready`.
        """
        prepared = prepare(source, filename=filename, content_type=content_type)
        ticket = self.create_upload(
            prepared.filename,
            prepared.content_type,
            prepared.size,
            title,
            description=description,
            tags=tags,
            metadata=metadata,
            external_key=external_key,
            expires_at=expires_at,
            summary=summary,
            run=run,
            infer_lineage=infer_lineage,
            project=project,
        )
        return self._upload(ticket, prepared, wait=wait, timeout=timeout)

    def create_version(
        self,
        artifact_id: str,
        source: Source,
        *,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        content_type: str | None = None,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> Artifact:
        """A new version of an existing artifact: `push`'s flow over `POST /{id}/uploads`."""
        prepared = prepare(source, filename=filename, content_type=content_type)
        ticket: UploadTicket = self._c._t.run(
            ops.create_version_upload,
            artifact_id=artifact_id,
            filename=prepared.filename,
            content_type=prepared.content_type,
            size_bytes=prepared.size,
            summary=summary,
            run_id=run,
            infer_lineage=infer_lineage,
        )
        return self._upload(ticket, prepared, wait=wait, timeout=timeout)

    def fulfil(
        self,
        resolution: Resolution,
        source: Source,
        *,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> Artifact:
        """Upload the bytes a `resolve` asked for; `ValueError` for any other status.

        `filename` is only needed for a `bytes` source -- the reservation already fixed
        the stored name, and the signed PUT carries its own content type, so it is used
        purely to satisfy `prepare`.
        """
        ticket = _fulfilment(resolution)
        prepared = prepare(source, filename=filename, content_type=None)
        return self._upload(ticket, prepared, wait=wait, timeout=timeout)

    # -- reads --------------------------------------------------------------------------

    def get(self, artifact_id: str) -> Artifact:
        return self._c._t.run(ops.get_artifact, artifact_id=artifact_id)

    def get_by_external_key(self, key: str, *, project: str | None = None) -> Artifact:
        return self._c._t.run(
            ops.get_by_external_key,
            project_id=self._c._project_id(project),
            external_key=key,
        )

    def list(
        self,
        status: str | Sequence[str] | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        external_key: str | None = None,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        expires_before: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        *,
        project: str | None = None,
    ) -> Page[Artifact]:
        return self._c._t.run(
            ops.list_artifacts,
            project_id=self._c._project_id(project),
            status=_str_list(status),
            content_type=content_type,
            tag=tag,
            external_key=external_key,
            created_before=created_before,
            created_after=created_after,
            expires_before=expires_before,
            limit=limit,
            cursor=cursor,
        )

    def iter_all(
        self,
        *,
        status: str | Sequence[str] | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        external_key: str | None = None,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        expires_before: datetime | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> Iterator[Artifact]:
        """Every artifact matching the filters, following `next_cursor` page by page.

        The project is resolved once, before the first page, so a cached slug lookup is
        not repeated per page.
        """
        project_id = self._c._project_id(project)
        cursor: str | None = None
        while True:
            page: Page[Artifact] = self._c._t.run(
                ops.list_artifacts,
                project_id=project_id,
                status=_str_list(status),
                content_type=content_type,
                tag=tag,
                external_key=external_key,
                created_before=created_before,
                created_after=created_after,
                expires_before=expires_before,
                limit=limit,
                cursor=cursor,
            )
            yield from page.items
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def search(
        self,
        query: str = "",
        mode: str = "hybrid",
        limit: int = 20,
        cursor: str | None = None,
        content_types: Sequence[str] = (),
        tags_all: Sequence[str] = (),
        status: Sequence[str] = ("ready",),
        external_key: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        exclude_superseded: bool = True,
        *,
        project: str | None = None,
    ) -> SearchPage:
        return self._c._t.run(
            ops.search,
            project_id=self._c._project_id(project),
            query=query,
            mode=mode,
            limit=limit,
            cursor=cursor,
            content_types=_str_list(content_types),
            tags_all=_str_list(tags_all),
            status=_str_list(status),
            external_key=external_key,
            created_after=created_after,
            created_before=created_before,
            exclude_superseded=exclude_superseded,
        )

    def resolve(
        self,
        external_key: str,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        max_age: MaxAge = None,
        run: str | None = None,
        project: str | None = None,
    ) -> Resolution:
        """Ask for an artifact by external key, reserving an upload if it is missing."""
        return self._c._t.run(
            ops.resolve,
            project_id=self._c._project_id(project),
            external_key=external_key,
            max_age_seconds=_seconds(max_age),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            title=title,
            description=description,
            tags=list(tags),
            metadata=_dict(metadata),
            run_id=run,
        )

    def download_url(self, artifact_id: str) -> Download:
        return self._c._t.run(ops.download_url, artifact_id=artifact_id)

    def pull(self, artifact_id: str, dest: Dest, *, verify: bool = True) -> Path:
        """Stream the bytes to `dest`, checking the digest, and return the final path.

        `dest` may be a directory -- an existing one, or any path written with a trailing
        separator, which is created -- in which case the signed version's original
        filename is used, reduced to a basename; otherwise `dest` is the file to write.
        The download runs on `_storage`, which carries no Authorization header, so the
        API key never reaches the storage host. A digest mismatch deletes the partial
        file and raises `IntegrityError`; `verify=False` skips the comparison.
        """
        download = self.download_url(artifact_id)
        version = _signed_version(self.versions(artifact_id), download)
        if version is None:
            version = _signed_version(_latest(self.get(artifact_id)), download)
        filename, expected = _pull_plan(version, download, verify)
        return download_to(
            self._c._storage,
            download.url,
            _dest_path(dest),
            filename=filename,
            expected_sha256=expected,
        )

    # -- metadata and graph -------------------------------------------------------------

    def update(
        self,
        artifact_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        clear_expires_at: bool = False,
    ) -> Artifact:
        return self._c._t.run(
            ops.update_artifact,
            artifact_id=artifact_id,
            title=title,
            description=description,
            tags=list(tags) if tags is not None else None,
            metadata=_dict(metadata),
            expires_at=expires_at,
            clear_expires_at=clear_expires_at,
        )

    def add_relation(
        self,
        artifact_id: str,
        to_artifact_id: str,
        relation_type: str,
        *,
        to_version_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Relation:
        _check_relation_type(relation_type)
        return self._c._t.run(
            ops.add_relation,
            artifact_id=artifact_id,
            to_artifact_id=to_artifact_id,
            relation_type=relation_type,
            to_version_id=to_version_id,
            metadata=_dict(metadata),
        )

    def relations(self, artifact_id: str) -> RelationList:
        return self._c._t.run(ops.list_relations, artifact_id=artifact_id)

    def versions(self, artifact_id: str) -> VersionList:
        return self._c._t.run(ops.list_versions, artifact_id=artifact_id)

    def delete(self, artifact_id: str) -> None:
        return self._c._t.run(ops.delete_artifact, artifact_id=artifact_id)


class Runs:
    def __init__(self, client: Artefaktum) -> None:
        self._c = client

    def create(self, run_id: str | None = None, *, project: str | None = None) -> Run:
        return self._c._t.run(
            ops.create_run, project_id=self._c._project_id(project), run_id=run_id
        )

    def seal(self, run_id: str) -> Run:
        return self._c._t.run(ops.seal_run, run_id=run_id)

    def artifacts(self, run_id: str, limit: int = 50, cursor: str | None = None) -> Page[Artifact]:
        return self._c._t.run(ops.run_artifacts, run_id=run_id, limit=limit, cursor=cursor)


class Keys:
    def __init__(self, client: Artefaktum) -> None:
        self._c = client

    def create(
        self,
        name: str,
        scopes: Sequence[str],
        *,
        project: str | None = None,
        expires_at: datetime | None = None,
    ) -> CreatedKey:
        # `project` is optional here: omitting it mints a tenant-wide key, so the client
        # default must NOT be applied.
        return self._c._t.run(
            ops.create_key,
            name=name,
            scopes=list(scopes),
            project_id=self._c._project_id(project) if project is not None else None,
            expires_at=expires_at,
        )

    def list(self) -> list[ApiKey]:
        return self._c._t.run(ops.list_keys)

    def revoke(self, key_id: str) -> ApiKey:
        return self._c._t.run(ops.revoke_key, key_id=key_id)


class Usage:
    def __init__(self, client: Artefaktum) -> None:
        self._c = client

    def get(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        granularity: str = "day",
        metric: str | None = None,
        *,
        project: str | None = None,
        principal_id: str | None = None,
    ) -> list[UsagePoint]:
        # As for `keys.create`: no project means "the whole tenant", not the default one.
        return self._c._t.run(
            ops.get_usage,
            start=start,
            end=end,
            granularity=granularity,
            metric=metric,
            project_id=self._c._project_id(project) if project is not None else None,
            principal_id=principal_id,
        )


class Artefaktum:
    """The synchronous client. `with Artefaktum() as c:` closes both httpx clients."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
        storage_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config: Config = load_config(api_key, base_url, project, timeout)
        self._http = httpx.Client(
            base_url=self._config.base_url,
            headers=make_headers(self._config),
            timeout=self._config.timeout,
            transport=transport,
        )
        # No default headers: the API key must never reach the object-storage host.
        self._storage = httpx.Client(timeout=self._config.timeout, transport=storage_transport)
        self._t = SyncTransport(self._http)
        self._default_project_id: str | None = None
        self.projects = Projects(self)
        self.artifacts = Artifacts(self)
        self.runs = Runs(self)
        self.keys = Keys(self)
        self.usage = Usage(self)

    def _project_id(self, override: str | None = None) -> str:
        """A project UUID for a project-scoped call: the override, else the cached default.

        A UUID is used as given. A slug is looked up through `GET /v1/projects`; the
        client default is resolved once and cached, a per-call override is not (it would
        evict the default and is rare enough not to matter).
        """
        if override is not None:
            return override if is_uuid(override) else _find_project(override, self.projects.list())
        if self._default_project_id is None:
            slug = self._config.project
            self._default_project_id = (
                slug if is_uuid(slug) else _find_project(slug, self.projects.list())
            )
        return self._default_project_id

    def whoami(self) -> Identity:
        return self._t.run(ops.whoami)

    def quota(self) -> Quota:
        """Plan, limits and current usage. Never blocked by the call limit."""
        return self._t.run(ops.get_quota)

    def close(self) -> None:
        self._http.close()
        self._storage.close()

    def __enter__(self) -> Artefaktum:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# == async ===============================================================================


class AsyncProjects:
    def __init__(self, client: AsyncArtefaktum) -> None:
        self._c = client

    async def list(self) -> list[Project]:
        return await self._c._t.run(ops.list_projects)


class AsyncArtifacts:
    #: See `Artifacts._interval`.
    _interval: float = 0.5

    def __init__(self, client: AsyncArtefaktum) -> None:
        self._c = client

    # -- the three upload steps ---------------------------------------------------------

    async def create_upload(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        external_key: str | None = None,
        expires_at: datetime | None = None,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        *,
        project: str | None = None,
    ) -> UploadTicket:
        return await self._c._t.run(
            ops.create_upload,
            project_id=await self._c._project_id(project),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            title=title,
            description=description,
            tags=list(tags),
            metadata=_dict(metadata),
            external_key=external_key,
            expires_at=expires_at,
            summary=summary,
            run_id=run,
            infer_lineage=infer_lineage,
        )

    async def complete_upload(
        self,
        artifact_id: str,
        version_id: str,
        sha256: str | None = None,
        size_bytes: int | None = None,
        etag: str | None = None,
    ) -> ArtifactRef:
        return await self._c._t.run(
            ops.complete_upload,
            artifact_id=artifact_id,
            version_id=version_id,
            sha256=sha256,
            size_bytes=size_bytes,
            etag=etag,
        )

    async def _upload(
        self, ticket: UploadTicket, prepared: Prepared, *, wait: bool, timeout: float
    ) -> Artifact:
        """PUT the bytes, complete the version, then read the artifact back (§7 steps 3-5).

        If the PUT raises `StorageError` the version stays `pending_upload` server-side:
        nothing is completed, and the caller may retry with a fresh ticket.
        """
        await aput_to_storage(self._c._storage, ticket.upload, prepared)
        await self.complete_upload(
            ticket.artifact.id,
            ticket.artifact.version_id,
            sha256=prepared.sha256,
            size_bytes=prepared.size,
        )
        if not wait:
            return await self.get(ticket.artifact.id)
        # `await_ready` sleeps with `asyncio.sleep`; nothing here blocks the event loop.
        return await await_ready(
            lambda: self.get(ticket.artifact.id), timeout=timeout, interval=self._interval
        )

    async def push(
        self,
        source: Source,
        *,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        external_key: str | None = None,
        expires_at: datetime | None = None,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        content_type: str | None = None,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
        project: str | None = None,
    ) -> Artifact:
        """Hash, reserve, upload, complete and (by default) wait for `ready` (§7).

        See `Artifacts.push` for the failure modes; they are identical here. The local
        hash runs in a worker thread and the PUT streams the file through one too, so a
        large source never stalls the event loop.
        """
        prepared = await asyncio.to_thread(
            prepare, source, filename=filename, content_type=content_type
        )
        ticket = await self.create_upload(
            prepared.filename,
            prepared.content_type,
            prepared.size,
            title,
            description=description,
            tags=tags,
            metadata=metadata,
            external_key=external_key,
            expires_at=expires_at,
            summary=summary,
            run=run,
            infer_lineage=infer_lineage,
            project=project,
        )
        return await self._upload(ticket, prepared, wait=wait, timeout=timeout)

    async def create_version(
        self,
        artifact_id: str,
        source: Source,
        *,
        summary: str | None = None,
        run: str | None = None,
        infer_lineage: bool = True,
        content_type: str | None = None,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> Artifact:
        """A new version of an existing artifact: `push`'s flow over `POST /{id}/uploads`."""
        prepared = await asyncio.to_thread(
            prepare, source, filename=filename, content_type=content_type
        )
        ticket: UploadTicket = await self._c._t.run(
            ops.create_version_upload,
            artifact_id=artifact_id,
            filename=prepared.filename,
            content_type=prepared.content_type,
            size_bytes=prepared.size,
            summary=summary,
            run_id=run,
            infer_lineage=infer_lineage,
        )
        return await self._upload(ticket, prepared, wait=wait, timeout=timeout)

    async def fulfil(
        self,
        resolution: Resolution,
        source: Source,
        *,
        filename: str | None = None,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> Artifact:
        """Upload the bytes a `resolve` asked for; `ValueError` for any other status.

        See `Artifacts.fulfil` for what `filename` is for.
        """
        ticket = _fulfilment(resolution)
        prepared = await asyncio.to_thread(prepare, source, filename=filename, content_type=None)
        return await self._upload(ticket, prepared, wait=wait, timeout=timeout)

    # -- reads --------------------------------------------------------------------------

    async def get(self, artifact_id: str) -> Artifact:
        return await self._c._t.run(ops.get_artifact, artifact_id=artifact_id)

    async def get_by_external_key(self, key: str, *, project: str | None = None) -> Artifact:
        return await self._c._t.run(
            ops.get_by_external_key,
            project_id=await self._c._project_id(project),
            external_key=key,
        )

    async def list(
        self,
        status: str | Sequence[str] | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        external_key: str | None = None,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        expires_before: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        *,
        project: str | None = None,
    ) -> Page[Artifact]:
        return await self._c._t.run(
            ops.list_artifacts,
            project_id=await self._c._project_id(project),
            status=_str_list(status),
            content_type=content_type,
            tag=tag,
            external_key=external_key,
            created_before=created_before,
            created_after=created_after,
            expires_before=expires_before,
            limit=limit,
            cursor=cursor,
        )

    async def iter_all(
        self,
        *,
        status: str | Sequence[str] | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        external_key: str | None = None,
        created_before: datetime | None = None,
        created_after: datetime | None = None,
        expires_before: datetime | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> AsyncIterator[Artifact]:
        """Every artifact matching the filters, following `next_cursor` page by page."""
        project_id = await self._c._project_id(project)
        cursor: str | None = None
        while True:
            page: Page[Artifact] = await self._c._t.run(
                ops.list_artifacts,
                project_id=project_id,
                status=_str_list(status),
                content_type=content_type,
                tag=tag,
                external_key=external_key,
                created_before=created_before,
                created_after=created_after,
                expires_before=expires_before,
                limit=limit,
                cursor=cursor,
            )
            for artifact in page.items:
                yield artifact
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    async def search(
        self,
        query: str = "",
        mode: str = "hybrid",
        limit: int = 20,
        cursor: str | None = None,
        content_types: Sequence[str] = (),
        tags_all: Sequence[str] = (),
        status: Sequence[str] = ("ready",),
        external_key: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        exclude_superseded: bool = True,
        *,
        project: str | None = None,
    ) -> SearchPage:
        return await self._c._t.run(
            ops.search,
            project_id=await self._c._project_id(project),
            query=query,
            mode=mode,
            limit=limit,
            cursor=cursor,
            content_types=_str_list(content_types),
            tags_all=_str_list(tags_all),
            status=_str_list(status),
            external_key=external_key,
            created_after=created_after,
            created_before=created_before,
            exclude_superseded=exclude_superseded,
        )

    async def resolve(
        self,
        external_key: str,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        title: str,
        description: str = "",
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        max_age: MaxAge = None,
        run: str | None = None,
        project: str | None = None,
    ) -> Resolution:
        """Ask for an artifact by external key, reserving an upload if it is missing."""
        return await self._c._t.run(
            ops.resolve,
            project_id=await self._c._project_id(project),
            external_key=external_key,
            max_age_seconds=_seconds(max_age),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            title=title,
            description=description,
            tags=list(tags),
            metadata=_dict(metadata),
            run_id=run,
        )

    async def download_url(self, artifact_id: str) -> Download:
        return await self._c._t.run(ops.download_url, artifact_id=artifact_id)

    async def pull(self, artifact_id: str, dest: Dest, *, verify: bool = True) -> Path:
        """Stream the bytes to `dest`, checking the digest, and return the final path.

        See `Artifacts.pull`; the API key never reaches the storage host here either.
        """
        download = await self.download_url(artifact_id)
        version = _signed_version(await self.versions(artifact_id), download)
        if version is None:
            version = _signed_version(_latest(await self.get(artifact_id)), download)
        filename, expected = _pull_plan(version, download, verify)
        return await adownload_to(
            self._c._storage,
            download.url,
            _dest_path(dest),
            filename=filename,
            expected_sha256=expected,
        )

    # -- metadata and graph -------------------------------------------------------------

    async def update(
        self,
        artifact_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        clear_expires_at: bool = False,
    ) -> Artifact:
        return await self._c._t.run(
            ops.update_artifact,
            artifact_id=artifact_id,
            title=title,
            description=description,
            tags=list(tags) if tags is not None else None,
            metadata=_dict(metadata),
            expires_at=expires_at,
            clear_expires_at=clear_expires_at,
        )

    async def add_relation(
        self,
        artifact_id: str,
        to_artifact_id: str,
        relation_type: str,
        *,
        to_version_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Relation:
        _check_relation_type(relation_type)
        return await self._c._t.run(
            ops.add_relation,
            artifact_id=artifact_id,
            to_artifact_id=to_artifact_id,
            relation_type=relation_type,
            to_version_id=to_version_id,
            metadata=_dict(metadata),
        )

    async def relations(self, artifact_id: str) -> RelationList:
        return await self._c._t.run(ops.list_relations, artifact_id=artifact_id)

    async def versions(self, artifact_id: str) -> VersionList:
        return await self._c._t.run(ops.list_versions, artifact_id=artifact_id)

    async def delete(self, artifact_id: str) -> None:
        return await self._c._t.run(ops.delete_artifact, artifact_id=artifact_id)


class AsyncRuns:
    def __init__(self, client: AsyncArtefaktum) -> None:
        self._c = client

    async def create(self, run_id: str | None = None, *, project: str | None = None) -> Run:
        return await self._c._t.run(
            ops.create_run, project_id=await self._c._project_id(project), run_id=run_id
        )

    async def seal(self, run_id: str) -> Run:
        return await self._c._t.run(ops.seal_run, run_id=run_id)

    async def artifacts(
        self, run_id: str, limit: int = 50, cursor: str | None = None
    ) -> Page[Artifact]:
        return await self._c._t.run(ops.run_artifacts, run_id=run_id, limit=limit, cursor=cursor)


class AsyncKeys:
    def __init__(self, client: AsyncArtefaktum) -> None:
        self._c = client

    async def create(
        self,
        name: str,
        scopes: Sequence[str],
        *,
        project: str | None = None,
        expires_at: datetime | None = None,
    ) -> CreatedKey:
        return await self._c._t.run(
            ops.create_key,
            name=name,
            scopes=list(scopes),
            project_id=await self._c._project_id(project) if project is not None else None,
            expires_at=expires_at,
        )

    async def list(self) -> list[ApiKey]:
        return await self._c._t.run(ops.list_keys)

    async def revoke(self, key_id: str) -> ApiKey:
        return await self._c._t.run(ops.revoke_key, key_id=key_id)


class AsyncUsage:
    def __init__(self, client: AsyncArtefaktum) -> None:
        self._c = client

    async def get(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        granularity: str = "day",
        metric: str | None = None,
        *,
        project: str | None = None,
        principal_id: str | None = None,
    ) -> list[UsagePoint]:
        return await self._c._t.run(
            ops.get_usage,
            start=start,
            end=end,
            granularity=granularity,
            metric=metric,
            project_id=await self._c._project_id(project) if project is not None else None,
            principal_id=principal_id,
        )


class AsyncArtefaktum:
    """The asynchronous client. `async with AsyncArtefaktum() as c:` closes both clients."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        storage_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config: Config = load_config(api_key, base_url, project, timeout)
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers=make_headers(self._config),
            timeout=self._config.timeout,
            transport=transport,
        )
        # No default headers: the API key must never reach the object-storage host.
        self._storage = httpx.AsyncClient(timeout=self._config.timeout, transport=storage_transport)
        self._t = AsyncTransport(self._http)
        self._default_project_id: str | None = None
        self.projects = AsyncProjects(self)
        self.artifacts = AsyncArtifacts(self)
        self.runs = AsyncRuns(self)
        self.keys = AsyncKeys(self)
        self.usage = AsyncUsage(self)

    async def _project_id(self, override: str | None = None) -> str:
        """A project UUID for a project-scoped call: the override, else the cached default."""
        if override is not None:
            if is_uuid(override):
                return override
            return _find_project(override, await self.projects.list())
        if self._default_project_id is None:
            slug = self._config.project
            self._default_project_id = (
                slug if is_uuid(slug) else _find_project(slug, await self.projects.list())
            )
        return self._default_project_id

    async def whoami(self) -> Identity:
        return await self._t.run(ops.whoami)

    async def quota(self) -> Quota:
        """Plan, limits and current usage. Never blocked by the call limit."""
        return await self._t.run(ops.get_quota)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._storage.aclose()

    async def __aenter__(self) -> AsyncArtefaktum:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = [
    "Artefaktum",
    "AsyncArtefaktum",
    "Artifacts",
    "AsyncArtifacts",
    "Projects",
    "AsyncProjects",
    "Runs",
    "AsyncRuns",
    "Keys",
    "AsyncKeys",
    "Usage",
    "AsyncUsage",
]

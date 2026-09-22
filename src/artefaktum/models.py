"""Frozen dataclasses mirroring the API's response views (design spec §6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit, urlunsplit

T = TypeVar("T")


def _redacted_url(url: str) -> str:
    """A signed URL with its signature dropped: scheme, host and path, then `?…`.

    A signed URL is a bearer credential -- anyone holding the query string can read or
    write the object until it expires -- so it must not reach a log or a traceback. What
    is left still identifies the object, which is what a repr is for.
    """
    split = urlsplit(url)
    base = urlunsplit((split.scheme, split.netloc, split.path, "", ""))
    return f"{base}?…" if split.query else base


def _redacted_secret(secret: str) -> str:
    """A minted key reduced to its prefix, enough to tell two keys apart."""
    return f"{secret[:3]}***"


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _req_dt(value: Any) -> datetime:
    dt = _dt(value)
    if dt is None:
        raise ValueError(f"expected an ISO-8601 datetime, got {value!r}")
    return dt


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    project_id: str | None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Identity:
        return cls(tenant_id=str(d["tenant_id"]), project_id=d.get("project_id"))


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    slug: str
    created_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Project:
        return cls(
            id=str(d["id"]), name=d["name"], slug=d["slug"], created_at=_req_dt(d["created_at"])
        )


@dataclass(frozen=True)
class Version:
    id: str
    version_number: int
    filename: str
    content_type: str
    size_bytes: int
    etag: str | None
    sha256: str | None
    summary: str | None
    status: str
    created_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Version:
        return cls(
            id=str(d["id"]),
            version_number=int(d["version_number"]),
            filename=d["original_filename"],
            content_type=d["content_type"],
            size_bytes=int(d["size_bytes"]),
            etag=d.get("etag"),
            sha256=d.get("sha256"),
            summary=d.get("summary"),
            status=d["status"],
            created_at=_req_dt(d["created_at"]),
        )


@dataclass(frozen=True)
class Artifact:
    id: str
    project_id: str
    external_key: str | None
    title: str
    description: str
    tags: list[str]
    metadata: dict[str, Any]
    status: str
    expires_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    latest_version: Version | None
    semantic_ready: bool = False
    superseded: bool = False
    superseded_by: str | None = None
    stale_upstream: bool = False

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Artifact:
        lv = d.get("latest_version")
        return cls(
            id=str(d["id"]),
            project_id=str(d["project_id"]),
            external_key=d.get("external_key"),
            title=d["title"],
            description=d.get("description", ""),
            tags=list(d.get("tags", [])),
            metadata=dict(d.get("metadata", {})),
            status=d["status"],
            expires_at=_dt(d.get("expires_at")),
            created_by=str(d.get("created_by", "")),
            created_at=_req_dt(d["created_at"]),
            updated_at=_req_dt(d["updated_at"]),
            latest_version=Version.from_json(lv) if lv else None,
            semantic_ready=bool(d.get("semantic_ready", False)),
            superseded=bool(d.get("superseded", False)),
            superseded_by=(str(d["superseded_by"]) if d.get("superseded_by") else None),
            stale_upstream=bool(d.get("stale_upstream", False)),
        )


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    version_id: str
    status: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ArtifactRef:
        return cls(id=str(d["id"]), version_id=str(d["version_id"]), status=d["status"])


@dataclass(frozen=True)
class UploadInstructions:
    method: str
    url: str
    headers: dict[str, str]
    expires_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UploadInstructions:
        return cls(
            method=d["method"],
            url=d["url"],
            headers=dict(d.get("headers", {})),
            expires_at=_req_dt(d["expires_at"]),
        )

    def __repr__(self) -> str:
        # `url` is signed; `.url` still holds the real thing for the code that sends it.
        return (
            f"UploadInstructions(method={self.method!r}, url={_redacted_url(self.url)!r}, "
            f"headers={self.headers!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True)
class UploadTicket:
    artifact: ArtifactRef
    upload: UploadInstructions

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UploadTicket:
        return cls(
            artifact=ArtifactRef.from_json(d["artifact"]),
            upload=UploadInstructions.from_json(d["upload"]),
        )


@dataclass(frozen=True)
class SearchHit:
    artifact: Artifact
    score: float
    match_mode: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SearchHit:
        return cls(
            artifact=Artifact.from_json(d["artifact"]),
            score=float(d["score"]),
            match_mode=d["match_mode"],
        )


@dataclass(frozen=True)
class SearchPage:
    items: list[SearchHit]
    next_cursor: str | None
    mode: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SearchPage:
        return cls(
            items=[SearchHit.from_json(x) for x in d.get("items", [])],
            next_cursor=d.get("next_cursor"),
            mode=d["mode"],
        )


@dataclass(frozen=True)
class Resolution:
    status: str
    artifact: Artifact | None
    reservation: ArtifactRef | None
    upload: UploadInstructions | None
    retry_after_seconds: int | None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Resolution:
        artifact = d.get("artifact")
        reservation = d.get("reservation")
        upload = d.get("upload")
        return cls(
            status=d["status"],
            artifact=Artifact.from_json(artifact) if artifact else None,
            reservation=ArtifactRef.from_json(reservation) if reservation else None,
            upload=UploadInstructions.from_json(upload) if upload else None,
            retry_after_seconds=d.get("retry_after_seconds"),
        )


@dataclass(frozen=True)
class Download:
    url: str
    method: str
    expires_at: datetime
    version_id: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Download:
        return cls(
            url=d["url"],
            method=d["method"],
            expires_at=_req_dt(d["expires_at"]),
            version_id=str(d["version_id"]),
        )

    def __repr__(self) -> str:
        # `url` is signed; `.url` still holds the real thing for the code that fetches it.
        return (
            f"Download(url={_redacted_url(self.url)!r}, method={self.method!r}, "
            f"expires_at={self.expires_at!r}, version_id={self.version_id!r})"
        )


@dataclass(frozen=True)
class Relation:
    id: str
    direction: str
    relation_type: str
    origin: str
    from_artifact_id: str
    from_version_id: str | None
    to_artifact_id: str
    to_version_id: str | None
    run_id: str | None
    metadata: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Relation:
        return cls(
            id=str(d["id"]),
            direction=d["direction"],
            relation_type=d["relation_type"],
            origin=d["origin"],
            from_artifact_id=str(d["from_artifact_id"]),
            from_version_id=(str(d["from_version_id"]) if d.get("from_version_id") else None),
            to_artifact_id=str(d["to_artifact_id"]),
            to_version_id=(str(d["to_version_id"]) if d.get("to_version_id") else None),
            run_id=(str(d["run_id"]) if d.get("run_id") else None),
            metadata=dict(d.get("metadata", {})),
            created_at=_req_dt(d["created_at"]),
        )


@dataclass(frozen=True)
class Run:
    id: str
    project_id: str
    created_by: str
    sealed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Run:
        return cls(
            id=str(d["id"]),
            project_id=str(d["project_id"]),
            created_by=str(d["created_by"]),
            sealed_at=_dt(d.get("sealed_at")),
            created_at=_req_dt(d["created_at"]),
        )


@dataclass(frozen=True)
class ApiKey:
    id: str
    name: str
    project_id: str | None
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ApiKey:
        return cls(
            id=str(d["id"]),
            name=d["name"],
            project_id=(str(d["project_id"]) if d.get("project_id") else None),
            scopes=list(d.get("scopes", [])),
            last_used_at=_dt(d.get("last_used_at")),
            expires_at=_dt(d.get("expires_at")),
            revoked_at=_dt(d.get("revoked_at")),
            created_at=_req_dt(d["created_at"]),
        )


@dataclass(frozen=True)
class CreatedKey:
    key: ApiKey
    secret: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CreatedKey:
        return cls(key=ApiKey.from_json(d["key"]), secret=d["secret"])

    def __repr__(self) -> str:
        # `secret` is a long-lived API key, shown once; `.secret` still holds it.
        return f"CreatedKey(key={self.key!r}, secret={_redacted_secret(self.secret)!r})"


@dataclass(frozen=True)
class UsagePoint:
    bucket_start: datetime
    metric: str
    value: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> UsagePoint:
        return cls(
            bucket_start=_req_dt(d["bucket_start"]), metric=d["metric"], value=int(d["value"])
        )


@dataclass(frozen=True)
class StorageQuota:
    used_bytes: int
    limit_bytes: int


@dataclass(frozen=True)
class CallQuota:
    used: int
    limit: int
    resets_at: datetime


@dataclass(frozen=True)
class Quota:
    """The tenant's plan, its limits and what is used (`GET /v1/quota`)."""

    plan: str
    storage: StorageQuota
    calls: CallQuota
    max_file_bytes: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Quota:
        s, c = d["storage"], d["calls"]
        return cls(
            plan=str(d["plan"]),
            storage=StorageQuota(
                used_bytes=int(s["used_bytes"]), limit_bytes=int(s["limit_bytes"])
            ),
            calls=CallQuota(
                used=int(c["used"]), limit=int(c["limit"]), resets_at=_req_dt(c["resets_at"])
            ),
            max_file_bytes=int(d["max_file_bytes"]),
        )


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None

    @classmethod
    def from_json(cls, d: dict[str, Any], item: Callable[[dict[str, Any]], T]) -> Page[T]:
        return cls(items=[item(x) for x in d.get("items", [])], next_cursor=d.get("next_cursor"))

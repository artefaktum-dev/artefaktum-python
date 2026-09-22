from __future__ import annotations

VERSION = {
    "id": "0199-v1",
    "version_number": 1,
    "original_filename": "report.pdf",
    "content_type": "application/pdf",
    "size_bytes": 1234,
    "etag": '"abc"',
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "summary": None,
    "status": "ready",
    "created_at": "2026-09-20T10:00:00Z",
}
ARTIFACT = {
    "id": "0199-a1",
    "project_id": "0199-p1",
    "external_key": None,
    "title": "Q3 churn",
    "description": "",
    "tags": ["churn"],
    "metadata": {"k": 1},
    "status": "ready",
    "expires_at": None,
    "created_by": "key:0199-k1",
    "created_at": "2026-09-20T10:00:00Z",
    "updated_at": "2026-09-20T10:00:01Z",
    "latest_version": VERSION,
    "semantic_ready": True,
    "superseded": False,
    "superseded_by": None,
    "stale_upstream": False,
    "some_future_field": "ignored",
}
PROJECT = {
    "id": "0199-p1",
    "name": "default",
    "slug": "default",
    "created_at": "2026-09-20T10:00:00Z",
}
UPLOAD = {
    "artifact": {"id": "0199-a1", "version_id": "0199-v1", "status": "pending_upload"},
    "upload": {
        "method": "PUT",
        "url": "https://storage.test/o?sig=1",
        "headers": {"content-type": "application/pdf"},
        "expires_at": "2026-09-20T10:15:00Z",
    },
}
RELATION = {
    "id": "0199-r1",
    "direction": "outgoing",
    "relation_type": "derived_from",
    "origin": "explicit",
    "from_artifact_id": "0199-a1",
    "from_version_id": None,
    "to_artifact_id": "0199-a0",
    "to_version_id": None,
    "run_id": None,
    "metadata": {},
    "created_at": "2026-09-20T10:00:00Z",
}
RUN = {
    "id": "run-1",
    "project_id": "0199-p1",
    "created_by": "key:0199-k1",
    "sealed_at": None,
    "created_at": "2026-09-20T10:00:00Z",
}
KEY = {
    "id": "0199-k2",
    "name": "agent",
    "project_id": None,
    "scopes": ["artifacts:read"],
    "last_used_at": None,
    "expires_at": None,
    "revoked_at": None,
    "created_at": "2026-09-20T10:00:00Z",
}
DOWNLOAD = {
    "url": "https://storage.test/o?sig=2",
    "method": "GET",
    "expires_at": "2026-09-20T10:15:00Z",
    "version_id": "0199-v1",
}
USAGE = {
    "granularity": "day",
    "items": [{"bucket_start": "2026-09-19T00:00:00Z", "metric": "requests", "value": 40}],
}
PROBLEM = {
    "type": "https://artefact.ai/errors/artifact_not_found",
    "title": "Artifact not found",
    "status": 404,
    "detail": "no such artifact",
    "instance": "/v1/artifacts/x",
    "code": "artifact_not_found",
    "request_id": "req-1",
}

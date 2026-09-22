from __future__ import annotations

import pytest

from artefaktum.errors import (
    ArtefaktumError,
    Conflict,
    Forbidden,
    NotFound,
    QuotaExceeded,
    ServiceUnavailable,
    Unauthorized,
    UploadError,
    ValidationFailed,
    from_problem,
)

from .fixtures import PROBLEM


@pytest.mark.parametrize(
    "code,cls",
    [
        ("artifact_not_found", NotFound),
        ("run_not_found", NotFound),
        # `api/errors.py` labels any bare 404 that carries no domain code `not_found`.
        ("not_found", NotFound),
        ("unauthorized", Unauthorized),
        ("insufficient_scope", Forbidden),
        ("quota_exceeded", QuotaExceeded),
        ("artifact_not_ready", Conflict),
        ("external_key_conflict", Conflict),
        ("idempotency_conflict", Conflict),
        ("run_sealed", Conflict),
        ("invalid_request", ValidationFailed),
        ("upload_expired", UploadError),
        ("object_verification_failed", UploadError),
        ("embedding_unavailable", ServiceUnavailable),
        ("something_new", ArtefaktumError),
    ],
)
def test_problem_maps_to_subclass(code, cls):
    err = from_problem(400, {**PROBLEM, "code": code}, "Bad Request")
    assert type(err) is cls and err.code == code and err.status == 400 and err.request_id == "req-1"
    assert str(err) == f"{code}: no such artifact (request_id=req-1)"


def test_non_json_body_becomes_http_error():
    err = from_problem(502, "<html>bad gateway</html>", "Bad Gateway")
    assert (
        type(err) is ArtefaktumError
        and err.code == "http_error"
        and err.message == "502 Bad Gateway"
    )
    assert str(err) == "http_error: 502 Bad Gateway"

"""The exception family and the problem+json → exception mapping (design spec §8)."""

from __future__ import annotations

from typing import Any

RELATION_TYPES = frozenset(
    {"derived_from", "supersedes", "attachment_of", "generated_by", "related_to"}
)


class ArtefaktumError(Exception):
    """Any failure reported by the API or by the SDK's own multi-step helpers."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message, self.code, self.status, self.request_id = message, code, status, request_id

    def __str__(self) -> str:
        tail = f" (request_id={self.request_id})" if self.request_id else ""
        return f"{self.code}: {self.message}{tail}"


class NotFound(ArtefaktumError): ...


class Unauthorized(ArtefaktumError): ...


class Forbidden(ArtefaktumError): ...


class QuotaExceeded(ArtefaktumError): ...


class Conflict(ArtefaktumError): ...


class ValidationFailed(ArtefaktumError): ...


class UploadError(ArtefaktumError): ...


class ServiceUnavailable(ArtefaktumError): ...


class MissingApiKey(ValueError):
    """No API key given and ARTEFAKTUM_API_KEY is not set."""


class ProcessingFailed(ArtefaktumError):
    def __init__(
        self, artifact: Any, message: str = "the server could not process the upload"
    ) -> None:
        super().__init__(message, code="processing_failed")
        self.artifact = artifact


class ProcessingTimeout(ArtefaktumError):
    def __init__(self, artifact: Any, timeout: float) -> None:
        super().__init__(
            f"artifact still {artifact.status} after {timeout:g}s", code="processing_timeout"
        )
        self.artifact = artifact


class IntegrityError(ArtefaktumError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"sha256 mismatch: expected {expected}, got {actual}", code="integrity_error"
        )
        self.expected, self.actual = expected, actual


class StorageError(ArtefaktumError):
    def __init__(self, status: int, host: str) -> None:
        super().__init__(
            f"object storage at {host} answered {status}", code="storage_error", status=status
        )
        self.host = host


_BY_CODE: dict[str, type[ArtefaktumError]] = {
    "artifact_not_found": NotFound,
    "run_not_found": NotFound,
    # `api/errors.py` labels a 404 that carries no domain code of its own `not_found`.
    "not_found": NotFound,
    "unauthorized": Unauthorized,
    "insufficient_scope": Forbidden,
    "quota_exceeded": QuotaExceeded,
    "artifact_not_ready": Conflict,
    "external_key_conflict": Conflict,
    "idempotency_conflict": Conflict,
    "run_sealed": Conflict,
    "invalid_request": ValidationFailed,
    "upload_expired": UploadError,
    "object_verification_failed": UploadError,
    "embedding_unavailable": ServiceUnavailable,
}


def from_problem(status: int, body: Any, reason: str) -> ArtefaktumError:
    """Turn an HTTP error response into the matching exception (RFC 9457 body or not)."""
    if not isinstance(body, dict) or "code" not in body:
        return ArtefaktumError(f"{status} {reason}".strip(), code="http_error", status=status)
    code = str(body["code"])
    cls = _BY_CODE.get(code, ArtefaktumError)
    message = str(body.get("detail") or body.get("title") or code)
    return cls(message, code=code, status=status, request_id=body.get("request_id"))

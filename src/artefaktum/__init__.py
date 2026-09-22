"""artefaktum: Python client for ArtefactAI.

`Artefaktum` and `AsyncArtefaktum` are the entry points; both expose the same namespaced
surface (`artifacts`, `projects`, `runs`, `keys`, `usage`) over the response models and
exception family re-exported here.
"""

from __future__ import annotations

from artefaktum._client import Artefaktum, AsyncArtefaktum
from artefaktum._version import __version__
from artefaktum.errors import (
    ArtefaktumError,
    Conflict,
    Forbidden,
    IntegrityError,
    MissingApiKey,
    NotFound,
    ProcessingFailed,
    ProcessingTimeout,
    QuotaExceeded,
    ServiceUnavailable,
    StorageError,
    Unauthorized,
    UploadError,
    ValidationFailed,
    from_problem,
)
from artefaktum.models import (
    ApiKey,
    Artifact,
    ArtifactRef,
    CallQuota,
    CreatedKey,
    Download,
    Identity,
    Page,
    Project,
    Quota,
    Relation,
    Resolution,
    Run,
    SearchHit,
    SearchPage,
    StorageQuota,
    UploadInstructions,
    UploadTicket,
    UsagePoint,
    Version,
)

__all__ = [
    "__version__",
    # clients
    "Artefaktum",
    "AsyncArtefaktum",
    # models
    "Identity",
    "Project",
    "Version",
    "Artifact",
    "ArtifactRef",
    "UploadInstructions",
    "UploadTicket",
    "Page",
    "SearchHit",
    "SearchPage",
    "Resolution",
    "Download",
    "Relation",
    "Run",
    "ApiKey",
    "CreatedKey",
    "UsagePoint",
    "Quota",
    "StorageQuota",
    "CallQuota",
    # errors
    "ArtefaktumError",
    "NotFound",
    "Unauthorized",
    "Forbidden",
    "QuotaExceeded",
    "Conflict",
    "ValidationFailed",
    "UploadError",
    "ServiceUnavailable",
    "MissingApiKey",
    "ProcessingFailed",
    "ProcessingTimeout",
    "IntegrityError",
    "StorageError",
    "from_problem",
]

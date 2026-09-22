from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import MissingApiKey

DEFAULT_BASE_URL = "https://api.artefaktum.dev"


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    project: str
    timeout: float


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def load_config(
    api_key: str | None = None,
    base_url: str | None = None,
    project: str | None = None,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> Config:
    e = os.environ if env is None else env
    key = api_key or e.get("ARTEFAKTUM_API_KEY")
    if not key:
        raise MissingApiKey("pass api_key= or set ARTEFAKTUM_API_KEY")
    url = (base_url or e.get("ARTEFAKTUM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return Config(
        api_key=key,
        base_url=url,
        project=project or e.get("ARTEFAKTUM_PROJECT") or "default",
        timeout=timeout,
    )

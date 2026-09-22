"""Sync and async transports: auth header, retries, error mapping (design spec §6, §8).

Both transports drive the sans-I/O `Operation` table from `_ops.py`: build a `Request`,
send it once, decode the response, map non-2xx bodies to typed errors, and retry an
idempotent read on a transient failure. Writes (`op.idempotent is False`) are never
retried -- a duplicated POST/PATCH/DELETE could double-apply server side.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

import httpx

from ._config import Config
from ._ops import Operation, Request
from ._version import __version__
from .errors import ArtefaktumError, from_problem

T = TypeVar("T")

RETRY_STATUSES = frozenset({429, 502, 503, 504})
# One delay per retry: `_should_retry` stops after the second, so there is no third.
BACKOFF = (0.5, 1.0)
# The ceiling on an honoured `Retry-After`. A server -- or a proxy in front of it -- can
# name any delay it likes; without a cap, one header parks the caller for that long inside
# a single call. `artifact.adapters.embeddings.openai` caps the same way.
MAX_RETRY_AFTER = 10.0
RETRYABLE_EXC = (httpx.ConnectError, httpx.ReadTimeout)


def make_headers(config: Config) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": f"artefaktum-python/{__version__}",
    }


def _delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        ra = response.headers.get("retry-after")
        if ra and ra.isdigit():
            return min(float(ra), MAX_RETRY_AFTER)
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def _decode(response: httpx.Response) -> Any:
    if response.is_success:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # A captive portal or a misrouted proxy can answer 200 text/html; a raw
            # json.JSONDecodeError would escape the SDK's error family.
            raise ArtefaktumError(
                f"{response.status_code} {response.reason_phrase}: response was not JSON",
                code="http_error",
                status=response.status_code,
            ) from None
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    raise from_problem(response.status_code, body, response.reason_phrase)


def _quota_exhausted(response: httpx.Response | None) -> bool:
    """A 429 whose problem code is `quota_exceeded`: the plan's allowance is spent."""
    if response is None or response.status_code != 429:
        return False
    try:
        body: Any = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("code") == "quota_exceeded"


def _should_retry(op: Operation[Any], attempt: int, response: httpx.Response | None) -> bool:
    # attempt is 0-based: retry after the first and second failures, never after the third.
    # A spent quota is a 429 too, but waiting does not bring it back before next month.
    return (
        op.idempotent
        and attempt < 2
        and (response is None or response.status_code in RETRY_STATUSES)
        and not _quota_exhausted(response)
    )


class SyncTransport:
    # The `Config` is spent before a transport exists: the client bakes the base URL and
    # `make_headers(config)` into the `httpx.Client` it hands over, so nothing here needs
    # to read it again.
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def run(self, op: Operation[T], **kwargs: Any) -> T:
        req: Request = op.build(**kwargs)
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = self._http.request(
                    req.method, req.path, params=req.params, json=req.json
                )
                return op.parse(_decode(response))
            except ArtefaktumError:
                if response is None or not _should_retry(op, attempt, response):
                    raise
            except RETRYABLE_EXC:
                if not _should_retry(op, attempt, None):
                    raise
            time.sleep(_delay(attempt, response))
            attempt += 1


class AsyncTransport:
    def __init__(self, http: httpx.AsyncClient) -> None:  # see `SyncTransport.__init__`
        self._http = http

    async def run(self, op: Operation[T], **kwargs: Any) -> T:
        req: Request = op.build(**kwargs)
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = await self._http.request(
                    req.method, req.path, params=req.params, json=req.json
                )
                return op.parse(_decode(response))
            except ArtefaktumError:
                if response is None or not _should_retry(op, attempt, response):
                    raise
            except RETRYABLE_EXC:
                if not _should_retry(op, attempt, None):
                    raise
            await asyncio.sleep(_delay(attempt, response))
            attempt += 1

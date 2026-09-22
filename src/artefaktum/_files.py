"""File helpers: hashing, PUT to object storage, waiting for `ready`, verified download
(design spec §7).

These helpers talk to the object-storage host, never the API: callers pass a plain
``httpx.Client``/``httpx.AsyncClient`` with no default headers, and the signed URL's own
``headers`` are the only headers ever sent -- the API key must never reach the storage
host, and the signed URL itself (a bearer credential) must never appear in an exception
message. A path source is never loaded whole: ``prepare`` hashes it in 1 MiB chunks,
``put_to_storage`` hands httpx the open file object and lets httpx stream it in its own
chunks, and ``aput_to_storage`` streams it in 1 MiB chunks through ``_aiter_file``; only
a ``bytes`` source is held in memory. Task 6's ``push``, ``pull``, ``create_version`` and
``fulfil`` compose these.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import httpx

from .errors import IntegrityError, ProcessingFailed, ProcessingTimeout, StorageError
from .models import Artifact, UploadInstructions

Source = str | os.PathLike[str] | bytes

_CHUNK_SIZE = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class Prepared:
    sha256: str
    size: int
    filename: str
    content_type: str
    source: Source


def _hash_source(source: Source) -> tuple[str, int]:
    h = hashlib.sha256()
    if isinstance(source, bytes):
        h.update(source)
        return h.hexdigest(), len(source)
    size = 0
    with open(source, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def prepare(
    source: Source, *, filename: str | None = None, content_type: str | None = None
) -> Prepared:
    if isinstance(source, bytes):
        if not filename:
            raise ValueError("filename is required when source is bytes")
        resolved_name = filename
    else:
        resolved_name = filename or Path(source).name
    sha256, size = _hash_source(source)
    ctype = content_type or mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
    return Prepared(
        sha256=sha256, size=size, filename=resolved_name, content_type=ctype, source=source
    )


def _put_body(prepared: Prepared) -> tuple[bytes | BinaryIO, BinaryIO | None]:
    """Return the request body and, if a file was opened, the handle to close after."""
    if isinstance(prepared.source, bytes):
        return prepared.source, None
    f = open(prepared.source, "rb")  # noqa: SIM115 -- closed by the caller after the request
    return f, f


def _storage_error(status: int, url: str) -> StorageError:
    # The signed URL is a credential (its query string carries the signature); only the
    # host is safe to put in an exception message.
    return StorageError(status, httpx.URL(url).host)


def put_to_storage(storage: httpx.Client, upload: UploadInstructions, prepared: Prepared) -> None:
    body, handle = _put_body(prepared)
    try:
        response = storage.request(upload.method, upload.url, headers=upload.headers, content=body)
    finally:
        if handle is not None:
            handle.close()
    if not response.is_success:
        raise _storage_error(response.status_code, upload.url)


async def _aiter_file(handle: BinaryIO) -> AsyncIterator[bytes]:
    """Stream a file in 1 MiB chunks without blocking the event loop.

    `httpx.AsyncClient` rejects a synchronous iterator as the request body, so a path
    source cannot be handed over the way `put_to_storage` does it; each read runs in a
    worker thread so a slow disk never stalls other coroutines.
    """
    while True:
        chunk = await asyncio.to_thread(handle.read, _CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _sized_headers(upload: UploadInstructions, size: int) -> dict[str, str]:
    """The signed headers plus an explicit `Content-Length`.

    httpx picks the framing from the body type: bytes and file objects get a
    `Content-Length`, but an *iterator* body unconditionally gets
    `Transfer-Encoding: chunked`. S3 rejects a chunked `PUT` to a presigned URL with
    `501 NotImplemented` (MinIO tolerates it, so a dev stack would not show this), and
    `Request._prepare` skips httpx's `Transfer-Encoding` default when the caller has
    already set `Content-Length` -- so setting it here is what keeps the async PUT of a
    path source framed the same way as the sync one.
    """
    headers = {k: v for k, v in upload.headers.items() if k.lower() != "content-length"}
    headers["Content-Length"] = str(size)
    return headers


async def aput_to_storage(
    storage: httpx.AsyncClient, upload: UploadInstructions, prepared: Prepared
) -> None:
    if isinstance(prepared.source, bytes):
        response = await storage.request(
            upload.method, upload.url, headers=upload.headers, content=prepared.source
        )
    else:
        headers = _sized_headers(upload, prepared.size)
        with open(prepared.source, "rb") as handle:
            response = await storage.request(
                upload.method, upload.url, headers=headers, content=_aiter_file(handle)
            )
    if not response.is_success:
        raise _storage_error(response.status_code, upload.url)


def wait_ready(
    get: Callable[[], Artifact],
    *,
    timeout: float,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Artifact:
    start = clock()
    while True:
        artifact = get()
        if artifact.status == "ready":
            return artifact
        if artifact.status == "failed":
            raise ProcessingFailed(artifact)
        if clock() - start >= timeout:
            raise ProcessingTimeout(artifact, timeout)
        sleep(interval)


async def await_ready(
    get: Callable[[], Awaitable[Artifact]],
    *,
    timeout: float,
    interval: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Artifact:
    start = clock()
    while True:
        artifact = await get()
        if artifact.status == "ready":
            return artifact
        if artifact.status == "failed":
            raise ProcessingFailed(artifact)
        if clock() - start >= timeout:
            raise ProcessingTimeout(artifact, timeout)
        await sleep(interval)


def _resolve_dest(dest: Path, filename: str) -> Path:
    """The file to write, given a destination and the *server's* filename.

    `filename` is the uploader's `original_filename`, which the API bounds only in
    length: it can be `../../.ssh/authorized_keys`, `/etc/cron.d/evil` or `a/b.txt`, and
    `dest / filename` would then escape `dest`, replace it outright, or silently write
    into a subdirectory. Reducing it to a basename keeps every write inside `dest`; a
    name with nothing usable left in it is refused rather than guessed at.
    """
    if not dest.is_dir():
        return dest
    name = PurePosixPath(filename.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise ValueError(f"server filename is not usable as a file name: {filename!r}")
    return dest / name


def _part_path(final: Path) -> Path:
    """
    A temp file no other download can be writing. Two pulls of one artifact into one
    directory would otherwise stream into the same `<final>.part`: each verifies only the
    chunks it wrote itself, so the mixture passes both digest checks, and whichever renames
    first leaves the other renaming a path that is gone.
    """
    return final.with_name(f"{final.name}.{secrets.token_hex(4)}.part")


def download_to(
    storage: httpx.Client,
    url: str,
    dest: Path,
    *,
    filename: str,
    expected_sha256: str | None,
) -> Path:
    final = _resolve_dest(dest, filename)
    part = _part_path(final)
    h = hashlib.sha256()
    try:
        with storage.stream("GET", url) as response:
            if not response.is_success:
                raise _storage_error(response.status_code, url)
            with open(part, "wb") as f:
                for chunk in response.iter_bytes():
                    h.update(chunk)
                    f.write(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return _finish_download(h.hexdigest(), expected_sha256, part, final)


async def adownload_to(
    storage: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    filename: str,
    expected_sha256: str | None,
) -> Path:
    final = _resolve_dest(dest, filename)
    part = _part_path(final)
    h = hashlib.sha256()
    try:
        async with storage.stream("GET", url) as response:
            if not response.is_success:
                raise _storage_error(response.status_code, url)
            with open(part, "wb") as f:
                async for chunk in response.aiter_bytes():
                    h.update(chunk)
                    f.write(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return _finish_download(h.hexdigest(), expected_sha256, part, final)


def _finish_download(
    actual_sha256: str, expected_sha256: str | None, part: Path, final: Path
) -> Path:
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        part.unlink(missing_ok=True)
        raise IntegrityError(expected_sha256, actual_sha256)
    os.replace(part, final)
    return final

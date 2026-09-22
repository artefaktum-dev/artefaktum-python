"""_files.py: hashing, storage PUT/GET, wait-for-ready (design spec §7).

These helpers talk to the object-storage host, never the API: they take a plain httpx
client with no default headers, and the signed URL's own `headers` are the only headers
sent. `wait_ready`/`await_ready` take an injectable clock as well as an injectable sleep
so tests never actually wait.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import pytest

from artefaktum import _files as files
from artefaktum.errors import IntegrityError, ProcessingFailed, ProcessingTimeout, StorageError
from artefaktum.models import Artifact, UploadInstructions

from .fixtures import ARTIFACT

UPLOAD = UploadInstructions(
    method="PUT",
    url="https://storage.test/o?sig=SECRET",
    headers={"content-type": "application/octet-stream"},
    expires_at=Artifact.from_json(ARTIFACT).created_at,
)


def _artifact(status: str) -> Artifact:
    d = dict(ARTIFACT)
    d["status"] = status
    return Artifact.from_json(d)


# -- prepare: hashing, filename, content-type ----------------------------------------


def test_prepare_hashes_small_file(tmp_path: Path) -> None:
    data = b"hello world"
    p = tmp_path / "note.txt"
    p.write_bytes(data)
    prepared = files.prepare(p, filename=None, content_type=None)
    assert prepared.sha256 == hashlib.sha256(data).hexdigest()
    assert prepared.size == len(data)
    assert prepared.filename == "note.txt"
    assert prepared.content_type == "text/plain"
    assert prepared.source == p


def test_prepare_hashes_large_file_in_chunks(tmp_path: Path) -> None:
    # 3 MiB, well over the 1 MiB chunk size, with non-repeating content so a
    # chunk-boundary bug (e.g. off-by-one in the read loop) would change the digest.
    data = os.urandom(3 * 1024 * 1024)
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    prepared = files.prepare(p, filename=None, content_type=None)
    assert prepared.sha256 == hashlib.sha256(data).hexdigest()
    assert prepared.size == len(data)


def test_prepare_reads_path_in_1mib_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = os.urandom(3 * 1024 * 1024)
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    seen_sizes: list[int] = []
    real_open = open

    class _Wrapped:
        def __init__(self, f: object) -> None:
            self._f = f

        def read(self, n: int) -> bytes:
            seen_sizes.append(n)
            return self._f.read(n)  # type: ignore[attr-defined]

        def __enter__(self) -> _Wrapped:
            return self

        def __exit__(self, *exc: object) -> None:
            self._f.close()  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._f, name)

    def fake_open(path: object, mode: str = "r", *a: object, **kw: object) -> object:
        if path == p and mode == "rb":
            return _Wrapped(real_open(path, mode))
        return real_open(path, mode, *a, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.open", fake_open)
    files.prepare(p, filename=None, content_type=None)
    assert seen_sizes  # some reads happened
    assert all(n == 1 << 20 for n in seen_sizes)  # every requested chunk is 1 MiB


def test_prepare_bytes_requires_filename() -> None:
    with pytest.raises(ValueError):
        files.prepare(b"data", filename=None, content_type=None)


def test_prepare_bytes_hashes_directly() -> None:
    data = b"some raw bytes"
    prepared = files.prepare(data, filename="raw.bin", content_type=None)
    assert prepared.sha256 == hashlib.sha256(data).hexdigest()
    assert prepared.size == len(data)
    assert prepared.filename == "raw.bin"
    assert prepared.source == data


def test_prepare_guesses_content_type_from_extension() -> None:
    prepared = files.prepare(b"{}", filename="data.json", content_type=None)
    assert prepared.content_type == "application/json"


def test_prepare_unknown_extension_falls_back_to_octet_stream() -> None:
    prepared = files.prepare(b"x", filename="mystery.qqzz", content_type=None)
    assert prepared.content_type == "application/octet-stream"


def test_prepare_honours_explicit_content_type() -> None:
    prepared = files.prepare(b"{}", filename="data.json", content_type="application/x-custom")
    assert prepared.content_type == "application/x-custom"


# -- put_to_storage / aput_to_storage --------------------------------------------------


def _storage_client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_put_sends_method_headers_and_body_verbatim_no_auth() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(200)

    prepared = files.prepare(b"payload bytes", filename="f.bin", content_type=None)
    files.put_to_storage(_storage_client(handler), UPLOAD, prepared)

    assert seen["method"] == "PUT"
    assert seen["url"] == UPLOAD.url
    assert seen["body"] == b"payload bytes"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["content-type"] == "application/octet-stream"
    assert "authorization" not in headers


def test_put_streams_path_source_without_loading_whole_file(tmp_path: Path) -> None:
    data = os.urandom(3 * 1024 * 1024)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    prepared = files.prepare(p, filename=None, content_type=None)

    received = bytearray()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        received.extend(request.read())
        return httpx.Response(200)

    files.put_to_storage(_storage_client(handler), UPLOAD, prepared)
    assert bytes(received) == data
    # httpx reads the length off the file object, so this PUT is never chunked.
    assert seen["content-length"] == str(len(data))
    assert "transfer-encoding" not in seen


def test_put_403_raises_storage_error_with_host_and_no_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    prepared = files.prepare(b"x", filename="f.bin", content_type=None)
    with pytest.raises(StorageError) as e:
        files.put_to_storage(_storage_client(handler), UPLOAD, prepared)
    assert e.value.status == 403
    assert e.value.host == "storage.test"
    assert "sig=" not in str(e.value)
    assert "SECRET" not in str(e.value)


async def test_aput_sends_method_headers_and_body_verbatim_no_auth() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(201)

    prepared = files.prepare(b"async payload", filename="f.bin", content_type=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await files.aput_to_storage(client, UPLOAD, prepared)

    assert seen["method"] == "PUT"
    assert seen["body"] == b"async payload"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert "authorization" not in headers


async def test_aput_streams_path_source(tmp_path: Path) -> None:
    # Regression (found by task 6's async push): handing `AsyncClient.request` a plain
    # file object makes httpx raise "Attempted to send an sync request with an
    # AsyncClient instance" -- a path source must be streamed as an async iterator.
    data = os.urandom(3 * 1024 * 1024)
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    prepared = files.prepare(p, filename=None, content_type=None)

    received = bytearray()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        received.extend(request.read())
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await files.aput_to_storage(client, UPLOAD, prepared)
    assert bytes(received) == data
    # httpx frames an *iterator* body as `Transfer-Encoding: chunked` unless the caller
    # sets a length; S3 answers a chunked presigned PUT with 501 NotImplemented.
    assert seen["content-length"] == str(len(data))
    assert "transfer-encoding" not in seen


async def test_aput_500_raises_storage_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    prepared = files.prepare(b"x", filename="f.bin", content_type=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StorageError) as e:
            await files.aput_to_storage(client, UPLOAD, prepared)
    assert e.value.status == 500 and e.value.host == "storage.test"


# -- wait_ready / await_ready ----------------------------------------------------------


def test_wait_ready_returns_immediately_when_ready() -> None:
    a = _artifact("ready")
    result = files.wait_ready(lambda: a, timeout=5.0, sleep=lambda s: pytest.fail("slept"))
    assert result is a


def test_wait_ready_raises_processing_failed() -> None:
    a = _artifact("failed")
    with pytest.raises(ProcessingFailed) as e:
        files.wait_ready(lambda: a, timeout=5.0, sleep=lambda s: pytest.fail("slept"))
    assert e.value.artifact is a


def test_wait_ready_times_out_with_last_artifact() -> None:
    # start=0.0; check#1 elapsed=0.5 (< 1.5s timeout, sleeps once); check#2 elapsed=2.0
    # (>= 1.5s timeout, raises with the second `get()`'s artifact as the last one seen).
    ticks = iter([0.0, 0.5, 2.0])
    clock = lambda: next(ticks)  # noqa: E731
    sleeps: list[float] = []
    calls = {"n": 0}

    def get() -> Artifact:
        calls["n"] += 1
        return _artifact("processing")

    with pytest.raises(ProcessingTimeout) as e:
        files.wait_ready(get, timeout=1.5, interval=0.1, sleep=sleeps.append, clock=clock)
    assert e.value.artifact.status == "processing"
    assert "1.5" in str(e.value)
    assert sleeps == [0.1]
    assert calls["n"] == 2


async def test_await_ready_returns_immediately_when_ready() -> None:
    a = _artifact("ready")

    async def get() -> Artifact:
        return a

    async def fail_sleep(_: float) -> None:
        pytest.fail("slept")

    result = await files.await_ready(get, timeout=5.0, sleep=fail_sleep)
    assert result is a


async def test_await_ready_raises_processing_failed() -> None:
    a = _artifact("failed")

    async def get() -> Artifact:
        return a

    with pytest.raises(ProcessingFailed):
        await files.await_ready(get, timeout=5.0)


async def test_await_ready_times_out_with_last_artifact() -> None:
    ticks = iter([0.0, 0.0, 2.0])
    clock = lambda: next(ticks)  # noqa: E731
    sleeps: list[float] = []

    async def get() -> Artifact:
        return _artifact("processing")

    async def rec_sleep(s: float) -> None:
        sleeps.append(s)

    with pytest.raises(ProcessingTimeout) as e:
        await files.await_ready(get, timeout=1.0, interval=0.2, sleep=rec_sleep, clock=clock)
    assert e.value.artifact.status == "processing"
    assert sleeps == [0.2]


# -- download_to / adownload_to --------------------------------------------------------

DOWNLOAD_URL = "https://storage.test/o?sig=SECRET2"


def _download_client(data: bytes, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_to_verifies_sha256_and_writes_file(tmp_path: Path) -> None:
    data = b"the downloaded content"
    digest = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "out.bin"
    result = files.download_to(
        _download_client(data), DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=digest
    )
    assert result == dest
    assert dest.read_bytes() == data
    # Nothing else in the directory: the temp file is named per call, so a leftover
    # would no longer be caught by naming one.
    assert [f.name for f in tmp_path.iterdir()] == ["out.bin"]


def test_download_to_mismatch_removes_part_and_raises(tmp_path: Path) -> None:
    data = b"actual content"
    wrong = "0" * 64
    dest = tmp_path / "out.bin"
    with pytest.raises(IntegrityError) as e:
        files.download_to(
            _download_client(data), DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=wrong
        )
    assert e.value.expected == wrong
    assert e.value.actual == hashlib.sha256(data).hexdigest()
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_to_no_verification_when_no_expected_sha(tmp_path: Path) -> None:
    data = b"unverified content"
    dest = tmp_path / "out.bin"
    result = files.download_to(
        _download_client(data), DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=None
    )
    assert result == dest
    assert dest.read_bytes() == data


def test_download_to_directory_uses_filename(tmp_path: Path) -> None:
    data = b"dir content"
    result = files.download_to(
        _download_client(data), DOWNLOAD_URL, tmp_path, filename="named.bin", expected_sha256=None
    )
    assert result == tmp_path / "named.bin"
    assert result.read_bytes() == data


def test_download_to_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old content that is longer than new")
    data = b"new"
    result = files.download_to(
        _download_client(data), DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=None
    )
    assert result == dest
    assert dest.read_bytes() == data


def test_download_to_storage_error_leaves_no_part_file(tmp_path: Path) -> None:
    dest = tmp_path / "out.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(StorageError) as e:
        files.download_to(
            httpx.Client(transport=httpx.MockTransport(handler)),
            DOWNLOAD_URL,
            dest,
            filename="out.bin",
            expected_sha256=None,
        )
    assert e.value.host == "storage.test"
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


async def test_adownload_to_verifies_and_writes(tmp_path: Path) -> None:
    data = b"async downloaded content"
    digest = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "out.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await files.adownload_to(
            client, DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=digest
        )
    assert result == dest
    assert dest.read_bytes() == data
    # Nothing else in the directory: the temp file is named per call, so a leftover
    # would no longer be caught by naming one.
    assert [f.name for f in tmp_path.iterdir()] == ["out.bin"]


async def test_adownload_to_mismatch_removes_part(tmp_path: Path) -> None:
    data = b"async content"
    wrong = "1" * 64
    dest = tmp_path / "out.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrityError):
            await files.adownload_to(
                client, DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=wrong
            )
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


# -- the server's filename is untrusted ------------------------------------------------
#
# `original_filename` is whatever the uploader sent (the server only bounds its length),
# so a writer in a shared project could otherwise steer a reader's `pull` outside `dest`.

ESCAPING_NAMES = ["../../escaped.txt", "/etc/escaped.txt", "sub/escaped.txt", "..", ""]


@pytest.mark.parametrize("filename", ESCAPING_NAMES)
def test_download_to_never_writes_outside_the_destination_directory(
    tmp_path: Path, filename: str
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        result = files.download_to(
            _download_client(b"payload"),
            DOWNLOAD_URL,
            dest,
            filename=filename,
            expected_sha256=None,
        )
    except ValueError:
        pass  # a filename with nothing usable left in it is refused outright
    else:
        assert result.parent == dest
        assert result.read_bytes() == b"payload"
    assert list(outside.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest", "outside"]


@pytest.mark.parametrize("filename", ESCAPING_NAMES)
async def test_adownload_to_never_writes_outside_the_destination_directory(
    tmp_path: Path, filename: str
) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"payload")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try:
            result = await files.adownload_to(
                client, DOWNLOAD_URL, dest, filename=filename, expected_sha256=None
            )
        except ValueError:
            pass
        else:
            assert result.parent == dest
            assert result.read_bytes() == b"payload"
    assert list(outside.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest", "outside"]


def test_download_to_explicit_file_path_ignores_the_server_filename(tmp_path: Path) -> None:
    dest = tmp_path / "exact.bin"
    result = files.download_to(
        _download_client(b"payload"),
        DOWNLOAD_URL,
        dest,
        filename="../../escaped.txt",
        expected_sha256=None,
    )
    assert result == dest


# -- a connection that dies mid-stream leaves nothing behind ---------------------------


def test_download_to_midstream_failure_leaves_no_part_file(tmp_path: Path) -> None:
    def body():
        yield b"the first chunk, which lands on disk"
        raise httpx.ReadError("the connection died mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    dest = tmp_path / "out.bin"
    with pytest.raises(httpx.ReadError):
        files.download_to(
            httpx.Client(transport=httpx.MockTransport(handler)),
            DOWNLOAD_URL,
            dest,
            filename="out.bin",
            expected_sha256=None,
        )
    assert list(tmp_path.iterdir()) == []


async def test_adownload_to_midstream_failure_leaves_no_part_file(tmp_path: Path) -> None:
    async def body():
        yield b"the first chunk, which lands on disk"
        raise httpx.ReadError("the connection died mid-stream")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    dest = tmp_path / "out.bin"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadError):
            await files.adownload_to(
                client, DOWNLOAD_URL, dest, filename="out.bin", expected_sha256=None
            )
    assert list(tmp_path.iterdir()) == []


# -- concurrent downloads of one artifact into one directory ---------------------------


def _streaming_client(data: bytes, midstream) -> httpx.Client:
    """A GET whose body stops after one byte to run `midstream`, then yields the rest."""

    def handler(request: httpx.Request) -> httpx.Response:
        def body():
            yield data[:1]
            midstream()
            yield data[1:]

        return httpx.Response(200, content=body())

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_to_does_not_share_its_temp_file_with_a_concurrent_download(
    tmp_path: Path,
) -> None:
    """Two pulls of one artifact into one directory: each needs its own temp file."""
    first, second = b"bytes of the first pull", b"bytes of the second pull!"
    inner: dict[str, Path] = {}

    def second_pull() -> None:
        inner["path"] = files.download_to(
            _download_client(second),
            DOWNLOAD_URL,
            tmp_path,
            filename="out.bin",
            expected_sha256=hashlib.sha256(second).hexdigest(),
        )

    out = files.download_to(
        _streaming_client(first, second_pull),
        DOWNLOAD_URL,
        tmp_path,
        filename="out.bin",
        expected_sha256=hashlib.sha256(first).hexdigest(),
    )

    assert out == inner["path"] == tmp_path / "out.bin"
    # The outer pull finishes last, so its verified bytes are what stays on disk: never a
    # mixture of the two, and never a crash because the other pull renamed the temp away.
    assert out.read_bytes() == first
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]


async def test_adownload_to_does_not_share_its_temp_file_with_a_concurrent_download(
    tmp_path: Path,
) -> None:
    """The async path has its own copy of the temp-file logic; it needs the same guarantee."""
    first, second = b"bytes of the first pull", b"bytes of the second pull!"
    inner: dict[str, Path] = {}

    async def second_pull() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=second)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            inner["path"] = await files.adownload_to(
                client,
                DOWNLOAD_URL,
                tmp_path,
                filename="out.bin",
                expected_sha256=hashlib.sha256(second).hexdigest(),
            )

    def outer_handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield first[:1]
            await second_pull()
            yield first[1:]

        return httpx.Response(200, content=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(outer_handler)) as client:
        out = await files.adownload_to(
            client,
            DOWNLOAD_URL,
            tmp_path,
            filename="out.bin",
            expected_sha256=hashlib.sha256(first).hexdigest(),
        )

    assert out == inner["path"] == tmp_path / "out.bin"
    assert out.read_bytes() == first
    assert [p.name for p in tmp_path.iterdir()] == ["out.bin"]

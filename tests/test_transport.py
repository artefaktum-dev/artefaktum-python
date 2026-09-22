"""SyncTransport/AsyncTransport: auth header, retries, error mapping (design spec §6, §8).

Note: the task brief's snippet calls `ops.get_artifact` and `ops.delete_artifact` with a
`project_id` kwarg. The already-committed `_ops.py` (task 2) found that neither route takes
`project_id` (see its module docstring and `test_get_artifact_has_no_project_query` /
`test_resolve_and_usage_and_delete` in `test_ops.py`): `get_artifact` takes only
`artifact_id` and sends no query params, and `delete_artifact` takes only `artifact_id`.
The tests below follow the actual operation table -- the source of truth -- omitting
`project_id` and expecting a bare `/v1/artifacts/a1` URL with no query string.
"""

from __future__ import annotations

import httpx
import pytest

from artefaktum import _ops as ops
from artefaktum import _transport as tr
from artefaktum._config import Config
from artefaktum.errors import ArtefaktumError, NotFound, QuotaExceeded

from .fixtures import ARTIFACT, PROBLEM

CFG = Config(api_key="ak_test", base_url="http://api.test", project="default", timeout=5.0)


def _sync(handler):
    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=CFG.base_url,
        headers=tr.make_headers(CFG),
    )
    return tr.SyncTransport(http)


def test_sends_bearer_and_user_agent_and_parses():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["ua"] = req.headers["user-agent"]
        seen["url"] = str(req.url)
        return httpx.Response(200, json=ARTIFACT)

    a = _sync(handler).run(ops.get_artifact, artifact_id="a1")
    assert a.id == "0199-a1" and seen["auth"] == "Bearer ak_test"
    assert seen["ua"].startswith("artefaktum-python/")
    assert seen["url"] == "http://api.test/v1/artifacts/a1"


def test_problem_json_becomes_typed_error():
    t = _sync(
        lambda req: httpx.Response(
            404, json=PROBLEM, headers={"content-type": "application/problem+json"}
        )
    )
    with pytest.raises(NotFound) as e:
        t.run(ops.get_artifact, artifact_id="x")
    assert e.value.request_id == "req-1"


def test_get_retries_on_503_and_honours_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, headers={"retry-after": "3"})
        return httpx.Response(200, json=ARTIFACT)

    assert _sync(handler).run(ops.get_artifact, artifact_id="a1").id == "0199-a1"
    assert calls["n"] == 3 and sleeps == [3.0, 3.0]


def test_get_backs_off_without_retry_after_then_gives_up(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway")

    t = _sync(handler)
    with pytest.raises(ArtefaktumError) as e:
        t.run(ops.get_artifact, artifact_id="a1")
    assert e.value.code == "http_error" and e.value.status == 502
    # Three attempts, so two waits: every delay `BACKOFF` holds is used, and no more.
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0] == list(tr.BACKOFF)


def test_post_is_never_retried(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: pytest.fail("slept on a write"))
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(ArtefaktumError):
        _sync(handler).run(ops.delete_artifact, artifact_id="a1")
    assert calls["n"] == 1


def test_connect_error_retries_on_reads(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json=ARTIFACT)

    result = _sync(handler).run(ops.get_artifact, artifact_id="a1")
    assert result.id and calls["n"] == 2


def test_connect_error_on_write_is_not_retried(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: pytest.fail("slept on a write"))
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(httpx.ConnectError):
        _sync(handler).run(ops.delete_artifact, artifact_id="a1")
    assert calls["n"] == 1


def test_read_timeout_on_write_is_reraised_untouched(monkeypatch):
    monkeypatch.setattr(tr.time, "sleep", lambda s: pytest.fail("slept on a write"))

    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)

    with pytest.raises(httpx.ReadTimeout):
        _sync(handler).run(ops.delete_artifact, artifact_id="a1")


def test_read_timeout_exhausts_retries_then_raises_unwrapped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=req)

    with pytest.raises(httpx.ReadTimeout):
        _sync(handler).run(ops.get_artifact, artifact_id="a1")
    assert calls["n"] == 3 and sleeps == [0.5, 1.0]


def test_non_numeric_retry_after_falls_back_to_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return httpx.Response(200, json=ARTIFACT)

    assert _sync(handler).run(ops.get_artifact, artifact_id="a1").id == "0199-a1"
    assert sleeps == [0.5]


def test_a_long_retry_after_is_capped(monkeypatch):
    """A server (or a proxy) asking for an hour must not park the caller for an hour."""
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"retry-after": "3600"})
        return httpx.Response(200, json=ARTIFACT)

    assert _sync(handler).run(ops.get_artifact, artifact_id="a1").id == "0199-a1"
    assert sleeps == [10.0]


def test_search_retries_on_429(monkeypatch):
    # `search` is a POST (the filter body is too large for a query string) but has no
    # side effects, so it is marked idempotent and retried like any other read.
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"items": [], "next_cursor": None, "mode": "hybrid"})

    result = _sync(handler).run(ops.search, project_id="p1", query="q")
    assert result.mode == "hybrid" and calls["n"] == 2 and sleeps == [0.5]


def test_decode_maps_non_dict_json_error_body_to_http_error():
    t = _sync(lambda req: httpx.Response(500, json=[1, 2]))
    with pytest.raises(ArtefaktumError) as e:
        t.run(ops.get_artifact, artifact_id="a1")
    assert e.value.code == "http_error" and e.value.status == 500


async def test_async_transport_mirrors_sync(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tr.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else httpx.Response(200, json=ARTIFACT)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=CFG.base_url,
        headers=tr.make_headers(CFG),
    )
    a = await tr.AsyncTransport(http).run(ops.get_artifact, artifact_id="a1")
    assert a.id == "0199-a1" and sleeps == [0.5]


def test_non_json_success_body_becomes_an_artefaktum_error():
    # A captive portal or a proxy answering 200 text/html must not escape the error
    # family as a bare json.JSONDecodeError.
    t = _sync(lambda req: httpx.Response(200, html="<html>sign in to the wifi</html>"))
    with pytest.raises(ArtefaktumError) as e:
        t.run(ops.get_artifact, artifact_id="a1")
    assert e.value.code == "http_error" and e.value.status == 200
    assert "not JSON" in e.value.message


async def test_async_non_json_success_body_becomes_an_artefaktum_error():
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, html="<html>hi</html>")),
        base_url=CFG.base_url,
        headers=tr.make_headers(CFG),
    )
    with pytest.raises(ArtefaktumError) as e:
        await tr.AsyncTransport(http).run(ops.get_artifact, artifact_id="a1")
    assert e.value.code == "http_error" and e.value.status == 200


def test_a_quota_429_is_not_retried(monkeypatch):
    # Retrying cannot succeed before the month rolls over; it would only delay the error.
    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(
            429, json=PROBLEM | {"status": 429, "code": "quota_exceeded", "title": "Quota exceeded"}
        )

    with pytest.raises(QuotaExceeded) as caught:
        _sync(handler).run(ops.search, project_id="p1", query="q")
    assert calls["n"] == 1 and sleeps == []
    assert caught.value.status == 429

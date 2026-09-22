"""Round-trip against a live stack (design spec §10 "Integration").

Skipped unless `ARTEFAKTUM_TEST_BASE_URL` and `ARTEFAKTUM_TEST_API_KEY` are set. This is
the only test file in the suite that makes real network calls; everything else runs
offline against `httpx.MockTransport`.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from artefaktum import Artefaktum, NotFound

BASE, KEY = os.environ.get("ARTEFAKTUM_TEST_BASE_URL"), os.environ.get("ARTEFAKTUM_TEST_API_KEY")
pytestmark = pytest.mark.skipif(
    not (BASE and KEY), reason="set ARTEFAKTUM_TEST_BASE_URL and ARTEFAKTUM_TEST_API_KEY"
)


def test_push_search_pull_delete_round_trip(tmp_path):
    body = b"hello from the sdk integration test\n" * 100
    src = tmp_path / "hello.txt"
    src.write_bytes(body)
    with Artefaktum(api_key=KEY, base_url=BASE) as c:
        a = c.artifacts.push(src, title="sdk integration", tags=["sdk-test"], timeout=60)
        assert a.status == "ready"
        assert a.latest_version and a.latest_version.sha256 == hashlib.sha256(body).hexdigest()

        hits = c.artifacts.search("sdk integration", mode="text")
        assert any(h.artifact.id == a.id for h in hits.items)

        # A trailing separator says "directory", and `pull` creates it; the file is
        # named after the version the signed URL points at.
        out = c.artifacts.pull(a.id, f"{tmp_path / 'out'}/")
        assert out == tmp_path / "out" / "hello.txt"
        assert out.read_bytes() == body

        c.artifacts.delete(a.id)
        for _ in range(20):
            try:
                c.artifacts.get(a.id)
                time.sleep(0.5)
            except NotFound:
                break
        else:
            pytest.fail("artifact still readable 10 s after delete")


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `python -m artefaktum.cli ARGS` as a real subprocess against the live stack.

    The key and base URL reach the child only through the environment, never argv, so
    neither can show up in a process listing or shell history the way a `--api-key`
    argument would.
    """
    assert BASE is not None and KEY is not None  # guaranteed once `pytestmark` lets a test run
    env = {**os.environ, "ARTEFAKTUM_API_KEY": KEY, "ARTEFAKTUM_BASE_URL": BASE}
    return subprocess.run(  # noqa: S603 - fixed argv (no shell), env carries the secret, not argv
        [sys.executable, "-m", "artefaktum.cli", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_round_trip(tmp_path):
    """The CLI, driven as a subprocess exactly as a shell or an agent would: push, search,
    pull, rm, then confirm the artifact is gone (design spec §6, "Testing").

    Captured stdout is never a TTY, so every command prints JSON without needing `--json`.
    """
    body = b"hello from the cli integration test\n" * 50
    src = tmp_path / "cli.txt"
    src.write_bytes(body)

    pushed = _cli(
        "push", str(src), "--title", "cli integration", "--tag", "sdk-test", "--timeout", "60"
    )
    assert pushed.returncode == 0, pushed.stderr
    artifact = json.loads(pushed.stdout)
    assert artifact["status"] == "ready"

    found = json.loads(_cli("search", "cli integration", "--mode", "text").stdout)
    assert any(hit["artifact"]["id"] == artifact["id"] for hit in found["items"])

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    pulled = json.loads(_cli("pull", artifact["id"], str(out_dir)).stdout)
    assert pathlib.Path(pulled["path"]).read_bytes() == body

    assert _cli("rm", artifact["id"]).returncode == 0

    for _ in range(20):
        if _cli("get", artifact["id"]).returncode == 3:
            break
        time.sleep(0.5)
    else:
        pytest.fail("artifact still readable 10 s after rm")

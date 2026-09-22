"""Test-only plumbing for driving the CLI end to end through `click.testing.CliRunner`.

`run_cli` wires the SDK's `Artefaktum` to `httpx.MockTransport`s so no real network call is
ever made, and swaps `artefaktum.cli.app.client_factory` for the duration of the call so
tests can both control what the client does and observe how it was constructed (via a
`Recorder`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from click.testing import CliRunner, Result

from artefaktum import Artefaktum
from artefaktum.cli import app as app_module


class Recorder:
    """Captures the keyword arguments the most recent `client_factory` call was made with."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None


def run_cli(
    args: list[str],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    storage: Callable[[httpx.Request], httpx.Response] | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
    recorder: Recorder | None = None,
) -> Result:
    """Run the CLI with the SDK wired to MockTransports.

    stdout is not a TTY here, so JSON is the default output mode - tests that want table
    text must pass `--table` explicitly. `env` is merged over a base environment that
    blanks the `ARTEFAKTUM_*` variables and points `XDG_CONFIG_HOME` at a path that does
    not exist, so a test starts with no ambient key/config unless it opts in.
    """

    def factory(**kwargs: Any) -> Artefaktum:
        if recorder is not None:
            recorder.kwargs = kwargs
        # Pass `api_key` through unchanged - substituting a fallback here would mask a
        # bug where an empty/None key reaches the factory (it should never get this far;
        # `make_client`/`login` are responsible for rejecting that before calling it).
        # A test that needs a working key passes `--api-key` or sets it in `env`.
        return Artefaktum(
            api_key=kwargs.get("api_key"),
            base_url="http://api.test",
            project=kwargs.get("project"),
            transport=httpx.MockTransport(handler),
            storage_transport=httpx.MockTransport(storage or (lambda r: httpx.Response(200))),
        )

    original = app_module.client_factory
    app_module.client_factory = factory
    try:
        base_env = {
            "ARTEFAKTUM_API_KEY": "",
            "ARTEFAKTUM_BASE_URL": "",
            "ARTEFAKTUM_PROJECT": "",
            "XDG_CONFIG_HOME": "/nonexistent-afk",
        }
        try:
            # click >= 8.2 removed `mix_stderr` (stdout/stderr are now always kept
            # separate); click 8.1.x needs it set to False for `result.stderr` to be
            # populated at all. Support both so this helper runs on either.
            runner = CliRunner(mix_stderr=False)
        except TypeError:
            runner = CliRunner()
        return runner.invoke(
            app_module.app, list(args), env={**base_env, **(env or {})}, input=input
        )
    finally:
        app_module.client_factory = original

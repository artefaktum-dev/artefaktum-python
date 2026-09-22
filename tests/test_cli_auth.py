"""End-to-end tests for the app core: global options, client factory, error handling,
and the `login`/`logout`/`whoami`/`projects` commands.
"""

from __future__ import annotations

import json
import stat
import sys

import click
import httpx
import pytest

from artefaktum.cli import config_file
from artefaktum.cli.app import DURATION, State

from .cli_helpers import Recorder, run_cli
from .fixtures import PROBLEM, PROJECT


def _whoami_ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"tenant_id": "t1", "project_id": None})


def _projects_ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"items": [PROJECT]})


_WITH_KEY = {"ARTEFAKTUM_API_KEY": "ak_test"}


# --- whoami --------------------------------------------------------------------


def test_whoami_json():
    result = run_cli(["whoami"], _whoami_ok, env=_WITH_KEY)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"tenant_id": "t1", "project_id": None}
    assert result.stderr == ""


def test_whoami_table():
    result = run_cli(["--table", "whoami"], _whoami_ok, env=_WITH_KEY)
    assert result.exit_code == 0
    assert result.stdout == "tenant_id: t1\n"


# --- projects --------------------------------------------------------------------


def test_projects_json():
    result = run_cli(["projects"], _projects_ok, env=_WITH_KEY)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert data[0]["slug"] == "default"


def test_projects_table():
    result = run_cli(["--table", "projects"], _projects_ok, env=_WITH_KEY)
    assert result.exit_code == 0
    header = result.stdout.splitlines()[0]
    assert header.split() == ["ID", "NAME", "SLUG"]


def test_projects_malformed_response_reports_error_instead_of_a_traceback():
    # `{"items": "not-a-list"}` makes `_parse_projects` iterate the string and hand each
    # character to `Project.from_json`, which raises `TypeError` when it does `d["id"]` -
    # not one of the exception types `App.invoke` used to list explicitly, so it used to
    # escape uncaught and print a full traceback (and bypass URL redaction) instead of a
    # clean, JSON-shaped error.
    result = run_cli(
        ["projects"], lambda r: httpx.Response(200, json={"items": "not-a-list"}), env=_WITH_KEY
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "error"
    assert payload["error"]["message"].startswith("TypeError:")


# --- precedence --------------------------------------------------------------------


def test_precedence_flag_wins(tmp_path):
    recorder = Recorder()
    config_file.save({"api_key": "ak_file"}, {"XDG_CONFIG_HOME": str(tmp_path)})
    result = run_cli(
        ["--api-key", "ak_flag", "whoami"],
        _whoami_ok,
        env={"ARTEFAKTUM_API_KEY": "ak_env", "XDG_CONFIG_HOME": str(tmp_path)},
        recorder=recorder,
    )
    assert result.exit_code == 0
    assert recorder.kwargs is not None
    assert recorder.kwargs["api_key"] == "ak_flag"


def test_precedence_env_wins_without_flag(tmp_path):
    recorder = Recorder()
    config_file.save({"api_key": "ak_file"}, {"XDG_CONFIG_HOME": str(tmp_path)})
    result = run_cli(
        ["whoami"],
        _whoami_ok,
        env={"ARTEFAKTUM_API_KEY": "ak_env", "XDG_CONFIG_HOME": str(tmp_path)},
        recorder=recorder,
    )
    assert result.exit_code == 0
    assert recorder.kwargs["api_key"] == "ak_env"


def test_precedence_file_wins_without_flag_or_env(tmp_path):
    recorder = Recorder()
    config_file.save({"api_key": "ak_file"}, {"XDG_CONFIG_HOME": str(tmp_path)})
    result = run_cli(
        ["whoami"],
        _whoami_ok,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        recorder=recorder,
    )
    assert result.exit_code == 0
    assert recorder.kwargs["api_key"] == "ak_file"


def test_precedence_base_url_and_project_flag_beats_env_beats_file(tmp_path):
    config_file.save(
        {"api_key": "ak_file", "base_url": "http://file.test", "project": "file-proj"},
        {"XDG_CONFIG_HOME": str(tmp_path)},
    )
    env_with_all = {
        "ARTEFAKTUM_API_KEY": "ak_env",
        "ARTEFAKTUM_BASE_URL": "http://env.test",
        "ARTEFAKTUM_PROJECT": "env-proj",
        "XDG_CONFIG_HOME": str(tmp_path),
    }

    flag_recorder = Recorder()
    result = run_cli(
        ["--base-url", "http://flag.test", "--project", "flag-proj", "whoami"],
        _whoami_ok,
        env=env_with_all,
        recorder=flag_recorder,
    )
    assert result.exit_code == 0
    assert flag_recorder.kwargs["base_url"] == "http://flag.test"
    assert flag_recorder.kwargs["project"] == "flag-proj"

    env_recorder = Recorder()
    result = run_cli(["whoami"], _whoami_ok, env=env_with_all, recorder=env_recorder)
    assert result.exit_code == 0
    assert env_recorder.kwargs["base_url"] == "http://env.test"
    assert env_recorder.kwargs["project"] == "env-proj"

    file_recorder = Recorder()
    result = run_cli(
        ["whoami"],
        _whoami_ok,
        env={"ARTEFAKTUM_API_KEY": "ak_env", "XDG_CONFIG_HOME": str(tmp_path)},
        recorder=file_recorder,
    )
    assert result.exit_code == 0
    assert file_recorder.kwargs["base_url"] == "http://file.test"
    assert file_recorder.kwargs["project"] == "file-proj"


# --- no key anywhere --------------------------------------------------------------------


def test_no_api_key_anywhere_exits_1_and_mentions_login():
    result = run_cli(["whoami"], _whoami_ok)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "artefaktum login" in result.stderr
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_api_key"


# --- error mapping --------------------------------------------------------------------


def test_404_problem_json_exits_3_with_matching_error_body():
    result = run_cli(["whoami"], lambda r: httpx.Response(404, json=PROBLEM), env=_WITH_KEY)
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload == {
        "error": {
            "code": "artifact_not_found",
            "message": "no such artifact",
            "request_id": "req-1",
        }
    }


def test_403_exits_1():
    body = {"code": "insufficient_scope", "detail": "nope"}
    result = run_cli(["whoami"], lambda r: httpx.Response(403, json=body), env=_WITH_KEY)
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "insufficient_scope"


def test_connect_error_exits_1_as_network_error():
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = run_cli(["whoami"], handler, env=_WITH_KEY)
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "network_error"


def test_keyboard_interrupt_exits_130_json():
    def handler(_: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt()

    result = run_cli(["whoami"], handler, env=_WITH_KEY)
    assert result.exit_code == 130
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload == {
        "error": {"code": "interrupted", "message": "interrupted", "request_id": None}
    }


def test_keyboard_interrupt_exits_130_table():
    def handler(_: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt()

    result = run_cli(["--table", "whoami"], handler, env=_WITH_KEY)
    assert result.exit_code == 130
    assert result.stderr == "error: interrupted: interrupted\n"


# --- login / logout --------------------------------------------------------------------


def test_login_prompts_verifies_and_saves(tmp_path):
    result = run_cli(
        ["login"],
        _whoami_ok,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="ak_typed\n",
    )
    assert result.exit_code == 0
    assert "ak_typed" not in result.stdout
    assert "ak_typed" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["logged_in"] is True
    assert payload["tenant_id"] == "t1"
    saved_path = tmp_path / "artefaktum" / "config.json"
    assert json.loads(saved_path.read_text()) == {"api_key": "ak_typed"}
    if sys.platform != "win32":
        assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600


def test_login_failed_whoami_writes_nothing(tmp_path):
    result = run_cli(
        ["login"],
        lambda r: httpx.Response(401, json={"code": "unauthorized", "detail": "nope"}),
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="ak_bad\n",
    )
    assert result.exit_code == 1
    assert "ak_bad" not in result.stdout
    assert "ak_bad" not in result.stderr
    assert not (tmp_path / "artefaktum" / "config.json").exists()


def test_login_api_key_stdin_does_not_prompt(tmp_path):
    result = run_cli(
        ["login", "--api-key-stdin"],
        _whoami_ok,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="ak_piped\n",
    )
    assert result.exit_code == 0
    # The prompt (when it happens at all) goes to stderr, not stdout - see
    # `test_login_prompts_verifies_and_saves`'s use of `click.prompt(..., err=True)` -
    # so asserting its absence from stdout could never fail. Check the stream the
    # prompt would actually land on, and that the piped key itself never leaks either.
    assert "API key" not in result.stderr
    assert "ak_piped" not in result.stdout
    assert "ak_piped" not in result.stderr
    saved_path = tmp_path / "artefaktum" / "config.json"
    assert json.loads(saved_path.read_text())["api_key"] == "ak_piped"


def test_login_rejects_empty_stdin_key_before_any_request(tmp_path):
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should have been made for an empty key")

    result = run_cli(
        ["login", "--api-key-stdin"],
        handler,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="\n",
    )
    assert result.exit_code == 2
    assert not (tmp_path / "artefaktum" / "config.json").exists()


def test_login_strips_whitespace_from_a_pasted_prompt_key(tmp_path):
    result = run_cli(
        ["login"],
        _whoami_ok,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="  ak_pasted \n",
    )
    assert result.exit_code == 0
    saved_path = tmp_path / "artefaktum" / "config.json"
    assert json.loads(saved_path.read_text())["api_key"] == "ak_pasted"


def test_login_help_is_user_facing_not_implementation_detail():
    # `--help` used to dump the docstring's implementation notes (`client_factory`,
    # `whoami()`) verbatim; those belong in a `#` comment, not in what a user reads to
    # decide how to use the command.
    result = run_cli(["login", "--help"], _whoami_ok)
    assert result.exit_code == 0
    assert "client_factory" not in result.stdout
    assert "whoami()" not in result.stdout
    assert "--api-key-stdin" in result.stdout
    assert "0600" in result.stdout


def test_login_keeps_previously_saved_base_url_and_project(tmp_path):
    config_file.save(
        {"api_key": "ak_old", "base_url": "http://old.test", "project": "proj-old"},
        {"XDG_CONFIG_HOME": str(tmp_path)},
    )
    result = run_cli(
        ["login"],
        _whoami_ok,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
        input="ak_new\n",
    )
    assert result.exit_code == 0
    saved = json.loads((tmp_path / "artefaktum" / "config.json").read_text())
    assert saved == {"api_key": "ak_new", "base_url": "http://old.test", "project": "proj-old"}


def test_logout_removes_file_and_says_so(tmp_path):
    config_file.save({"api_key": "ak_1"}, {"XDG_CONFIG_HOME": str(tmp_path)})
    result = run_cli(["--table", "logout"], _whoami_ok, env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert result.exit_code == 0
    assert "logged out" in result.stdout
    assert not (tmp_path / "artefaktum" / "config.json").exists()


def test_logout_when_nothing_saved(tmp_path):
    result = run_cli(["--table", "logout"], _whoami_ok, env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert result.exit_code == 0
    assert "not logged in" in result.stdout


# --- misc --------------------------------------------------------------------


def test_version_flag():
    result = run_cli(["--version"], _whoami_ok)
    assert result.exit_code == 0
    assert result.stdout.strip() == "artefaktum, version 0.1.1"


def test_json_and_table_together_is_a_usage_error():
    result = run_cli(["--json", "--table", "whoami"], _whoami_ok)
    assert result.exit_code == 2


def test_api_key_option_help_warns_about_process_listing_visibility():
    result = run_cli(["--help"], _whoami_ok)
    assert result.exit_code == 0
    assert "ps" in result.stdout or "process listing" in result.stdout
    assert "artefaktum login" in result.stdout


def test_duration_param_type_rejects_bad_value_directly():
    with pytest.raises(click.BadParameter):
        DURATION.convert("1y", None, None)


def test_state_repr_never_shows_the_api_key():
    assert "ak_" not in repr(State(api_key="ak_x"))


def test_broken_pipe_while_emitting_exits_0_and_prints_nothing(monkeypatch):
    # e.g. `artefaktum ls | head -1`: the reader closes its end before we finish writing,
    # and the write raises `BrokenPipeError`. This should look like a clean, quiet exit,
    # not `error: io_error: [Errno 32] Broken pipe`.
    from artefaktum.cli import app as app_module

    def boom(*_a, **_k):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(app_module, "emit", boom)
    result = run_cli(["whoami"], _whoami_ok, env=_WITH_KEY)
    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == ""

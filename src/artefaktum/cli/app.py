"""The `artefaktum` click group: global options, the client factory, central error
handling, and the `login`/`logout`/`whoami`/`projects` commands.

Every other CLI command (task 4 onward) is built the same way: a thin function that
opens a client through `make_client`, calls one SDK method, and hands the result to
`emit`. `App.invoke` is the single place that turns an SDK/network failure into a
stderr message and an exit code, so command bodies never need their own try/except.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

import click
import httpx

from .._client import Artefaktum
from .._files import prepare
from .._version import __version__
from ..errors import RELATION_TYPES, ArtefaktumError, MissingApiKey, NotFound
from ..models import Artifact, Project, Run
from . import config_file
from .durations import parse_duration
from .output import (
    ARTIFACT_COLUMNS,
    RELATION_COLUMNS,
    SEARCH_COLUMNS,
    VERSION_COLUMNS,
    artifact_fields,
    artifact_row,
    json_mode,
    relation_row,
    render_error,
    render_fields,
    render_table,
    search_hit_row,
    short_time,
    to_jsonable,
    version_row,
)

EXIT_ERROR, EXIT_NOT_FOUND, EXIT_PENDING, EXIT_INTERRUPTED = 1, 3, 4, 130

#: The client constructor. Tests replace this (see `tests/cli_helpers.py`) so a CLI
#: invocation can be driven end to end against `httpx.MockTransport`s with no network
#: access and no real config file.
client_factory: Callable[..., Artefaktum] = Artefaktum

#: `time.sleep`/`time.monotonic`, exposed as module attributes so `resolve`'s
#: pending-retry loop can be driven by tests without a real wait (see
#: `tests/test_cli_write.py::test_resolve_pending_then_hit_sleeps_once`, which
#: monkeypatches `_sleep`).
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


class ResolvePending(ArtefaktumError):  # noqa: N818 -- named for the resolution status, like
    # `errors.py`'s own `NotFound`/`Conflict`/etc, none of which carry an "Error" suffix either.
    """`resolve`'s `--wait` ran out while the reservation was still held by another writer.

    No HTTP response ends this wait (it's a client-side deadline over several `resolve`
    calls, each of which came back `pending`), so `status` and `request_id` stay at
    `ArtefaktumError`'s defaults of `None`.
    """

    def __init__(self, waited: float) -> None:
        super().__init__(
            f"still pending after waiting {waited:g}s; the reservation is held by another writer",
            code="resolve_pending",
        )
        self.waited = waited


@dataclass
class State:
    # `repr=False`: a `State` can end up in a traceback (e.g. via `ctx.find_object`), and
    # the default dataclass repr would otherwise print the raw API key.
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    project: str | None = None
    output: str | None = None  # "json" | "table" | None (None -> detect from the tty)


class _Duration(click.ParamType):  # type: ignore[type-arg]
    """A click option type for `parse_duration` (e.g. `--max-age 30d`).

    `click.ParamType` is only `Generic` from click 8.2 onward; subscripting it (as in
    `click.ParamType[timedelta, str]`) evaluates the base class at class-definition time
    and raises `TypeError: type 'ParamType' is not subscriptable` on click 8.1.x, which
    the `cli` extra's `click>=8.1` constraint still allows. Keep the bare base class and
    silence mypy's `[type-arg]` warning instead - it type-checks clean under 8.5 and
    runs under 8.1.8 alike.
    """

    name = "duration"

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> timedelta:
        if isinstance(value, timedelta):
            return value
        try:
            return parse_duration(str(value))
        except ValueError as err:
            self.fail(str(err), param, ctx)


DURATION = _Duration()


def _pick(flag: str | None, env_name: str, saved: Mapping[str, str], key: str) -> str | None:
    """The first of flag, environment variable, and saved config value, or `None`."""
    return flag or os.environ.get(env_name) or saved.get(key) or None


def make_client(state: State) -> Artefaktum:
    """Build a client from `state`, applying flag -> env -> config file precedence.

    Raises `MissingApiKey` itself (never passing an empty key into `client_factory`)
    when no key was found anywhere, so `App.invoke` reports a clean, CLI-specific
    error instead of whatever the SDK or `httpx` would otherwise raise first.
    """
    saved = config_file.load()
    api_key = _pick(state.api_key, "ARTEFAKTUM_API_KEY", saved, "api_key")
    if not api_key:
        raise MissingApiKey('no API key: run "artefaktum login" or set ARTEFAKTUM_API_KEY')
    return client_factory(
        api_key=api_key,
        base_url=_pick(state.base_url, "ARTEFAKTUM_BASE_URL", saved, "base_url"),
        project=_pick(state.project, "ARTEFAKTUM_PROJECT", saved, "project"),
    )


def emit(state: State, obj: Any, table: Callable[[], str]) -> None:
    """Print `obj` as one JSON document, or `table()`'s text, per the output mode."""
    if json_mode(state.output, sys.stdout):
        click.echo(json.dumps(to_jsonable(obj), indent=2))
    else:
        click.echo(table())


class App(click.Group):
    """Turns SDK and network failures into a message on stderr and a meaningful exit code."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (KeyboardInterrupt, click.Abort):
            # Routed through `_fail` (rather than a bare `click.echo("interrupted")`) so
            # JSON mode gets the same `{"error": {...}}` shape as any other failure,
            # instead of a plain string that breaks a JSON consumer.
            self._fail(ctx, ArtefaktumError("interrupted", code="interrupted"), EXIT_INTERRUPTED)
        except (click.ClickException, click.exceptions.Exit):
            raise
        except NotFound as err:
            self._fail(ctx, err, EXIT_NOT_FOUND)
        except ResolvePending as err:
            # Must be caught ahead of the generic `ArtefaktumError` branch below (it *is*
            # one, code `resolve_pending`) so it gets `EXIT_PENDING` instead of `EXIT_ERROR`
            # - the same pattern as `NotFound` just above.
            self._fail(ctx, err, EXIT_PENDING)
        except BrokenPipeError:
            # e.g. `artefaktum ls | head -1`: the reader closed its end before we finished
            # writing. Print nothing (there's no one left to read it) and point stdout at
            # devnull first, so the interpreter's own exit-time flush of the real stdout
            # doesn't raise the same error again on the way out. `fileno()` can itself
            # raise under `CliRunner` (its stdout isn't backed by a real OS file
            # descriptor), so failing to redirect is not fatal - just exit clean either way.
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError):
                pass
            ctx.exit(0)
        except (ArtefaktumError, MissingApiKey, httpx.HTTPError, ValueError, OSError) as err:
            self._fail(ctx, err, EXIT_ERROR)
        except Exception as err:  # noqa: BLE001 -- last resort: never let a bug print a
            # traceback (which would also skip URL redaction); see `output.error_payload`'s
            # generic "error" branch for the message shape.
            self._fail(ctx, err, EXIT_ERROR)

    @staticmethod
    def _fail(ctx: click.Context, err: BaseException, code: int) -> NoReturn:
        state = ctx.find_object(State) or State()
        click.echo(render_error(err, json_mode(state.output, sys.stdout)), err=True)
        ctx.exit(code)


@click.group(cls=App, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--api-key",
    default=None,
    help=(
        "API key (else ARTEFAKTUM_API_KEY, else the config file). Visible in process "
        'listings: prefer the env var or "artefaktum login".'
    ),
)
@click.option(
    "--base-url",
    default=None,
    help="API base URL (else ARTEFAKTUM_BASE_URL, else the config file).",
)
@click.option(
    "--project",
    default=None,
    help="Project id or slug (else ARTEFAKTUM_PROJECT, else the config file).",
)
@click.option("--json", "output_json", is_flag=True, help="Force JSON output.")
@click.option("--table", "output_table", is_flag=True, help="Force table/text output.")
@click.version_option(__version__, prog_name="artefaktum")
@click.pass_context
def app(
    ctx: click.Context,
    api_key: str | None,
    base_url: str | None,
    project: str | None,
    output_json: bool,
    output_table: bool,
) -> None:
    """Store, find and trust artifacts exchanged between AI agents."""
    if output_json and output_table:
        raise click.UsageError("--json and --table cannot both be given")
    output = "json" if output_json else "table" if output_table else None
    ctx.obj = State(api_key=api_key, base_url=base_url, project=project, output=output)


def _whoami_pairs(tenant_id: str, project_id: str | None) -> list[tuple[str, str]]:
    pairs = [("tenant_id", tenant_id)]
    if project_id:
        pairs.append(("project_id", project_id))
    return pairs


@app.command("whoami")
@click.pass_obj
def whoami_cmd(state: State) -> None:
    """Show the tenant (and project, if scoped) the API key belongs to."""
    with make_client(state) as client:
        identity = client.whoami()
    emit(
        state,
        identity,
        lambda: render_fields(_whoami_pairs(identity.tenant_id, identity.project_id)),
    )


def _project_row(p: Project) -> dict[str, str]:
    return {"id": p.id, "name": p.name, "slug": p.slug}


@app.command("projects")
@click.pass_obj
def projects_cmd(state: State) -> None:
    """List the tenant's projects."""
    with make_client(state) as client:
        items = client.projects.list()
    emit(
        state, items, lambda: render_table([_project_row(p) for p in items], ["id", "name", "slug"])
    )


@app.command("login")
@click.option(
    "--api-key-stdin", is_flag=True, help="Read the API key from stdin instead of prompting."
)
@click.pass_obj
def login_cmd(state: State, api_key_stdin: bool) -> None:
    """Verify an API key and save it for later commands to use.

    The key is never echoed - typed at a hidden prompt, or piped in with
    `--api-key-stdin` - and is saved to the config file (mode 0600) only once it has
    been checked against the server.
    """
    # Whitespace is stripped from either source (a pasted key can carry a trailing
    # newline or leading space); an empty result is rejected before any network call is
    # made, rather than being handed to `client_factory`, where the SDK would otherwise
    # silently fall back to `ARTEFAKTUM_API_KEY` if one happened to be set.
    key = (
        sys.stdin.readline().strip()
        if api_key_stdin
        else click.prompt("API key", hide_input=True, err=True).strip()
    )
    if not key:
        raise click.UsageError("no API key given")
    with client_factory(api_key=key, base_url=state.base_url, project=state.project) as client:
        identity = client.whoami()
    # Start from whatever is already saved so a bare `login` (no --base-url/--project)
    # keeps those settings instead of dropping them; only the key always changes.
    values = {**config_file.load(), "api_key": key}
    if state.base_url:
        values["base_url"] = state.base_url
    if state.project:
        values["project"] = state.project
    path = config_file.save(values)
    emit(
        state,
        {"logged_in": True, "tenant_id": identity.tenant_id, "config": str(path)},
        lambda: f"logged in — tenant {identity.tenant_id}\nsaved to {path}",
    )


@app.command("logout")
@click.pass_obj
def logout_cmd(state: State) -> None:
    """Remove the saved API key and config file, if any."""
    removed = config_file.clear()
    message = "logged out" if removed else "not logged in"
    emit(state, {"logged_out": removed, "message": message}, lambda: message)


# == read commands (task 4) ===============================================================


@app.command("get")
@click.argument("id_", metavar="ID", required=False)
@click.option("--key", "external_key", default=None, help="Look up by external key instead of ID.")
@click.pass_obj
def get_cmd(state: State, id_: str | None, external_key: str | None) -> None:
    """Show one artifact, by ID or by --key (exactly one is required)."""
    if (id_ is None) == (external_key is None):
        raise click.UsageError("give exactly one of ID or --key")
    with make_client(state) as client:
        if id_ is not None:
            artifact = client.artifacts.get(id_)
        else:
            assert external_key is not None  # for mypy: the check above guarantees this
            artifact = client.artifacts.get_by_external_key(external_key)
    emit(state, artifact, lambda: render_fields(artifact_fields(artifact)))


@app.command("ls")
@click.option("--tag", default=None, help="Filter by tag.")
@click.option("--status", "statuses", multiple=True, help="Filter by status (repeatable).")
@click.option(
    "--limit", default=50, type=click.IntRange(min=1), show_default=True, help="Page size."
)
@click.option("--all", "all_", is_flag=True, help="Follow every page instead of just one.")
@click.pass_obj
def ls_cmd(
    state: State, tag: str | None, statuses: tuple[str, ...], limit: int, all_: bool
) -> None:
    """List artifacts."""
    status = list(statuses) or None
    with make_client(state) as client:
        if all_:
            items = list(client.artifacts.iter_all(status=status, tag=tag, limit=limit))
            emit(
                state,
                items,
                lambda: render_table([artifact_row(a) for a in items], ARTIFACT_COLUMNS),
            )
        else:
            page = client.artifacts.list(status=status, tag=tag, limit=limit)
            emit(
                state,
                page,
                lambda: render_table([artifact_row(a) for a in page.items], ARTIFACT_COLUMNS),
            )
            # Only a hint, never data: goes to stderr, and only in table mode, so a JSON
            # consumer sees exactly one document on stdout and nothing extra on stderr.
            if page.next_cursor and not json_mode(state.output, sys.stdout):
                click.echo("more results available: use --all", err=True)


@app.command("search")
@click.argument("query")
@click.option(
    "--mode",
    type=click.Choice(["hybrid", "text", "semantic", "exact"]),
    default="hybrid",
    show_default=True,
    help="Search strategy.",
)
@click.option("--tag", "tags", multiple=True, help="Require this tag (repeatable).")
@click.option(
    "--limit", default=10, type=click.IntRange(min=1), show_default=True, help="Max results."
)
@click.option("--history", is_flag=True, help="Include superseded artifacts (otherwise excluded).")
@click.pass_obj
def search_cmd(
    state: State, query: str, mode: str, tags: tuple[str, ...], limit: int, history: bool
) -> None:
    """Search artifacts."""
    with make_client(state) as client:
        page = client.artifacts.search(
            query=query,
            mode=mode,
            limit=limit,
            tags_all=list(tags),
            exclude_superseded=not history,
        )
    emit(
        state,
        page,
        lambda: render_table([search_hit_row(h) for h in page.items], SEARCH_COLUMNS),
    )


@app.command("link")
@click.argument("id_", metavar="ID")
@click.pass_obj
def link_cmd(state: State, id_: str) -> None:
    """Print a short-lived, signed download URL for an artifact.

    This is the only command that ever prints a signed URL; `--table` prints exactly the
    URL followed by a newline, and nothing else.
    """
    with make_client(state) as client:
        download = client.artifacts.download_url(id_)
    emit(state, download, lambda: download.url)


@app.command("relations")
@click.argument("id_", metavar="ID")
@click.pass_obj
def relations_cmd(state: State, id_: str) -> None:
    """List an artifact's relations to other artifacts."""
    with make_client(state) as client:
        rels = client.artifacts.relations(id_)
    emit(state, rels, lambda: render_table([relation_row(r) for r in rels], RELATION_COLUMNS))


@app.command("versions")
@click.argument("id_", metavar="ID")
@click.pass_obj
def versions_cmd(state: State, id_: str) -> None:
    """List an artifact's versions."""
    with make_client(state) as client:
        vers = client.artifacts.versions(id_)
    emit(state, vers, lambda: render_table([version_row(v) for v in vers], VERSION_COLUMNS))


# == write commands (task 5) ==============================================================


def _parse_meta(
    ctx: click.Context, param: click.Parameter, values: tuple[str, ...]
) -> dict[str, str]:
    """`--meta k=v` (repeatable) -> `{"k": "v", ...}`.

    Splits on the *first* `=` only, so a value may itself contain `=`. An item with no
    `=`, or an empty key, is a usage error (exit 2) rather than a silently-dropped or
    misparsed entry. Later `--meta` for the same key wins - a plain `dict` already does
    this by insertion order.
    """
    result: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise click.BadParameter(f"expected key=value, got {item!r}", ctx, param)
        result[key] = value
    return result


_META_OPTION = click.option(
    "--meta",
    "meta",
    multiple=True,
    callback=_parse_meta,
    metavar="K=V",
    help="Metadata field, key=value (repeatable).",
)


@app.command("push")
@click.argument(
    "file_",
    metavar="FILE",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
)
@click.option("--title", required=True, help="Artifact title.")
@click.option("--tag", "tags", multiple=True, help="Tag (repeatable).")
@click.option("--description", default="", help="Artifact description.")
@click.option("--key", "external_key", default=None, help="External key for this artifact.")
@_META_OPTION
@click.option(
    "--expires", "expires", type=DURATION, default=None, help="Expire after this long (e.g. 30d)."
)
@click.option("--run", "run_id", default=None, help="Attach this upload to a run id.")
@click.option("--summary", default=None, help="A short summary of what changed.")
@click.option(
    "--no-wait", is_flag=True, help="Return once the upload is accepted, without waiting."
)
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=float,
    help="Seconds to wait for the artifact to become ready.",
)
@click.pass_obj
def push_cmd(
    state: State,
    file_: pathlib.Path,
    title: str,
    tags: tuple[str, ...],
    description: str,
    external_key: str | None,
    meta: dict[str, str],
    expires: timedelta | None,
    run_id: str | None,
    summary: str | None,
    no_wait: bool,
    timeout: float,
) -> None:
    """Upload FILE as a new artifact."""
    expires_at = datetime.now(timezone.utc) + expires if expires else None
    with make_client(state) as client:
        artifact = client.artifacts.push(
            file_,
            title=title,
            description=description,
            tags=tags,
            metadata=meta or None,
            external_key=external_key,
            expires_at=expires_at,
            summary=summary,
            run=run_id,
            wait=not no_wait,
            timeout=timeout,
        )
    emit(state, artifact, lambda: render_fields(artifact_fields(artifact)))


@app.command("pull")
@click.argument("id_", metavar="ID")
@click.argument(
    "dest",
    metavar="[DEST]",
    required=False,
    default=".",
    # Plain `click.Path()` (str), not `path_type=pathlib.Path`: the SDK's `_dest_path`
    # treats a *trailing separator on the string* as "this is a directory, create it if
    # missing" (`os.fspath(dest).endswith(("/", os.sep))`). Converting to `Path` here
    # first would silently drop that trailing slash (`Path("out/") == Path("out")`), so
    # `pull ID out/` with `out` absent would write a *file* named `out` instead of
    # creating the directory and writing the artifact's filename inside it.
    type=click.Path(),
)
@click.option(
    "--no-verify", is_flag=True, help="Skip checking the download against its recorded sha256."
)
@click.pass_obj
def pull_cmd(state: State, id_: str, dest: str, no_verify: bool) -> None:
    """Download an artifact's latest version to DEST (a directory by default: `.`)."""
    with make_client(state) as client:
        path = client.artifacts.pull(id_, dest, verify=not no_verify)
    emit(state, {"path": str(path)}, lambda: str(path))


@app.command("rm")
@click.argument("id_", metavar="ID")
@click.pass_obj
def rm_cmd(state: State, id_: str) -> None:
    """Delete an artifact."""
    with make_client(state) as client:
        client.artifacts.delete(id_)
    emit(state, {"deleted": id_}, lambda: f"deleted {id_}")


@app.command("relate")
@click.argument("id_", metavar="ID")
@click.argument("target", metavar="TARGET")
@click.option(
    "--type",
    "relation_type",
    type=click.Choice(sorted(RELATION_TYPES)),
    default="derived_from",
    show_default=True,
    help="The kind of relation from ID to TARGET.",
)
@click.pass_obj
def relate_cmd(state: State, id_: str, target: str, relation_type: str) -> None:
    """Record that artifact ID relates to artifact TARGET."""
    with make_client(state) as client:
        relation = client.artifacts.add_relation(id_, target, relation_type)
    emit(state, relation, lambda: render_table([relation_row(relation)], RELATION_COLUMNS))


def _resolve_table(resolution_status: str, artifact: Artifact) -> str:
    # `artifact_fields` already includes an `("status", a.status)` pair - labelling this
    # first line `resolution:` (never `status:`) keeps it from reading as a second,
    # contradictory status line ("status: hit" followed by "status: ready").
    return "\n".join([f"resolution: {resolution_status}", render_fields(artifact_fields(artifact))])


@app.command("resolve")
@click.argument("key", metavar="KEY")
@click.option(
    "--file",
    "file_",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
    help="File to upload if KEY does not resolve to an existing artifact.",
)
@click.option("--title", required=True, help="Artifact title, used only if a new upload starts.")
@click.option("--tag", "tags", multiple=True, help="Tag (repeatable).")
@click.option("--description", default="", help="Artifact description.")
@_META_OPTION
@click.option(
    "--max-age",
    type=DURATION,
    default=None,
    help="Treat an existing artifact older than this as stale and re-upload (e.g. 30d).",
)
@click.option(
    "--wait",
    "wait_seconds",
    default=60.0,
    show_default=True,
    type=float,
    help="Seconds to wait while another writer holds the reservation.",
)
@click.pass_obj
def resolve_cmd(
    state: State,
    key: str,
    file_: pathlib.Path,
    title: str,
    tags: tuple[str, ...],
    description: str,
    meta: dict[str, str],
    max_age: timedelta | None,
    wait_seconds: float,
) -> None:
    """Get-or-create an artifact by external KEY, uploading FILE only if needed.

    A hit means KEY already resolves to an artifact: nothing is uploaded, and the
    existing artifact is printed. A create means no artifact existed yet: FILE is
    uploaded and the new artifact is printed. Otherwise another writer currently holds
    the reservation for KEY (pending); this is retried until `--wait` seconds have
    passed, then the command exits with code 4.
    """
    prepared = prepare(file_)
    deadline = _monotonic() + wait_seconds
    with make_client(state) as client:
        while True:
            resolution = client.artifacts.resolve(
                key,
                filename=prepared.filename,
                content_type=prepared.content_type,
                size_bytes=prepared.size,
                title=title,
                description=description,
                tags=tags,
                metadata=meta or None,
                max_age=max_age,
            )
            if resolution.status == "hit":
                hit = resolution.artifact
                assert hit is not None  # a "hit" resolution always carries one
                emit(
                    state,
                    {"status": "hit", "artifact": hit},
                    functools.partial(_resolve_table, "hit", hit),
                )
                return
            if resolution.status == "create":
                created = client.artifacts.fulfil(resolution, file_)
                emit(
                    state,
                    {"status": "created", "artifact": created},
                    functools.partial(_resolve_table, "created", created),
                )
                return
            if resolution.status != "pending":
                raise ValueError(f"unexpected resolution status: {resolution.status!r}")
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise ResolvePending(wait_seconds)
            retry_after = resolution.retry_after_seconds or 2
            _sleep(min(retry_after, remaining))


def _run_fields(run: Run) -> list[tuple[str, str]]:
    return [
        ("id", run.id),
        ("project_id", run.project_id),
        ("sealed", "yes" if run.sealed_at else "no"),
        ("created", short_time(run.created_at)),
    ]


@app.group("run")
def run_group() -> None:
    """Create, seal, and inspect runs."""


@run_group.command("new")
@click.argument("run_id", metavar="[RUN_ID]", required=False, default=None)
@click.pass_obj
def run_new_cmd(state: State, run_id: str | None) -> None:
    """Create a run, optionally with a caller-chosen RUN_ID."""
    with make_client(state) as client:
        run = client.runs.create(run_id)
    emit(state, run, lambda: render_fields(_run_fields(run)))


@run_group.command("seal")
@click.argument("run_id", metavar="RUN_ID")
@click.pass_obj
def run_seal_cmd(state: State, run_id: str) -> None:
    """Seal a run so no further artifacts can be attached to it."""
    with make_client(state) as client:
        run = client.runs.seal(run_id)
    emit(state, run, lambda: render_fields(_run_fields(run)))


@run_group.command("ls")
@click.argument("run_id", metavar="RUN_ID")
@click.option(
    "--limit", default=50, type=click.IntRange(min=1), show_default=True, help="Page size."
)
@click.pass_obj
def run_ls_cmd(state: State, run_id: str, limit: int) -> None:
    """List the artifacts attached to a run."""
    with make_client(state) as client:
        page = client.runs.artifacts(run_id, limit=limit)
    emit(state, page, lambda: render_table([artifact_row(a) for a in page.items], ARTIFACT_COLUMNS))

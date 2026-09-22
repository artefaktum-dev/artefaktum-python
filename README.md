# artefaktum

Python client for [ArtefactAI](https://artefaktum.dev): store, find and trust artifacts
exchanged between AI agents, in a few lines, without re-implementing the upload protocol.

## Install

```bash
pip install artefaktum
```

Requires Python 3.10+. The only dependency is `httpx`.

Until the first PyPI release ships (roadmap step 6, `TODO.md`), install from the
repository instead: `pip install -e clients/python`.

## Quick start

```python
from artefaktum import Artefaktum

client = Artefaktum()  # ARTEFAKTUM_API_KEY, project "default"
a = client.artifacts.push("report.pdf", title="Q3 churn", tags=["churn"])
hits = client.artifacts.search("q3 churn")
client.artifacts.pull(hits.items[0].artifact.id, "out/")
```

`push` accepts a path (streamed, never held whole in memory) or `bytes`. `pull` writes to
a directory or an exact file path, and returns the final `Path`. A destination is a
directory when it already is one, or when it is a *string* ending in `/` — `"out/"`
above, which `pull` creates; a `pathlib.Path` cannot say it, since `Path("out/")` is just
`Path("out")`. Into a directory, the file is named after the version's stored filename,
reduced to a basename so an uploader cannot steer the write out of the directory.

`pull` verifies the sha256 digest by default and raises `IntegrityError` on a mismatch.
A version the server recorded no digest for is written unverified — `verify=True` is the
default, not a guarantee, and cannot force a check the server has nothing to check
against.

## Command line

The `cli` extra installs `artefaktum` (and the shorter alias `afk`), a click-based CLI
over the same SDK: `pip install "artefaktum[cli]"`. That extra isn't on PyPI yet
(roadmap step 6, `TODO.md`); until then, install it from the repository:
`pip install -e "clients/python[cli]"`. Without the extra, the entry points still exist
and print `the CLI needs an extra: pip install "artefaktum[cli]"`.

Log in once — the key is never echoed and never taken as a positional argument, so it
can't land in shell history. `artefaktum login` verifies the key against the server,
then saves it to `~/.config/artefaktum/config.json` (`$XDG_CONFIG_HOME`, or
`%APPDATA%\artefaktum` on Windows), mode `0600`. For headless use (CI, another agent),
prefer `ARTEFAKTUM_API_KEY` over `--api-key`, which shows up in `ps` for other users on
the same host; precedence is `--api-key`, then `ARTEFAKTUM_API_KEY`, then the config file.

The four commands used most:

```bash
artefaktum push report.pdf --title "Q3 churn" --tag churn
artefaktum search "q3 churn"
artefaktum pull 0199198a-8f21-... out/
artefaktum get --key vendor-x/report/2026-09
```

Output is an aligned text table on a terminal and one JSON document otherwise — global
`--json` / `--table` (before the command) force either — so scripts need no flag at
all, only a pipe: `artefaktum ls | jq -r '.items[].id'` (a page); `ls --all | jq -r
'.[].id'` (every match, printed as a bare array instead of a page).

`resolve KEY --file F --title T` is get-or-create for an external key: `hit` prints the
existing artifact and uploads nothing; `create` uploads `F` and prints the new one;
`pending` (another writer holds the reservation) is retried until `--wait` seconds pass,
then the command exits `4`. A plain existence check with no upload is `get --key`.

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | API or SDK error |
| 2 | usage error |
| 3 | not found |
| 4 | `resolve` still pending when `--wait` ran out |
| 130 | interrupted |

Which makes "push only if it isn't already there" one line:

```bash
artefaktum get --key vendor-x/report/2026-09 >/dev/null \
  || artefaktum push report.json --title "Vendor X report" --key vendor-x/report/2026-09
```

`artefaktum --help` and `artefaktum <command> --help` list every command and flag.

## Configuration

Resolved in order: explicit argument, then environment variable, then default.

| Argument | Environment variable | Default |
|---|---|---|
| `api_key` | `ARTEFAKTUM_API_KEY` | *(required)* |
| `base_url` | `ARTEFAKTUM_BASE_URL` | `https://api.artefaktum.dev` |
| `project` | `ARTEFAKTUM_PROJECT` | `"default"` |

```python
client = Artefaktum(api_key="ak_...", base_url="http://localhost:3000", project="default")
```

`project` is a slug or a UUID. A slug is resolved to a project id through
`GET /v1/projects` once, and the client's default is cached for its lifetime.
Project-scoped methods (`push`, `create_upload`, `get_by_external_key`, `list`,
`iter_all`, `search`, `resolve`, `runs.create`, `keys.create`, `usage.get`) take a
per-call `project=` override, resolved the same way but not cached; methods addressed by
an artifact, run or key id (`get`, `pull`, `delete`, and the rest) take no `project`.

## Async

`AsyncArtefaktum` mirrors every method on `Artefaktum` as a coroutine (`iter_all` becomes
an async generator), sharing the same request-building and response-parsing code:

```python
import asyncio
from artefaktum import AsyncArtefaktum


async def main():
    async with AsyncArtefaktum() as client:
        a = await client.artifacts.push("report.pdf", title="Q3 churn")
        async for artifact in client.artifacts.iter_all(tag="churn"):
            print(artifact.id)


asyncio.run(main())
```

## Errors

Every failure is an `ArtefaktumError(message, code, status, request_id)` or a subclass,
so callers can branch on the type or the `code`, and quote `request_id` to support:

```python
from artefaktum import Artefaktum, NotFound, ArtefaktumError

with Artefaktum() as client:
    try:
        client.artifacts.get("01a0be89-1cde-74c1-ab17-8814cc9d9141")  # no such artifact
    except NotFound as e:
        print(e.code, e.request_id)  # "artifact_not_found", "req_..."
    except ArtefaktumError as e:
        print(str(e))  # "<code>: <message> (request_id=<id>)"
```

Subclasses: `NotFound`, `Unauthorized`, `Forbidden`, `QuotaExceeded`, `Conflict`,
`ValidationFailed`, `UploadError`, `ServiceUnavailable`. Push and pull also raise
`ProcessingFailed` / `ProcessingTimeout` (carrying the `artifact`), `IntegrityError` (a
pull sha256 mismatch) and `StorageError` (a storage-host failure). `MissingApiKey` (a
`ValueError`) is raised locally, before any request is made.

`QuotaExceeded` means the plan's limit is reached: status 413 for a file that is too
large or storage that is full, 429 for the month's API calls. When the calls are spent,
writes and `search` stop; reads, downloads and deletes keep working. `client.quota()`
returns the plan, the limits and what is used, and is never blocked:

```python
q = client.quota()
print(q.plan, q.storage.used_bytes, "/", q.storage.limit_bytes, q.calls.resets_at)
```

## What `push` does

1. Hashes and sizes the source locally, streamed; guesses a content type from the
   filename unless one is given.
2. Reserves an upload (`create_upload`), which returns a signed PUT URL.
3. Uploads the bytes directly to object storage, over a second HTTP client that carries
   **no** `Authorization` header — the API key never reaches the storage host.
4. Marks the version complete (`complete_upload`) with the digest and size, so the server
   can verify the object it received.
5. By default, polls until the artifact is `ready`, raising `ProcessingFailed` or
   `ProcessingTimeout` on the way; `wait=False` returns right after step 4, with status
   `processing` or `ready`.

`create_version` and `fulfil` reuse the same upload/complete/wait flow; `resolve` only
checks whether an artifact exists for an external key and returns a `Resolution` — when
its `status` is `"create"`, hand it and the bytes to `fulfil`, which does the upload.

Signed URLs (`UploadInstructions.url`, `Download.url`) and a freshly minted key
(`CreatedKey.secret`) are bearer credentials, so `repr()` redacts them — but the
attributes still hold the real values, so log the objects, never their fields.

## Retries

Reads are retried automatically on HTTP 429, 502, 503 or 504, and on connection errors, up
to 3 attempts, waiting 0.5 s and then 1 s (or the server's `Retry-After`, capped at 10 s):
`get`,
`get_by_external_key`, `list` / `iter_all`, `search`, `download_url`, `whoami`,
`projects.list`, `usage.get` and the other listing calls. `search` is a POST (its filter
body is too large for a query string) but has no side effects, so it is retried too.
A 429 with code `quota_exceeded` is the exception: it is raised at once, because the
allowance does not come back until `quota().calls.resets_at`.
Writes — `push`, `update`, `add_relation`, `delete`, `keys.create` and the rest — are
never retried automatically, so a client never double-submits one; nor is the storage
upload/download.

## Issues and contributions

Bug reports and questions belong in this repository's issue tracker. The code here is
mirrored out of a private monorepo it shares with the server, so a pull request cannot be
merged directly -- it will be applied upstream and land here in the next release, with the
author credited. Small fixes are welcome that way.

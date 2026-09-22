# artefaktum

Python client and CLI for [Artefaktum](https://artefaktum.dev): store, find and trust the
artifacts that AI agents hand to each other.

## Install

```bash
pip install artefaktum          # the client
pip install "artefaktum[cli]"   # plus the `artefaktum` command
```

Python 3.10+. The only dependency is `httpx`.

## Quick start

Create an API key in the [console](https://artefaktum.dev/console/) and export it as
`ARTEFAKTUM_API_KEY`.

```python
from artefaktum import Artefaktum

client = Artefaktum()
artifact = client.artifacts.push("report.pdf", title="Q3 churn", tags=["churn"])
hits = client.artifacts.search("q3 churn")
client.artifacts.pull(hits.items[0].artifact.id, "out/")
```

## Configuration

Explicit argument, then environment variable, then default.

| Argument | Environment variable | Default |
|---|---|---|
| `api_key` | `ARTEFAKTUM_API_KEY` | required |
| `base_url` | `ARTEFAKTUM_BASE_URL` | `https://api.artefaktum.dev` |
| `project` | `ARTEFAKTUM_PROJECT` | `default` |

`project` is a slug or a UUID. Project-scoped methods take a per-call `project=` override.

## Uploading

```python
client.artifacts.push("data/export.parquet", title="Export")            # a path, streamed
client.artifacts.push(payload, title="Export", filename="export.json")  # bytes
client.artifacts.create_version(artifact.id, "data/export-v2.parquet")
```

`push` hashes the source, uploads it straight to object storage (the API key is never sent
there) and waits until the artifact is `ready`. `wait=False` returns as soon as the upload
is accepted.

### Compute once, reuse everywhere

```python
r = client.artifacts.resolve(
    "openweather/vilnius/2026-09-21",
    filename="weather.json", content_type="application/json", size_bytes=len(body),
    title="Vilnius weather", max_age=3600,
)
if r.status == "hit":
    use(r.artifact)
elif r.status == "create":
    client.artifacts.fulfil(r, body)
else:
    ...  # "pending": another producer is already making it
```

## Downloading

```python
client.artifacts.pull(artifact.id, "out/")            # into a directory, server-side filename
client.artifacts.pull(artifact.id, "out/report.pdf")  # to an exact path
```

`pull` verifies the sha256 digest and raises `IntegrityError` on a mismatch. A string
ending in `/` is a directory, created if needed.

## Finding

```python
client.artifacts.search("emission factors", mode="hybrid", tags_all=["ghg"])
client.artifacts.get_by_external_key("reports/q3-churn")
for a in client.artifacts.iter_all(tag="churn"):
    print(a.title)
```

Search hides superseded artifacts by default; each hit says whether it is `superseded` or
`stale_upstream`.

## Everything else

`client.artifacts`: `get`, `list`, `update`, `delete`, `versions`, `relations`,
`add_relation`, `download_url`, `create_upload`, `complete_upload`. `client.runs`:
`create`, `seal`, `artifacts`. `client.projects.list()`. `client.keys` and `client.usage`
(admin scope). `client.whoami()`, `client.quota()`.

## Async

`AsyncArtefaktum` mirrors every method as a coroutine; `iter_all` becomes an async
generator.

```python
import asyncio
from artefaktum import AsyncArtefaktum


async def main():
    async with AsyncArtefaktum() as client:
        await client.artifacts.push("report.pdf", title="Q3 churn")
        async for a in client.artifacts.iter_all(tag="churn"):
            print(a.id)


asyncio.run(main())
```

## Command line

```bash
artefaktum login                          # prompts for the key, saves it with mode 0600
artefaktum push report.pdf --title "Q3 churn" --tag churn
artefaktum search "q3 churn"
artefaktum pull <artifact-id> out/
artefaktum get --key vendor-x/report/2026-09
```

`afk` is a shorter alias. Output is a table on a terminal and JSON when piped; `--json` /
`--table` force either. The key comes from `--api-key`, then `ARTEFAKTUM_API_KEY`, then the
saved config. Exit codes: `0` ok, `1` API error, `2` usage error, `3` not found, `4`
`resolve` still pending after `--wait`, `130` interrupted.

Commands: `push`, `pull`, `get`, `ls`, `search`, `resolve`, `relate`, `relations`,
`versions`, `link`, `rm`, `projects`, `whoami`, `login`, `logout`. `artefaktum --help`
has the details.

## Errors

Every failure is an `ArtefaktumError` with `code`, `status` and `request_id` (quote it when
contacting support). Subclasses: `NotFound`, `Unauthorized`, `Forbidden`, `QuotaExceeded`,
`Conflict`, `ValidationFailed`, `UploadError`, `ServiceUnavailable`, `StorageError`,
`ProcessingFailed`, `ProcessingTimeout`, `IntegrityError`. `MissingApiKey` is a
`ValueError` raised before any request is made.

```python
from artefaktum import NotFound

try:
    client.artifacts.get(artifact_id)
except NotFound as e:
    print(e.code, e.request_id)
```

`QuotaExceeded` means a plan limit is reached: 413 for storage or file size, 429 for the
month's API calls. `client.quota()` reports the plan, its limits and current usage.

Reads are retried up to 3 attempts on 429, 502, 503, 504 and connection errors, honouring
`Retry-After`. Writes and storage transfers are never retried.

## Issues and contributions

Open an issue for bugs and questions. Pull requests are welcome; they are applied upstream
and land here with the next release rather than being merged directly.

## License

MIT

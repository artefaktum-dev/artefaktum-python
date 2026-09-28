# Changelog

## 0.2.0

Breaking:

- `summary` is removed. `push`, `create_upload` and `create_version` no longer take a
  `summary` argument, `Version` has no `summary` field, and the CLI has no `--summary`
  option. The API refuses a request that still sends `summary`, so versions up to 0.1.1
  fail an upload when a summary is set.

Added:

- `RequestTooLarge`, raised for HTTP 413 with code `request_too_large`: the JSON request
  is over 256 KB. It is separate from `QuotaExceeded`.

Changed on the server:

- One tag is 1 to 64 characters. `metadata` is at most 16,384 bytes as compact JSON and
  nested at most 5 levels. A value over a limit raises `ValidationFailed`, and its message
  names the limit. See <https://artefaktum.dev/docs/rest/#limits>.

## 0.1.1

- README rewritten. No code changes.

## 0.1.0

- First release.

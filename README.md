# apifuse

`apifuse` is a user-space FUSE proof of concept that projects a REST API described by OpenAPI into a read-only filesystem.

It can also mount a local JSON document directly as a static filesystem tree.

It is designed around a simple idea:

- collections such as `/users/` become directories
- resource ids such as `/users/3` become child directories
- scalar fields become readable files
- nested objects and arrays become subdirectories

## Current Status

This is an early prototype. The current implementation focuses on:

- OpenAPI-driven discovery of top-level collection and item `GET` routes
- JSON-file offline mode (`--json-input`)
- auth-token auth
- strict schema-aware path filtering
- short-lived response caching
- optional collection-level symlink aliases such as `users/alice -> 3`

It is currently read-only. `POST`, `PUT`, `PATCH`, and `DELETE` are not implemented yet.

## Requirements

- Python
- a working FUSE/macFUSE environment
- `mfusepy`

This repo already uses a local virtualenv in `.venv/`.

## Install

If you are using the existing local venv:

```bash
./.venv/bin/python -m pip list | grep mfuse
```

If you need to install dependencies:

```bash
uv pip install -r pyproject.toml
```

## Basic Usage

OpenAPI mode:

Mount using a local OpenAPI file and an explicit server URL:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  /tmp/mnt_apifuse
```

Then inspect the mounted tree:

```bash
ls /tmp/mnt_apifuse
ls /tmp/mnt_apifuse/users
ls /tmp/mnt_apifuse/users/3
cat /tmp/mnt_apifuse/users/3/username
```

JSON mode:

Mount a local JSON file directly with no API calls:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --json-input ./repos.json \
  /tmp/mnt_apifuse_json
```

Then inspect:

```bash
ls /tmp/mnt_apifuse_json
ls /tmp/mnt_apifuse_json/0
cat /tmp/mnt_apifuse_json/0/name
```

## Authentication

Auth tokens can be supplied in three ways:

- `--auth-token <token>`
- `--auth-token-file <path>`
- `--auth-token-env <ENV_NAME>` (defaults to `APIFUSE_auth_token`)

The token value should be the raw token string. By default, `apifuse` sends it as `Authorization: Bearer <token>`.

Header formatting is configurable:

- `--auth-header` (default: `Authorization`)
- `--auth-scheme` (default: `Bearer`)

Optional refresh-on-401 support:

- `--refresh-url`
- `--refresh-token` / `--refresh-token-file` / `--refresh-token-env`
- `--refresh-body-token-key` (default: `refresh_token`)
- `--refresh-response-token-key` (default: `access_token`)

Optional refresh discovery from API responses (disabled by default):

- `--discover-refresh-from-response`
- `--refresh-discovery-path <prefix>` (repeatable allowlist; when omitted, only auth-like paths are considered)
- `--refresh-discovery-url-key <json_key>` (repeatable; defaults: `refresh_url`, `refresh_endpoint`, `token_refresh_url`)
- `--refresh-discovery-token-key <json_key>` (repeatable; default: `refresh_token`)

Precedence rule: explicit CLI/env/file refresh values win; discovered values only fill missing `refresh_url`/`refresh_token`.

## Symlink Aliases

`apifuse` can expose collection-level aliases that point at the canonical resource id entry.

Example:

```text
products/
  1/
  2/
  dollhouse -> 2
```

Enable common name-like aliases:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --symlink-names \
  /tmp/mnt_apifuse
```

Add explicit alias mappings:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --symlink-map users=username \
  --symlink-map products=title \
  --symlink-map products=categories/name \
  /tmp/mnt_apifuse
```

`--symlink-map` syntax is:

```text
<collection>=<field-path>
```

The right-hand side is a path inside each resource object, not an API path.

With `--json-input`, aliases are created at the root when the JSON document is a top-level list. For JSON-mode mappings, use either `root=<field-path>` or just `<field-path>`.

## Caching

Short-lived caching is enabled to protect the local machine and the API from noisy filesystem clients.

Relevant flags:

- `--cache-ttl`
- `--error-cache-ttl`
- `--cache-max-entries`
- `--probe-limit`

## Bootstrap Failure Policy

In OpenAPI mode, apifuse performs a startup probe of sampled endpoints.

- default: mount fails only if none of the sampled endpoints are reachable
- `-f` / `--force`: mount anyway, even if bootstrap validation fails

Example:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --cache-ttl 5 \
  --error-cache-ttl 2 \
  --probe-limit 10 \
  /tmp/mnt_apifuse
```

## Logging

Enable debug logging:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --debug \
  /tmp/mnt_apifuse
```

Write logs to a file:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --debug \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse
```

## macOS Caveat

Foreground mode is the default and is the recommended mode.

On macOS, libfuse's internal daemon mode has been unreliable in testing. If you need background behavior, keep `apifuse` in foreground mode and background the process externally:

```bash
nohup env PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse \
  >/tmp/apifuse.stdout 2>&1 &
```

If you explicitly want libfuse daemon mode anyway, use:

```bash
PYTHONPATH=src ./.venv/bin/python -m apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --daemonize \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse
```

## Unmount

On macOS:

```bash
umount /tmp/mnt_apifuse
```

If the mount is stuck:

```bash
umount -f /tmp/mnt_apifuse
```

## Next Steps

Planned follow-up work:

- stricter schema/exploratory mode split
- smarter alias caching and alias derivation from collection payloads
- writable CRUD mappings for create/update/delete
- quieter handling of common macOS metadata probes

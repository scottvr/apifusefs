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
- JSON-file offline mode (`--mode json`)
- bearer-token auth
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
./.venv/bin/python apifuse.py \
  --mode openapi \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
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
./.venv/bin/python apifuse.py \
  --mode json \
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

Bearer auth can be supplied in three ways:

- `--bearer-token <token>`
- `--bearer-token-file <path>`
- `--bearer-token-env <ENV_NAME>` (defaults to `APIFUSE_BEARER_TOKEN`)

The token value should be the raw bearer token string. `apifuse` adds the `Authorization: Bearer ...` header itself.

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
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
  --symlink-names \
  /tmp/mnt_apifuse
```

Add explicit alias mappings:

```bash
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
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

In `--mode json`, aliases are created at the root when the JSON document is a top-level list. For JSON mode mappings, use either `root=<field-path>` or just `<field-path>`.

## Caching

Short-lived caching is enabled to protect the local machine and the API from noisy filesystem clients.

Relevant flags:

- `--cache-ttl`
- `--error-cache-ttl`
- `--cache-max-entries`
- `--probe-limit`

Example:

```bash
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
  --cache-ttl 5 \
  --error-cache-ttl 2 \
  --probe-limit 10 \
  /tmp/mnt_apifuse
```

## Logging

Enable debug logging:

```bash
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
  --debug \
  /tmp/mnt_apifuse
```

Write logs to a file:

```bash
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
  --debug \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse
```

## macOS Caveat

Foreground mode is the default and is the recommended mode.

On macOS, libfuse's internal daemon mode has been unreliable in testing. If you need background behavior, keep `apifuse` in foreground mode and background the process externally:

```bash
nohup ./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse \
  >/tmp/apifuse.stdout 2>&1 &
```

If you explicitly want libfuse daemon mode anyway, use:

```bash
./.venv/bin/python apifuse.py \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --bearer-token-file ./bearer.token \
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

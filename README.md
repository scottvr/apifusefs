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

## Suggested Installation

- Create a new venv if needed 
e.g.,
```bash
python -mvenv .venv
```
or using `uv`, `pipx`, etc.  if you prefer.

- Install dependencies as needed, using your package manager of choice:
```bash
uv pip install -e .
```

Since `apifuse` is not yet packaged for PyPi and you are running from 
the cloned repo, the `-e` (editable) method is recommended.

An entry-point to the cli will be installed into your venv's bin path such
that it will then be available just by invoking `apifuse` from within your
shell with the venv activated.  e.g., with (`source .venv/bin/activate` or `.\venv\Scripts\activate`)

## Basic Usage

**OpenAPI (HTTP/REST) mode:**

Mount using a local OpenAPI/swagger json or yaml file and an explicit server URL:

```bash
apifuse \
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
cat /tmp/mnt_apifuse/users/3/groups
```

**JSON (local file) mode:**

Mount a local JSON file directly with no API callsi.
Here I am using the output of a call to the GitHub API
endpoint `/user/repos`a:


```bash
apifuse \
  --json-input ./repos.json \
  --symlink-names \
  /tmp/mnt_apifuse_json
```

Then inspect:

```bash
ls /tmp/mnt_apifuse_json
ls /tmp/mnt_apifuse_json/0/
cat /tmp/mnt_apifuse_json/0/name
```

With the `--symlink-names` flag, the contents of that `name` file (field from the json)
have already been read and applied as a symlink in the root directory of the mount so that
you don't have to do such things as shown above in order to know which repo `0` refers to:


```bash
apifuse \
  --json-input ./repos.json \
  --symlink-names \
  /tmp/mnt_apifuse_json
```

Then inspect:

```bash
$ ls /tmp/mnt_apifuse_json
$ cat /tmp/mnt_apifuse_json/apifuse/name
apifuse
$ cat /tmp/mnt_apifuse_json/0/name
apifuse
$ readlink -f /tmp/mnt_apifuse_json/apifuse
/tmp/mnt_apifuse_json/0
$ readlink -f /tmp/mnt_apifuse_json/0
/tmp/mnt_apifuse_json/0
```

## Symlinking values to Collection names

Note: `--symlink-names` works exactly the same in `OpenAI` mode as what is shown above in
the JSON example wrt to the filesystem; it just retrieves the data with an HTTP call as per
the API spec.

In addition to the symlinking using the values within common field names such as `name`,  `username`, `title`, `SLUG`, explicit alias mappings can be achieved with `--symlink-map`:  

Add explicit alias mappings:

```bash
apifuse \
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

## Authentication

Auth tokens can be supplied in three ways:

- `--auth-token <token>`
- `--auth-token-file <path>`
- `--auth-token-env <ENV_NAME>` (defaults to `APIFUSE_auth_token`)
- `--auth-json-file <path>` (optional bundle input for `access_token`/`refresh_token`/`refresh_url`)

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
For `--auth-json-file`, JSON-derived values are only used when token/refresh values are not already provided by direct CLI, token-file, or configured token-env values.


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
apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --cache-ttl 5 \
  --error-cache-ttl 2 \
  --probe-limit 10 \
  /tmp/mnt_apifuse
```

## Logging

Enable debug logging with `--debug`

```bash
apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --debug \
  /tmp/mnt_apifuse
```

Write logs to a filei with `--log-file`:

```bash
apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --debug \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse
```

## macOS Caveat

Running `apifuse` in foreground mode is the default and is the recommended mode.

On macOS, libfuse's internal daemon mode has been unreliable in testing. If you need background behavior, keep `apifuse` in foreground mode and background the process externally:

```bash
nohup env apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse \
  >/tmp/apifuse.stdout 2>&1 &
```

If you explicitly want `apifuse` to use `libfuse daemon mode` despite the warnings, use:

```bash
apifuse \
  --api-spec ./openapi.json \
  --server-url http://127.0.0.1:8000 \
  --auth-token-file ./bearer.token \
  --daemonize \
  --log-file /tmp/apifuse.log \
  /tmp/mnt_apifuse
```

## Unmount

Ensure the `apifuse` process controlling this mount is stopped. Then run:

```bash
umount /tmp/mnt_apifuse
```

If the mount is stuck:

```bash
umount -f /tmp/mnt_apifuse
```

## Next Steps

Planned follow-up work:

- writable CRUD mappings for create/update/delete (Smart mappings between filesystem operation intent and HTTP POST/PUT/PATCH/DELETE)

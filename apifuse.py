#!/usr/bin/env python3

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import mfusepy as fuse

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileNode:
    content: bytes


@dataclass(frozen=True)
class EndpointDefinition:
    name: str
    base_path: str
    list_path: str | None
    item_path: str | None
    item_parameter: str | None
    summary: str | None
    description: str | None
    operation_id: str | None
    responses: dict[str, Any]


class APISpecError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class APIFuse(fuse.Operations):
    """
    Read-only FUSE filesystem backed by an OpenAPI document.

    Layout:
    - `/` lists top-level collection names such as `users`.
    - `/users/` lists resource ids, discovered from `GET /users` or by probing `GET /users/{id}`.
    - `/users/3/` exposes the fetched object as files and subdirectories.
    - Scalars become files whose contents are the scalar value plus a trailing newline.
    - Dicts and arrays become directories.
    """

    def __init__(
        self,
        api_spec: str,
        server_url: str | None = None,
        timeout: float = 10.0,
        bearer_token: str | None = None,
        bearer_token_file: str | None = None,
        bearer_token_env: str | None = "APIFUSE_BEARER_TOKEN",
        probe_limit: int = 10,
    ) -> None:
        self.timeout = timeout
        self.spec_source = api_spec
        self.server_url_override = server_url.rstrip("/") if server_url else None
        self.bearer_token = self._resolve_bearer_token(
            bearer_token=bearer_token,
            bearer_token_file=bearer_token_file,
            bearer_token_env=bearer_token_env,
        )
        self.probe_limit = max(0, probe_limit)
        self._ssl_context = ssl.create_default_context()
        self.spec = self._load_spec(api_spec)
        self.base_url = self._determine_base_url(api_spec, self.spec, self.server_url_override)
        self.endpoints = self._discover_endpoints(self.spec)
        self._dir_mode = stat.S_IFDIR | 0o755
        self._file_mode = stat.S_IFREG | 0o444

    def access(self, path: str, mode: int) -> int:
        try:
            if mode & os.W_OK:
                raise fuse.FuseOSError(errno.EROFS)
            self.getattr(path)
            return 0
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("access failed for %s", path)
            raise self._unexpected_fuse_error(exc) from exc

    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
        try:
            now = time.time()
            normalized = self._normalize_path(path)
            if normalized == "/":
                return self._stat_for_dir(now)
            if self._is_directory(normalized):
                return self._stat_for_dir(now)
            node = self._get_file_node(normalized)
            if node is not None:
                return self._stat_for_file(now, len(node.content))
            raise fuse.FuseOSError(errno.ENOENT)
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("getattr failed for %s", path)
            raise self._unexpected_fuse_error(exc) from exc

    def open(self, path: str, flags: int) -> int:
        try:
            if flags & os.O_WRONLY or flags & os.O_RDWR:
                raise fuse.FuseOSError(errno.EROFS)
            if self._get_file_node(self._normalize_path(path)) is None:
                raise fuse.FuseOSError(errno.ENOENT)
            return 0
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("open failed for %s", path)
            raise self._unexpected_fuse_error(exc) from exc

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        try:
            node = self._get_file_node(self._normalize_path(path))
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            return node.content[offset : offset + size]
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("read failed for %s", path)
            raise self._unexpected_fuse_error(exc) from exc

    def readdir(self, path: str, fh: int) -> list[str]:
        try:
            normalized = self._normalize_path(path)
            entries = [".", ".."]
            if normalized == "/":
                entries.extend(sorted(self.endpoints))
                return entries

            path_type, endpoint, remainder = self._classify_path(normalized)
            if path_type == "collection":
                entries.extend(self._list_collection_entries(endpoint))
                return entries
            if path_type == "resource_dir":
                try:
                    entries.extend(self._list_resource_entries(endpoint, remainder))
                except APISpecError as exc:
                    raise self._to_fuse_error(exc) from exc
                return entries
            raise fuse.FuseOSError(errno.ENOENT)
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("readdir failed for %s", path)
            raise self._unexpected_fuse_error(exc) from exc

    def statfs(self, path: str) -> dict[str, int]:
        return {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": 1,
            "f_bfree": 0,
            "f_bavail": 0,
            "f_files": 4096,
            "f_ffree": 0,
            "f_favail": 0,
            "f_flag": 0,
            "f_namemax": 255,
        }

    def _stat_for_dir(self, now: float) -> dict[str, Any]:
        return {
            "st_mode": self._dir_mode,
            "st_nlink": 2,
            "st_size": 0,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
        }

    def _stat_for_file(self, now: float, size: int) -> dict[str, Any]:
        return {
            "st_mode": self._file_mode,
            "st_nlink": 1,
            "st_size": size,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
        }

    def _normalize_path(self, path: str) -> str:
        normalized = os.path.normpath(path)
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    def _split_path(self, path: str) -> list[str]:
        if path == "/":
            return []
        return [part for part in path.strip("/").split("/") if part]

    def _classify_path(
        self, path: str
    ) -> tuple[str | None, EndpointDefinition | None, list[str]]:
        parts = self._split_path(path)
        if not parts:
            return "root", None, []
        endpoint = self.endpoints.get(parts[0])
        if endpoint is None:
            return None, None, []
        if len(parts) == 1:
            return "collection", endpoint, []
        if len(parts) == 2 and parts[1] in {".meta.json", ".error.txt"}:
            return "collection_file", endpoint, [parts[1]]
        return "resource_dir", endpoint, parts[1:]

    def _is_directory(self, path: str) -> bool:
        path_type, endpoint, remainder = self._classify_path(path)
        if path_type in {"root", "collection"}:
            return True
        if path_type != "resource_dir" or endpoint is None:
            return False
        try:
            return self._resolve_resource_node(endpoint, remainder)[0] == "dir"
        except APISpecError:
            return False

    def _get_file_node(self, path: str) -> FileNode | None:
        path_type, endpoint, remainder = self._classify_path(path)
        if path_type == "collection_file" and endpoint is not None:
            if remainder[0] == ".meta.json":
                return self._endpoint_meta_file(endpoint)
            if remainder[0] == ".error.txt":
                return self._collection_error_file(endpoint)
        if path_type != "resource_dir" or endpoint is None:
            return None
        try:
            kind, value = self._resolve_resource_node(endpoint, remainder)
        except APISpecError:
            return None
        if kind != "file":
            return None
        assert isinstance(value, bytes)
        return FileNode(content=value)

    def _endpoint_meta_file(self, endpoint: EndpointDefinition) -> FileNode:
        body = json.dumps(
            {
                "base_path": endpoint.base_path,
                "list_path": endpoint.list_path,
                "item_path": endpoint.item_path,
                "item_parameter": endpoint.item_parameter,
                "summary": endpoint.summary,
                "description": endpoint.description,
                "operationId": endpoint.operation_id,
                "responses": endpoint.responses,
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        return FileNode(content=body)

    def _collection_error_file(self, endpoint: EndpointDefinition) -> FileNode | None:
        error = self._collection_error(endpoint)
        if error is None:
            return None
        return FileNode(content=f"{error}\n".encode("utf-8"))

    def _collection_error(self, endpoint: EndpointDefinition) -> str | None:
        if endpoint.list_path is not None:
            try:
                self._fetch_collection_ids(endpoint)
                return None
            except APISpecError as exc:
                if endpoint.item_path is None:
                    return str(exc)
        if endpoint.item_path is not None:
            try:
                discovered = self._probe_resource_ids(endpoint)
            except APISpecError as exc:
                return str(exc)
            if discovered:
                return None
            return (
                f"list endpoint unavailable and no ids were discovered by probing "
                f"0..{max(self.probe_limit - 1, 0)}"
            )
        return "no readable endpoint is available for this collection"

    def _list_collection_entries(self, endpoint: EndpointDefinition) -> list[str]:
        entries = [".meta.json"]
        ids: list[str] = []
        error_message: str | None = None

        if endpoint.list_path is not None:
            try:
                ids.extend(self._fetch_collection_ids(endpoint))
            except APISpecError as exc:
                error_message = str(exc)

        if not ids and endpoint.item_path is not None:
            try:
                ids.extend(self._probe_resource_ids(endpoint))
                if ids:
                    error_message = None
            except APISpecError as exc:
                if error_message is None:
                    error_message = str(exc)

        entries.extend(sorted(set(ids), key=self._sort_key))
        if error_message and not ids:
            entries.append(".error.txt")
        return entries

    def _fetch_collection_ids(self, endpoint: EndpointDefinition) -> list[str]:
        if endpoint.list_path is None:
            return []
        payload = self._fetch_json_path(endpoint.list_path)
        items = self._extract_collection_items(payload)
        ids: list[str] = []
        for index, item in enumerate(items):
            ids.append(self._item_identifier(item, index))
        return ids

    def _probe_resource_ids(self, endpoint: EndpointDefinition) -> list[str]:
        if endpoint.item_path is None:
            return []
        found: list[str] = []
        for candidate in range(self.probe_limit):
            identifier = str(candidate)
            try:
                self._fetch_resource_response(endpoint, identifier)
            except APISpecError as exc:
                if exc.status_code in {400, 404}:
                    continue
                raise
            found.append(identifier)
        return found

    def _resolve_resource_node(
        self, endpoint: EndpointDefinition, remainder: list[str]
    ) -> tuple[str | None, bytes | list[str] | None]:
        if not remainder:
            return None, None

        resource_id = remainder[0]
        response = self._fetch_resource_response(endpoint, resource_id)
        resource_root = self._extract_resource_root(response)

        if len(remainder) == 1:
            return "dir", None
        if len(remainder) == 2 and remainder[1] == ".raw.json":
            body = json.dumps(response, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            return "file", body

        node = resource_root
        for part in remainder[1:]:
            if isinstance(node, dict):
                if part not in node:
                    return None, None
                node = node[part]
                continue
            if isinstance(node, list):
                if not part.isdigit():
                    return None, None
                index = int(part)
                if index < 0 or index >= len(node):
                    return None, None
                node = node[index]
                continue
            return None, None

        if isinstance(node, (dict, list)):
            return "dir", None
        return "file", self._encode_scalar(node)

    def _list_resource_entries(
        self, endpoint: EndpointDefinition, remainder: list[str]
    ) -> list[str]:
        if not remainder:
            raise fuse.FuseOSError(errno.ENOENT)

        resource_id = remainder[0]
        response = self._fetch_resource_response(endpoint, resource_id)
        resource_root = self._extract_resource_root(response)

        if len(remainder) == 1:
            entries = [".raw.json"]
            entries.extend(self._list_child_names(resource_root))
            return entries

        node = resource_root
        for part in remainder[1:]:
            if isinstance(node, dict):
                if part not in node:
                    raise fuse.FuseOSError(errno.ENOENT)
                node = node[part]
                continue
            if isinstance(node, list):
                if not part.isdigit():
                    raise fuse.FuseOSError(errno.ENOENT)
                index = int(part)
                if index < 0 or index >= len(node):
                    raise fuse.FuseOSError(errno.ENOENT)
                node = node[index]
                continue
            raise fuse.FuseOSError(errno.ENOENT)

        if not isinstance(node, (dict, list)):
            raise fuse.FuseOSError(errno.ENOTDIR)
        return self._list_child_names(node)

    def _list_child_names(self, node: Any) -> list[str]:
        if isinstance(node, dict):
            return sorted(node.keys(), key=self._sort_key)
        if isinstance(node, list):
            return [str(index) for index in range(len(node))]
        return []

    def _fetch_resource_response(self, endpoint: EndpointDefinition, resource_id: str) -> Any:
        if endpoint.item_path is None or endpoint.item_parameter is None:
            raise APISpecError(f"{endpoint.name} does not define a GET item endpoint")
        quoted_id = urllib.parse.quote(resource_id, safe="")
        api_path = endpoint.item_path.replace(
            f"{{{endpoint.item_parameter}}}",
            quoted_id,
        )
        return self._fetch_json_path(api_path)

    def _extract_collection_items(self, payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "results", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise APISpecError("collection endpoint did not return a JSON array-like payload")

    def _extract_resource_root(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, (dict, list)):
                return data
        return payload

    def _item_identifier(self, item: Any, index: int) -> str:
        if isinstance(item, dict):
            for key in ("id", "uuid", "name", "slug"):
                value = item.get(key)
                if value is not None:
                    identifier = self._sanitize_path_component(str(value))
                    if identifier:
                        return identifier
        return str(index)

    def _encode_scalar(self, value: Any) -> bytes:
        if isinstance(value, bool):
            return ("true\n" if value else "false\n").encode("utf-8")
        if value is None:
            return b"null\n"
        if isinstance(value, (int, float)):
            return f"{value}\n".encode("utf-8")
        if isinstance(value, str):
            return value.encode("utf-8") + b"\n"
        return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    def _sanitize_path_component(self, value: str) -> str:
        allowed = []
        for char in value.strip():
            if char.isalnum() or char in ("-", "_", "."):
                allowed.append(char)
            else:
                allowed.append("_")
        return "".join(allowed).strip("._")[:200] or "item"

    def _fetch_json_path(self, api_path: str) -> Any:
        url = urllib.parse.urljoin(f"{self.base_url.rstrip('/')}/", api_path.lstrip("/"))
        LOGGER.debug("GET %s", url)
        payload = self._request_bytes(url, accept="application/json, application/*+json")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APISpecError(f"{url} did not return JSON") from exc

    def _resolve_bearer_token(
        self,
        bearer_token: str | None,
        bearer_token_file: str | None,
        bearer_token_env: str | None,
    ) -> str | None:
        if bearer_token:
            return bearer_token.strip()
        if bearer_token_file:
            try:
                with open(bearer_token_file, "r", encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError as exc:
                raise APISpecError(f"unable to read bearer token file {bearer_token_file}: {exc}") from exc
            return value or None
        if bearer_token_env:
            value = os.environ.get(bearer_token_env, "").strip()
            if value:
                return value
        return None

    def _load_spec(self, source: str) -> dict[str, Any]:
        candidates = [source]
        if self._looks_like_url(source) and not source.rstrip("/").endswith(
            (".json", ".yaml", ".yml")
        ):
            base = source.rstrip("/")
            candidates = [
                f"{base}/openapi.json",
                f"{base}/swagger.json",
                f"{base}/openapi.yaml",
                f"{base}/openapi.yml",
                source,
            ]

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                raw = self._read_text(candidate)
                data = self._parse_spec_text(raw)
            except (APISpecError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                continue
            if isinstance(data, dict) and isinstance(data.get("paths"), dict):
                return data
            last_error = APISpecError(f"{candidate} did not look like an OpenAPI document")

        if last_error is None:
            raise APISpecError("unable to load OpenAPI spec")
        raise APISpecError(str(last_error))

    def _parse_spec_text(self, raw: str) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if yaml is None:
                raise APISpecError(
                    "spec is not valid JSON and PyYAML is not installed for YAML support"
                ) from None
            return yaml.safe_load(raw)

    def _read_text(self, source: str) -> str:
        if self._looks_like_url(source):
            try:
                payload = self._request_bytes(
                    source,
                    accept="application/json, application/yaml, text/yaml, text/plain, */*",
                )
                return payload.decode("utf-8")
            except (APISpecError, UnicodeDecodeError) as exc:
                raise APISpecError(f"unable to read spec from {source}: {exc}") from exc
        try:
            with open(source, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise APISpecError(f"unable to read spec file {source}: {exc}") from exc

    def _request_bytes(self, url: str, accept: str) -> bytes:
        headers = {
            "Accept": accept,
            "User-Agent": "apifuse/0.1",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self._ssl_context,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            message = f"HTTP {exc.code}"
            if details:
                message = f"{message}: {details[:200]}"
            raise APISpecError(message, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise APISpecError(str(exc.reason)) from exc

    def _determine_base_url(
        self,
        source: str,
        spec: dict[str, Any],
        server_url: str | None,
    ) -> str:
        if server_url:
            return server_url
        if self._looks_like_url(source):
            parsed = urllib.parse.urlparse(source)
            if parsed.path.endswith((".json", ".yaml", ".yml")):
                servers = spec.get("servers")
                if isinstance(servers, list):
                    for server in servers:
                        if isinstance(server, dict) and isinstance(server.get("url"), str):
                            return server["url"]
                return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
            return source.rstrip("/")
        servers = spec.get("servers")
        if isinstance(servers, list):
            for server in servers:
                if isinstance(server, dict) and isinstance(server.get("url"), str):
                    return server["url"]
        raise APISpecError("spec does not include a usable servers[0].url value")

    def _discover_endpoints(self, spec: dict[str, Any]) -> dict[str, EndpointDefinition]:
        paths = spec.get("paths")
        if not isinstance(paths, dict):
            raise APISpecError("spec is missing a paths object")

        endpoints: dict[str, EndpointDefinition] = {}
        for raw_path, path_item in paths.items():
            if not isinstance(raw_path, str) or not isinstance(path_item, dict):
                continue
            get_op = path_item.get("get")
            if not isinstance(get_op, dict):
                continue

            normalized = self._normalize_path(raw_path)
            if normalized == "/":
                continue

            base_path, item_parameter = self._split_item_path(normalized)
            if base_path is None:
                base_path = normalized
            name = base_path.strip("/")
            if not name or "/" in name:
                continue

            existing = endpoints.get(name)
            if existing is None:
                existing = EndpointDefinition(
                    name=name,
                    base_path=base_path,
                    list_path=None,
                    item_path=None,
                    item_parameter=None,
                    summary=None,
                    description=None,
                    operation_id=None,
                    responses={},
                )

            list_path = existing.list_path
            item_path = existing.item_path
            item_name = existing.item_parameter

            if item_parameter is None:
                list_path = normalized
            else:
                item_path = normalized
                item_name = item_parameter

            endpoints[name] = EndpointDefinition(
                name=name,
                base_path=base_path,
                list_path=list_path,
                item_path=item_path,
                item_parameter=item_name,
                summary=get_op.get("summary") or existing.summary,
                description=get_op.get("description") or existing.description,
                operation_id=get_op.get("operationId") or existing.operation_id,
                responses=get_op.get("responses", existing.responses),
            )

        if not endpoints:
            raise APISpecError("no supported GET endpoints were found in the spec")
        return endpoints

    def _split_item_path(self, normalized_path: str) -> tuple[str | None, str | None]:
        parts = self._split_path(normalized_path)
        if len(parts) != 2:
            return None, None
        tail = parts[-1]
        if not (tail.startswith("{") and tail.endswith("}")):
            return None, None
        parameter = tail[1:-1].strip()
        if not parameter:
            return None, None
        return f"/{parts[0]}", parameter

    def _looks_like_url(self, value: str) -> bool:
        parsed = urllib.parse.urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _sort_key(self, value: str) -> tuple[int, Any]:
        if value.isdigit():
            return (0, int(value))
        return (1, value)

    def _to_fuse_error(self, exc: APISpecError) -> fuse.FuseOSError:
        if exc.status_code == 404:
            return fuse.FuseOSError(errno.ENOENT)
        if exc.status_code == 401:
            return fuse.FuseOSError(errno.EACCES)
        return fuse.FuseOSError(errno.EIO)

    def _unexpected_fuse_error(self, exc: Exception) -> fuse.FuseOSError:
        if isinstance(exc, OSError) and exc.errno:
            return fuse.FuseOSError(exc.errno)
        return fuse.FuseOSError(errno.EIO)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mount a read-only FUSE filesystem backed by an OpenAPI spec."
    )
    parser.add_argument("mountpoint", help="directory where the filesystem will be mounted")
    parser.add_argument(
        "--api-spec",
        required=True,
        help="path or URL for the OpenAPI JSON/YAML spec, or a base API URL",
    )
    parser.add_argument(
        "--server-url",
        help="override the API server base URL used for endpoint requests",
    )
    parser.add_argument(
        "--bearer-token",
        help="bearer token value to send as Authorization: Bearer <token>",
    )
    parser.add_argument(
        "--bearer-token-file",
        help="read the bearer token from a local file",
    )
    parser.add_argument(
        "--bearer-token-env",
        default="APIFUSE_BEARER_TOKEN",
        help="environment variable name to read the bearer token from (default: APIFUSE_BEARER_TOKEN)",
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=10,
        help="when collection GET fails, probe ids 0..N-1 via the item endpoint",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds for spec and endpoint requests",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="run in the foreground instead of daemonizing",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        operations = APIFuse(
            args.api_spec,
            server_url=args.server_url,
            timeout=args.timeout,
            bearer_token=args.bearer_token,
            bearer_token_file=args.bearer_token_file,
            bearer_token_env=args.bearer_token_env,
            probe_limit=args.probe_limit,
        )
    except APISpecError as exc:
        parser.error(str(exc))

    fuse.FUSE(
        operations,
        args.mountpoint,
        foreground=args.foreground,
        ro=True,
        nothreads=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

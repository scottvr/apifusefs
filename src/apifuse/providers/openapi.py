#!/usr/bin/env python3

from __future__ import annotations

import errno
import json
import logging
import os
import re
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import mfusepy as fuse

from apifuse.auth import NoAuth, RefreshingTokenAuth, StaticTokenAuth
from apifuse.fuse_ops import ProviderError, ProviderNode

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
    list_schema: dict[str, Any] | None
    item_schema: dict[str, Any] | None
    item_parameter_schema: dict[str, Any] | None


@dataclass
class JSONCacheEntry:
    expires_at: float
    payload: Any | None = None
    error: APISpecError | None = None


@dataclass(frozen=True)
class SymlinkNode:
    target: str


@dataclass
class AliasCacheEntry:
    expires_at: float
    aliases: dict[str, str]


class APISpecError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenAPIProviderAdapter:
    def __init__(self, impl: "APIFuse") -> None:
        self.impl = impl

    def get_node(self, path: str) -> ProviderNode | None:
        try:
            st = self.impl.getattr(path)
        except fuse.FuseOSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise ProviderError(str(exc), errno_code=exc.errno) from exc

        mode = int(st.get("st_mode", 0))
        if stat.S_ISDIR(mode):
            return ProviderNode(kind="dir")
        if stat.S_ISLNK(mode):
            target = self.impl.readlink(path)
            return ProviderNode(kind="symlink", target=target)
        if stat.S_ISREG(mode):
            size = int(st.get("st_size", 0))
            content = self.impl.read(path, size, 0, 0)
            return ProviderNode(kind="file", content=content)
        return None

    def list_dir(self, path: str) -> list[str]:
        try:
            entries = self.impl.readdir(path, 0)
        except fuse.FuseOSError as exc:
            raise ProviderError(str(exc), errno_code=exc.errno) from exc
        return [entry for entry in entries if entry not in {".", ".."}]

    def statfs(self, path: str) -> dict[str, int]:
        return self.impl.statfs(path)


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

    use_ns = True

    def __init__(
        self,
        api_spec: str,
        server_url: str | None = None,
        timeout: float = 10.0,
        auth_token: str | None = None,
        auth_token_file: str | None = None,
        auth_token_env: str | None = "APIFUSE_auth_token",
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        refresh_url: str | None = None,
        refresh_token: str | None = None,
        refresh_token_file: str | None = None,
        refresh_token_env: str | None = "APIFUSE_refresh_token",
        refresh_body_token_key: str = "refresh_token",
        refresh_response_token_key: str = "access_token",
        probe_limit: int = 10,
        cache_ttl: float = 2.0,
        error_cache_ttl: float = 1.0,
        cache_max_entries: int = 512,
        symlink_names: bool = False,
        symlink_map: list[str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.spec_source = api_spec
        self.server_url_override = server_url.rstrip("/") if server_url else None
        self.auth_token = self._resolve_auth_token(
            auth_token=auth_token,
            auth_token_file=auth_token_file,
            auth_token_env=auth_token_env,
        )
        self.refresh_url = refresh_url.rstrip("/") if refresh_url else None
        self.refresh_token = self._resolve_auth_token(
            auth_token=refresh_token,
            auth_token_file=refresh_token_file,
            auth_token_env=refresh_token_env,
        )
        self.refresh_body_token_key = refresh_body_token_key
        self.refresh_response_token_key = refresh_response_token_key
        self._last_auth_error: str | None = None
        self._auth = self._build_auth_provider(
            header_name=auth_header,
            scheme=auth_scheme,
        )
        self.probe_limit = max(0, probe_limit)
        self.cache_ttl = max(0.0, cache_ttl)
        self.error_cache_ttl = max(0.0, error_cache_ttl)
        self.cache_max_entries = max(1, cache_max_entries)
        self.symlink_names = symlink_names
        self._json_cache: dict[str, JSONCacheEntry] = {}
        self._alias_cache: dict[str, AliasCacheEntry] = {}
        self._ssl_context = ssl.create_default_context()
        self.spec = self._load_spec(api_spec)
        self._components = self._extract_components(self.spec)
        self.base_url = self._determine_base_url(api_spec, self.spec, self.server_url_override)
        self.endpoints = self._discover_endpoints(self.spec)
        self._symlink_field_map = self._build_symlink_field_map(symlink_names, symlink_map or [])
        if self._symlink_field_map:
            LOGGER.debug("symlink aliases enabled: %s", self._symlink_field_map)
        else:
            LOGGER.debug("symlink aliases disabled")
        self._dir_mode = stat.S_IFDIR | 0o755
        self._file_mode = stat.S_IFREG | 0o444
        self._symlink_mode = stat.S_IFLNK | 0o777

    def bootstrap_validate(self, force: bool = False, sample_limit: int = 20) -> None:
        candidates = self._bootstrap_probe_paths(sample_limit=sample_limit)
        if not candidates:
            message = "bootstrap probe found no GET endpoints to test"
            if force:
                LOGGER.warning("%s; continuing because --force is enabled", message)
                return
            raise APISpecError(message)

        failures: list[str] = []
        successes = 0
        for api_path in candidates:
            try:
                self._fetch_json_path(api_path)
                successes += 1
                # One confirmed reachable endpoint is enough for default mount policy.
                break
            except APISpecError as exc:
                failures.append(f"{api_path}: {exc}")

        if successes > 0:
            if failures:
                LOGGER.warning(
                    "bootstrap validation found partial reachability (%d failed probes before first success): %s",
                    len(failures),
                    "; ".join(failures[:3]),
                )
            return

        message = "bootstrap validation failed; no reachable endpoints found"
        if failures:
            message = f"{message}. sample failures: {'; '.join(failures[:3])}"
        if force:
            LOGGER.warning("%s; continuing because --force is enabled", message)
            return
        raise APISpecError(message)

    def _bootstrap_probe_paths(self, sample_limit: int = 20) -> list[str]:
        paths: list[str] = []
        for endpoint in self.endpoints.values():
            if endpoint.list_path:
                paths.append(endpoint.list_path)
            if endpoint.item_path and endpoint.item_parameter:
                paths.append(endpoint.item_path.replace(f"{{{endpoint.item_parameter}}}", "0"))
            if len(paths) >= sample_limit:
                break
        # Deduplicate while preserving order.
        return list(dict.fromkeys(paths))[:sample_limit]

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
            symlink = self._get_symlink_node(normalized)
            if symlink is not None:
                return self._stat_for_symlink(now, len(symlink.target.encode("utf-8")))
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

    def readlink(self, path: str) -> str:
        try:
            symlink = self._get_symlink_node(self._normalize_path(path))
            if symlink is None:
                raise fuse.FuseOSError(errno.ENOENT)
            return symlink.target
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            LOGGER.exception("readlink failed for %s", path)
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

    def _stat_for_symlink(self, now: float, size: int) -> dict[str, Any]:
        return {
            "st_mode": self._symlink_mode,
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
        if self._should_ignore_collection_child(parts[1]):
            return "ignored", endpoint, parts[1:]
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

    def _get_symlink_node(self, path: str) -> SymlinkNode | None:
        path_type, endpoint, remainder = self._classify_path(path)
        if path_type != "resource_dir" or endpoint is None or len(remainder) != 1:
            return None
        alias_target = self._collection_alias_map(endpoint).get(remainder[0])
        if alias_target is None:
            return None
        return SymlinkNode(target=alias_target)

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
                "list_schema": endpoint.list_schema,
                "item_schema": endpoint.item_schema,
                "item_parameter_schema": endpoint.item_parameter_schema,
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        return FileNode(content=body)

    def _collection_error_file(self, endpoint: EndpointDefinition) -> FileNode | None:
        error = self._collection_error(endpoint)
        if error is None:
            return None
        if self._last_auth_error:
            error = f"{error}\nlast_auth_error: {self._last_auth_error}"
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

        unique_ids = sorted(set(ids), key=self._sort_key)
        entries.extend(unique_ids)
        entries.extend(self._collection_alias_entries(endpoint, unique_ids))
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

        if len(remainder) == 2 and remainder[1] == ".raw.json":
            response = self._fetch_resource_response(endpoint, remainder[0])
            body = json.dumps(response, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            return "file", body
        if any(self._should_ignore_nested_child(part) for part in remainder[1:]):
            return None, None
        if not self._is_schema_path_allowed(endpoint, remainder):
            return None, None

        resource_id = remainder[0]
        response = self._fetch_resource_response(endpoint, resource_id)
        resource_root = self._extract_resource_root(response)

        if len(remainder) == 1:
            return "dir", None

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
        if any(self._should_ignore_nested_child(part) for part in remainder[1:]):
            raise fuse.FuseOSError(errno.ENOENT)
        if not self._is_schema_path_allowed(endpoint, remainder):
            raise fuse.FuseOSError(errno.ENOENT)

        resource_id = remainder[0]
        response = self._fetch_resource_response(endpoint, resource_id)
        resource_root = self._extract_resource_root(response)

        if len(remainder) == 1:
            entries = [".raw.json"]
            entries.extend(self._merge_child_names(resource_root, endpoint.item_schema))
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

        schema = self._resolve_schema_node_for_path(endpoint.item_schema, remainder[1:])

        if not isinstance(node, (dict, list)):
            raise fuse.FuseOSError(errno.ENOTDIR)
        return self._merge_child_names(node, schema)

    def _list_child_names(self, node: Any) -> list[str]:
        if isinstance(node, dict):
            return sorted(node.keys(), key=self._sort_key)
        if isinstance(node, list):
            return [str(index) for index in range(len(node))]
        return []

    def _merge_child_names(self, node: Any, schema: dict[str, Any] | None) -> list[str]:
        names = set(self._list_child_names(node))
        names.update(self._schema_child_names(schema))
        return sorted(names, key=self._sort_key)

    def _schema_child_names(self, schema: dict[str, Any] | None) -> list[str]:
        resolved = self._resolve_schema(schema)
        if not isinstance(resolved, dict):
            return []
        schema_type = resolved.get("type")
        if schema_type == "object":
            properties = resolved.get("properties")
            if isinstance(properties, dict):
                return [key for key in properties if isinstance(key, str)]
        if schema_type == "array":
            return []
        return []

    def _is_schema_path_allowed(
        self,
        endpoint: EndpointDefinition,
        remainder: list[str],
    ) -> bool:
        if not remainder:
            return True
        if not self._is_valid_resource_id(endpoint, remainder[0]):
            return False
        if len(remainder) == 1:
            return True
        if len(remainder) == 2 and remainder[1] == ".raw.json":
            return True
        if endpoint.item_schema is None:
            return True
        return self._resolve_schema_node_for_path(endpoint.item_schema, remainder[1:]) is not None

    def _is_valid_resource_id(self, endpoint: EndpointDefinition, resource_id: str) -> bool:
        schema = self._resolve_schema(endpoint.item_parameter_schema)
        if schema is None:
            return True

        enum_values = schema.get("enum")
        if isinstance(enum_values, list):
            normalized_enum = {str(value) for value in enum_values}
            if resource_id not in normalized_enum:
                return False

        schema_type = schema.get("type")
        if schema_type == "integer":
            return self._is_integer_string(resource_id)
        if schema_type == "number":
            return self._is_number_string(resource_id)
        if schema_type == "boolean":
            return resource_id in {"true", "false", "0", "1"}
        if schema_type == "string" or schema_type is None:
            min_length = schema.get("minLength")
            if isinstance(min_length, int) and len(resource_id) < min_length:
                return False
            max_length = schema.get("maxLength")
            if isinstance(max_length, int) and len(resource_id) > max_length:
                return False
            pattern = schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    if re.fullmatch(pattern, resource_id) is None:
                        return False
                except re.error:
                    LOGGER.debug("ignoring invalid regex pattern in OpenAPI schema: %s", pattern)
            return True

        return True

    def _resolve_schema_node_for_path(
        self,
        schema: dict[str, Any] | None,
        parts: list[str],
    ) -> dict[str, Any] | None:
        current = self._resolve_schema(schema)
        if current is None:
            return None
        if not parts:
            return current

        for part in parts:
            current = self._resolve_schema(current)
            if current is None:
                return None

            schema_type = current.get("type")
            if schema_type == "object":
                properties = current.get("properties")
                if not isinstance(properties, dict):
                    additional = current.get("additionalProperties")
                    if additional is True:
                        current = None
                        continue
                    if isinstance(additional, dict):
                        current = additional
                        continue
                    return None
                child = properties.get(part)
                if not isinstance(child, dict):
                    additional = current.get("additionalProperties")
                    if additional is True:
                        current = None
                        continue
                    if isinstance(additional, dict):
                        current = additional
                        continue
                    return None
                current = child
                continue

            if schema_type == "array":
                if not part.isdigit():
                    return None
                items = current.get("items")
                if not isinstance(items, dict):
                    return None
                current = items
                continue

            return None

        return self._resolve_schema(current)

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

    def _collection_alias_entries(
        self,
        endpoint: EndpointDefinition,
        ids: list[str],
    ) -> list[str]:
        if not ids:
            return []
        return sorted(self._collection_alias_map(endpoint, ids).keys(), key=self._sort_key)

    def _collection_alias_map(
        self,
        endpoint: EndpointDefinition,
        resource_ids: list[str] | None = None,
    ) -> dict[str, str]:
        field_paths = self._symlink_field_map.get(endpoint.name, [])
        if not field_paths:
            return {}

        cached_aliases = self._get_cached_aliases(endpoint.name)
        if cached_aliases is not None:
            return cached_aliases

        if resource_ids is None:
            try:
                resource_ids = self._fetch_collection_ids(endpoint)
            except APISpecError as exc:
                LOGGER.debug(
                    "alias build for /%s could not use collection listing: %s; falling back to probing",
                    endpoint.name,
                    exc,
                )
                resource_ids = self._probe_resource_ids(endpoint)

        alias_map: dict[str, str] = {}
        reserved = set(resource_ids)
        reserved.update({".meta.json", ".error.txt"})

        for resource_id in sorted(set(resource_ids), key=self._sort_key):
            try:
                response = self._fetch_resource_response(endpoint, resource_id)
            except APISpecError as exc:
                LOGGER.debug(
                    "skipping alias generation for /%s/%s: %s",
                    endpoint.name,
                    resource_id,
                    exc,
                )
                continue
            resource_root = self._extract_resource_root(response)
            for field_path in field_paths:
                alias = self._alias_from_field_path(resource_root, field_path)
                if alias is None or alias in reserved or alias in alias_map:
                    continue
                alias_map[alias] = resource_id

        self._cache_aliases(endpoint.name, alias_map)
        LOGGER.debug(
            "built %d symlink aliases for /%s from fields %s",
            len(alias_map),
            endpoint.name,
            ["/".join(path) for path in field_paths],
        )
        return alias_map

    def _alias_from_field_path(self, node: Any, field_path: tuple[str, ...]) -> str | None:
        value = self._extract_value_at_parts(node, list(field_path))
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return None
        alias = self._sanitize_path_component(str(value))
        return alias or None

    def _extract_value_at_parts(self, node: Any, parts: list[str]) -> Any | None:
        current = node
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current[part]
                continue
            if isinstance(current, list):
                if part.isdigit():
                    index = int(part)
                    if index < 0 or index >= len(current):
                        return None
                    current = current[index]
                    continue
                if len(current) == 1:
                    current = current[0]
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                        continue
                return None
            return None
        return current

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
        cache_key = self._normalize_api_cache_key(api_path)
        cached = self._get_cached_json(cache_key)
        if cached is not None:
            return cached

        url = urllib.parse.urljoin(f"{self.base_url.rstrip('/')}/", api_path.lstrip("/"))
        LOGGER.debug("GET %s", url)
        try:
            payload = self._request_bytes(url, accept="application/json, application/*+json")
            data = json.loads(payload.decode("utf-8"))
        except APISpecError as exc:
            self._cache_json_error(cache_key, exc)
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = APISpecError(f"{url} did not return JSON")
            self._cache_json_error(cache_key, error)
            raise error from exc

        self._cache_json_success(cache_key, data)
        return data

    def _resolve_auth_token(
        self,
        auth_token: str | None,
        auth_token_file: str | None,
        auth_token_env: str | None,
    ) -> str | None:
        if auth_token:
            return auth_token.strip()
        if auth_token_file:
            try:
                with open(auth_token_file, "r", encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError as exc:
                raise APISpecError(f"unable to read auth token file {auth_token_file}: {exc}") from exc
            return value or None
        if auth_token_env:
            value = os.environ.get(auth_token_env, "").strip()
            if value:
                return value
        return None

    def _build_auth_provider(self, header_name: str, scheme: str):
        if self.refresh_url and self.refresh_token is not None:
            return RefreshingTokenAuth(
                token=self.auth_token or "",
                header_name=header_name,
                scheme=scheme,
                refresh_callback=self._refresh_access_token,
            )
        if self.auth_token:
            return StaticTokenAuth(
                token=self.auth_token,
                header_name=header_name,
                scheme=scheme,
            )
        return NoAuth()

    def _build_symlink_field_map(
        self,
        symlink_names: bool,
        symlink_map_args: list[str],
    ) -> dict[str, list[tuple[str, ...]]]:
        alias_map: dict[str, list[tuple[str, ...]]] = {}
        if symlink_names:
            default_paths = (
                ("name",),
                ("username",),
                ("slug",),
                ("title",),
            )
            for endpoint_name in self.endpoints:
                alias_map.setdefault(endpoint_name, []).extend(default_paths)

        for raw_arg in symlink_map_args:
            for entry in raw_arg.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                parsed = self._parse_symlink_map_entry(entry)
                if parsed is None:
                    LOGGER.warning("ignoring invalid --symlink-map entry: %s", entry)
                    continue
                endpoint_name, field_path = parsed
                if endpoint_name not in self.endpoints:
                    LOGGER.warning(
                        "ignoring --symlink-map entry for unknown collection %s",
                        endpoint_name,
                    )
                    continue
                bucket = alias_map.setdefault(endpoint_name, [])
                if field_path not in bucket:
                    bucket.append(field_path)

        return alias_map

    def _parse_symlink_map_entry(self, entry: str) -> tuple[str, tuple[str, ...]] | None:
        if "=" not in entry:
            return None
        endpoint_name, mapping = entry.split("=", 1)
        endpoint_name = endpoint_name.strip().strip("/")
        mapping = mapping.strip()
        if not endpoint_name or not mapping:
            return None
        if ":" in mapping:
            return None

        parts = tuple(part for part in mapping.strip("/").split("/") if part)
        if not parts:
            return None
        return endpoint_name, parts

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

    def _extract_components(self, spec: dict[str, Any]) -> dict[str, Any]:
        components = spec.get("components")
        if isinstance(components, dict):
            return components
        return {}

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
        self._auth.apply(headers)

        def _execute(request_headers: dict[str, str]) -> bytes:
            request = urllib.request.Request(url, headers=request_headers, method="GET")
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self._ssl_context,
            ) as response:
                return response.read()

        try:
            payload = _execute(headers)
            self._last_auth_error = None
            return payload
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code == 401 and self._auth.on_unauthorized():
                refreshed_headers = {
                    "Accept": accept,
                    "User-Agent": "apifuse/0.1",
                }
                self._auth.apply(refreshed_headers)
                try:
                    payload = _execute(refreshed_headers)
                    self._last_auth_error = None
                    return payload
                except urllib.error.HTTPError as retry_exc:
                    retry_details = retry_exc.read().decode("utf-8", errors="replace").strip()
                    retry_message = f"HTTP {retry_exc.code}"
                    if retry_details:
                        retry_message = f"{retry_message}: {retry_details[:200]}"
                    self._last_auth_error = f"auth retry failed: {retry_message}"
                    raise APISpecError(retry_message, status_code=retry_exc.code) from retry_exc
                except urllib.error.URLError as retry_exc:
                    self._last_auth_error = f"auth retry failed: {retry_exc.reason}"
                    raise APISpecError(str(retry_exc.reason)) from retry_exc
            message = f"HTTP {exc.code}"
            if details:
                message = f"{message}: {details[:200]}"
            if exc.code in {401, 403}:
                self._last_auth_error = message
            raise APISpecError(message, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise APISpecError(str(exc.reason)) from exc

    def _refresh_access_token(self) -> str | None:
        if not self.refresh_url or not self.refresh_token:
            return None

        refresh_payload = {self.refresh_body_token_key: self.refresh_token}
        body = json.dumps(refresh_payload).encode("utf-8")
        headers = {
            "Accept": "application/json, application/*+json",
            "Content-Type": "application/json",
            "User-Agent": "apifuse/0.1",
        }
        request = urllib.request.Request(
            self.refresh_url,
            headers=headers,
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self._ssl_context,
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            self._last_auth_error = f"token refresh failed: HTTP {exc.code}: {details[:200]}"
            LOGGER.warning(self._last_auth_error)
            return None
        except urllib.error.URLError as exc:
            self._last_auth_error = f"token refresh failed: {exc.reason}"
            LOGGER.warning(self._last_auth_error)
            return None

        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._last_auth_error = "token refresh failed: response was not JSON"
            LOGGER.warning(self._last_auth_error)
            return None

        if not isinstance(data, dict):
            self._last_auth_error = "token refresh failed: response JSON was not an object"
            LOGGER.warning(self._last_auth_error)
            return None

        token = data.get(self.refresh_response_token_key)
        if not isinstance(token, str) or not token.strip():
            self._last_auth_error = (
                f"token refresh failed: missing non-empty '{self.refresh_response_token_key}' in response"
            )
            LOGGER.warning(self._last_auth_error)
            return None

        self.auth_token = token.strip()
        self._last_auth_error = None
        LOGGER.info("refreshed access token successfully")
        return self.auth_token

    def _extract_get_response_schema(self, get_op: dict[str, Any]) -> dict[str, Any] | None:
        responses = get_op.get("responses")
        if not isinstance(responses, dict):
            return None

        preferred_codes = ("200", "201", "202", "203", "204", "default")
        response_obj: dict[str, Any] | None = None
        for code in preferred_codes:
            candidate = responses.get(code)
            if isinstance(candidate, dict):
                response_obj = candidate
                break
        if response_obj is None:
            for candidate in responses.values():
                if isinstance(candidate, dict):
                    response_obj = candidate
                    break
        if response_obj is None:
            return None

        content = response_obj.get("content")
        if not isinstance(content, dict):
            return None
        for media_type, media_obj in content.items():
            if not isinstance(media_type, str) or not isinstance(media_obj, dict):
                continue
            if "json" not in media_type:
                continue
            schema = media_obj.get("schema")
            if isinstance(schema, dict):
                return schema
        for media_obj in content.values():
            if not isinstance(media_obj, dict):
                continue
            schema = media_obj.get("schema")
            if isinstance(schema, dict):
                return schema
        return None

    def _extract_parameter_schema(
        self,
        path_item: dict[str, Any],
        get_op: dict[str, Any],
        parameter_name: str,
    ) -> dict[str, Any] | None:
        merged_parameters: list[Any] = []
        for source in (path_item.get("parameters"), get_op.get("parameters")):
            if isinstance(source, list):
                merged_parameters.extend(source)

        for parameter in merged_parameters:
            resolved = self._resolve_schema(parameter)
            if not isinstance(resolved, dict):
                continue
            if resolved.get("in") != "path":
                continue
            if resolved.get("name") != parameter_name:
                continue
            schema = resolved.get("schema")
            if isinstance(schema, dict):
                return schema
        return None

    def _extract_resource_schema(self, response_schema: dict[str, Any] | None) -> dict[str, Any] | None:
        resolved = self._resolve_schema(response_schema)
        if not isinstance(resolved, dict):
            return None
        if resolved.get("type") == "array":
            items = resolved.get("items")
            if isinstance(items, dict):
                return items
        if resolved.get("type") == "object":
            properties = resolved.get("properties")
            if isinstance(properties, dict):
                data_schema = properties.get("data")
                if isinstance(data_schema, dict):
                    data_resolved = self._resolve_schema(data_schema)
                    if isinstance(data_resolved, dict):
                        return data_resolved
        return resolved

    def _resolve_schema(
        self,
        schema: dict[str, Any] | None,
        seen_refs: set[str] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(schema, dict):
            return None

        current = schema
        refs_seen = set() if seen_refs is None else set(seen_refs)
        while isinstance(current, dict) and "$ref" in current:
            ref = current.get("$ref")
            if not isinstance(ref, str):
                return current
            if ref in refs_seen:
                return current
            refs_seen.add(ref)
            resolved = self._resolve_ref(ref)
            if not isinstance(resolved, dict):
                return current
            merged = dict(resolved)
            for key, value in current.items():
                if key != "$ref":
                    merged[key] = value
            current = merged

        if isinstance(current, dict) and "allOf" in current:
            all_of = current.get("allOf")
            if isinstance(all_of, list):
                merged: dict[str, Any] = {}
                properties: dict[str, Any] = {}
                required: list[str] = []
                for part in all_of:
                    part_schema = self._resolve_schema(part, refs_seen)
                    if not isinstance(part_schema, dict):
                        continue
                    for key, value in part_schema.items():
                        if key == "properties" and isinstance(value, dict):
                            properties.update(value)
                            continue
                        if key == "required" and isinstance(value, list):
                            required.extend(v for v in value if isinstance(v, str))
                            continue
                        merged[key] = value
                if properties:
                    merged["properties"] = properties
                    merged.setdefault("type", "object")
                if required:
                    merged["required"] = list(dict.fromkeys(required))
                for key, value in current.items():
                    if key != "allOf":
                        merged[key] = value
                current = merged

        return current

    def _resolve_ref(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return None
        node: Any = self.spec
        for part in ref[2:].split("/"):
            token = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict):
                return None
            node = node.get(token)
        return node

    def _is_integer_string(self, value: str) -> bool:
        if not value:
            return False
        if value[0] in "+-":
            return value[1:].isdigit()
        return value.isdigit()

    def _is_number_string(self, value: str) -> bool:
        try:
            float(value)
        except ValueError:
            return False
        return True

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
                    list_schema=None,
                    item_schema=None,
                    item_parameter_schema=None,
                )

            list_path = existing.list_path
            item_path = existing.item_path
            item_name = existing.item_parameter

            if item_parameter is None:
                list_path = normalized
            else:
                item_path = normalized
                item_name = item_parameter

            list_schema = existing.list_schema
            item_schema = existing.item_schema
            item_parameter_schema = existing.item_parameter_schema
            response_schema = self._extract_get_response_schema(get_op)
            if item_parameter is None:
                list_schema = response_schema
            else:
                item_schema = self._extract_resource_schema(response_schema)
                item_parameter_schema = self._extract_parameter_schema(
                    path_item,
                    get_op,
                    item_parameter,
                )

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
                list_schema=list_schema,
                item_schema=item_schema,
                item_parameter_schema=item_parameter_schema,
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

    def _should_ignore_collection_child(self, name: str) -> bool:
        return name.startswith(".") and name not in {".meta.json", ".error.txt"}

    def _should_ignore_nested_child(self, name: str) -> bool:
        return name.startswith(".") and name != ".raw.json"

    def _normalize_api_cache_key(self, api_path: str) -> str:
        normalized = self._normalize_path(api_path)
        if api_path.endswith("/") and normalized != "/":
            return f"{normalized}/"
        return normalized

    def _get_cached_json(self, cache_key: str) -> Any | None:
        entry = self._json_cache.get(cache_key)
        if entry is None:
            return None
        now = time.time()
        if entry.expires_at <= now:
            self._json_cache.pop(cache_key, None)
            return None
        self._json_cache.pop(cache_key, None)
        self._json_cache[cache_key] = entry
        if entry.error is not None:
            raise APISpecError(str(entry.error), status_code=entry.error.status_code)
        return entry.payload

    def _cache_json_success(self, cache_key: str, payload: Any) -> None:
        if self.cache_ttl <= 0:
            self._json_cache.pop(cache_key, None)
            return
        self._json_cache.pop(cache_key, None)
        self._json_cache[cache_key] = JSONCacheEntry(
            expires_at=time.time() + self.cache_ttl,
            payload=payload,
        )
        self._trim_cache()

    def _cache_json_error(self, cache_key: str, error: APISpecError) -> None:
        if self.error_cache_ttl <= 0:
            self._json_cache.pop(cache_key, None)
            return
        self._json_cache.pop(cache_key, None)
        self._json_cache[cache_key] = JSONCacheEntry(
            expires_at=time.time() + self.error_cache_ttl,
            error=APISpecError(str(error), status_code=error.status_code),
        )
        self._trim_cache()

    def _trim_cache(self) -> None:
        while len(self._json_cache) > self.cache_max_entries:
            oldest_key = next(iter(self._json_cache))
            self._json_cache.pop(oldest_key, None)

    def _get_cached_aliases(self, endpoint_name: str) -> dict[str, str] | None:
        entry = self._alias_cache.get(endpoint_name)
        if entry is None:
            return None
        now = time.time()
        if entry.expires_at <= now:
            self._alias_cache.pop(endpoint_name, None)
            return None
        self._alias_cache.pop(endpoint_name, None)
        self._alias_cache[endpoint_name] = entry
        return dict(entry.aliases)

    def _cache_aliases(self, endpoint_name: str, aliases: dict[str, str]) -> None:
        if self.cache_ttl <= 0:
            self._alias_cache.pop(endpoint_name, None)
            return
        self._alias_cache.pop(endpoint_name, None)
        self._alias_cache[endpoint_name] = AliasCacheEntry(
            expires_at=time.time() + self.cache_ttl,
            aliases=dict(aliases),
        )
        while len(self._alias_cache) > self.cache_max_entries:
            oldest_key = next(iter(self._alias_cache))
            self._alias_cache.pop(oldest_key, None)

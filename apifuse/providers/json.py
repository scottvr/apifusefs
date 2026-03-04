from __future__ import annotations

import errno
import json
import logging
import os
from typing import Any

from apifuse.fuse_ops import ProviderError, ProviderFuse, ProviderNode


LOGGER = logging.getLogger(__name__)


class JSONProvider:
    def __init__(
        self,
        data: Any,
        symlink_names: bool = False,
        symlink_map: list[str] | None = None,
    ) -> None:
        self.data = data
        self._symlink_paths = self._build_root_symlink_paths(
            symlink_names=symlink_names,
            symlink_map=symlink_map or [],
        )
        self._aliases = self._build_root_alias_map()
        if self._aliases:
            LOGGER.debug("json mode symlink aliases enabled: %s", self._aliases)
        else:
            LOGGER.debug("json mode symlink aliases disabled")

    def get_node(self, path: str) -> ProviderNode | None:
        normalized = self._normalize_path(path)
        if normalized == "/":
            return ProviderNode(kind="dir")
        symlink_target = self._symlink_target(normalized)
        if symlink_target is not None:
            return ProviderNode(kind="symlink", target=symlink_target)
        node = self._resolve_node(normalized)
        if node is None:
            return None
        if isinstance(node, (dict, list)):
            return ProviderNode(kind="dir")
        return ProviderNode(kind="file", content=self._encode_scalar(node))

    def list_dir(self, path: str) -> list[str]:
        normalized = self._normalize_path(path)
        node = self._resolve_node(normalized)
        if node is None:
            raise ProviderError("path not found", errno_code=errno.ENOENT)
        entries: list[str] = []
        if normalized == "/":
            entries.extend(sorted(self._aliases))
        if isinstance(node, dict):
            entries.extend(sorted(node.keys()))
            return entries
        if isinstance(node, list):
            entries.extend(str(index) for index in range(len(node)))
            return entries
        raise ProviderError("not a directory", errno_code=errno.ENOTDIR)

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

    def _normalize_path(self, path: str) -> str:
        normalized = os.path.normpath(path)
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    def _resolve_node(self, path: str) -> Any | None:
        if path != "/" and path.startswith("/"):
            parts = [part for part in path.strip("/").split("/") if part]
            if len(parts) == 1:
                alias_target = self._aliases.get(parts[0])
                if alias_target is not None:
                    path = f"/{alias_target}"
        if path == "/":
            return self.data
        parts = [part for part in path.strip("/").split("/") if part]
        node: Any = self.data
        for part in parts:
            if isinstance(node, dict):
                if part not in node:
                    return None
                node = node[part]
                continue
            if isinstance(node, list):
                if not part.isdigit():
                    return None
                index = int(part)
                if index < 0 or index >= len(node):
                    return None
                node = node[index]
                continue
            return None
        return node

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

    def _build_root_symlink_paths(
        self,
        symlink_names: bool,
        symlink_map: list[str],
    ) -> list[tuple[str, ...]]:
        paths: list[tuple[str, ...]] = []
        if symlink_names:
            paths.extend([("name",), ("username",), ("slug",), ("title",)])
        for raw_arg in symlink_map:
            for entry in raw_arg.split(","):
                parsed = self._parse_symlink_map_entry(entry.strip())
                if parsed is None:
                    continue
                if parsed not in paths:
                    paths.append(parsed)
        return paths

    def _parse_symlink_map_entry(self, entry: str) -> tuple[str, ...] | None:
        if not entry:
            return None
        mapping = entry
        if "=" in entry:
            collection, right = entry.split("=", 1)
            collection = collection.strip().strip("/")
            if collection and collection not in {"root", "*"}:
                return None
            mapping = right.strip()
        if not mapping or ":" in mapping:
            return None
        parts = tuple(part for part in mapping.strip("/").split("/") if part)
        if not parts:
            return None
        return parts

    def _build_root_alias_map(self) -> dict[str, str]:
        if not isinstance(self.data, list) or not self._symlink_paths:
            return {}
        aliases: dict[str, str] = {}
        reserved = {str(index) for index in range(len(self.data))}
        for index, item in enumerate(self.data):
            for field_path in self._symlink_paths:
                value = self._extract_value(item, list(field_path))
                if value is None or isinstance(value, (dict, list)):
                    continue
                alias = self._sanitize_path_component(str(value))
                if not alias or alias in reserved or alias in aliases:
                    continue
                aliases[alias] = str(index)
        return aliases

    def _extract_value(self, node: Any, parts: list[str]) -> Any | None:
        current = node
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current[part]
                continue
            if isinstance(current, list):
                if part.isdigit():
                    idx = int(part)
                    if idx < 0 or idx >= len(current):
                        return None
                    current = current[idx]
                    continue
                if len(current) == 1 and isinstance(current[0], dict) and part in current[0]:
                    current = current[0][part]
                    continue
                return None
            return None
        return current

    def _sanitize_path_component(self, value: str) -> str:
        allowed = []
        for char in value.strip():
            if char.isalnum() or char in ("-", "_", "."):
                allowed.append(char)
            else:
                allowed.append("_")
        return "".join(allowed).strip("._")[:200]

    def _symlink_target(self, path: str) -> str | None:
        if path == "/":
            return None
        parts = [part for part in path.strip("/").split("/") if part]
        if len(parts) != 1:
            return None
        return self._aliases.get(parts[0])


class JSONFuse(ProviderFuse):
    def __init__(
        self,
        data: Any,
        symlink_names: bool = False,
        symlink_map: list[str] | None = None,
    ) -> None:
        super().__init__(
            JSONProvider(
                data,
                symlink_names=symlink_names,
                symlink_map=symlink_map,
            )
        )

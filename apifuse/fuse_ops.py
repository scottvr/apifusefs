from __future__ import annotations

import errno
import logging
import os
import stat
import time
from dataclasses import dataclass
from typing import Protocol

import mfusepy as fuse


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderNode:
    kind: str
    content: bytes | None = None
    target: str | None = None


class ProviderError(RuntimeError):
    def __init__(self, message: str, errno_code: int = errno.EIO) -> None:
        super().__init__(message)
        self.errno_code = errno_code


class FilesystemProvider(Protocol):
    def get_node(self, path: str) -> ProviderNode | None:
        ...

    def list_dir(self, path: str) -> list[str]:
        ...

    def statfs(self, path: str) -> dict[str, int]:
        ...


class ProviderFuse(fuse.Operations):
    use_ns = True

    def __init__(self, provider: FilesystemProvider) -> None:
        self.provider = provider
        self._dir_mode = stat.S_IFDIR | 0o755
        self._file_mode = stat.S_IFREG | 0o444
        self._symlink_mode = stat.S_IFLNK | 0o777

    def access(self, path: str, mode: int) -> int:
        try:
            if mode & os.W_OK:
                raise fuse.FuseOSError(errno.EROFS)
            self.getattr(path)
            return 0
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def getattr(self, path: str, fh: int | None = None) -> dict[str, float | int]:
        try:
            node = self.provider.get_node(path)
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            now = time.time()
            if node.kind == "dir":
                return {
                    "st_mode": self._dir_mode,
                    "st_nlink": 2,
                    "st_size": 0,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }
            if node.kind == "symlink":
                size = len((node.target or "").encode("utf-8"))
                return {
                    "st_mode": self._symlink_mode,
                    "st_nlink": 1,
                    "st_size": size,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }
            content = node.content or b""
            return {
                "st_mode": self._file_mode,
                "st_nlink": 1,
                "st_size": len(content),
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
            }
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def readdir(self, path: str, fh: int) -> list[str]:
        try:
            entries = self.provider.list_dir(path)
            return [".", "..", *entries]
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def open(self, path: str, flags: int) -> int:
        try:
            if flags & os.O_WRONLY or flags & os.O_RDWR:
                raise fuse.FuseOSError(errno.EROFS)
            node = self.provider.get_node(path)
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            if node.kind == "dir":
                raise fuse.FuseOSError(errno.EISDIR)
            if node.kind == "symlink":
                raise fuse.FuseOSError(errno.ELOOP)
            return 0
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        try:
            node = self.provider.get_node(path)
            if node is None:
                raise fuse.FuseOSError(errno.ENOENT)
            if node.kind == "dir":
                raise fuse.FuseOSError(errno.EISDIR)
            if node.kind == "symlink":
                raise fuse.FuseOSError(errno.ELOOP)
            content = node.content or b""
            return content[offset : offset + size]
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def readlink(self, path: str) -> str:
        try:
            node = self.provider.get_node(path)
            if node is None or node.kind != "symlink":
                raise fuse.FuseOSError(errno.ENOENT)
            return node.target or ""
        except fuse.FuseOSError:
            raise
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def statfs(self, path: str) -> dict[str, int]:
        try:
            return self.provider.statfs(path)
        except Exception as exc:
            raise self._to_fuse_error(exc) from exc

    def _to_fuse_error(self, exc: Exception) -> fuse.FuseOSError:
        if isinstance(exc, ProviderError):
            return fuse.FuseOSError(exc.errno_code)
        if isinstance(exc, OSError) and exc.errno:
            return fuse.FuseOSError(exc.errno)
        LOGGER.exception("provider operation failed")
        return fuse.FuseOSError(errno.EIO)


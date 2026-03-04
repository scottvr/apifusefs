from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class AuthProvider(Protocol):
    def apply(self, headers: dict[str, str]) -> None:
        ...

    def on_unauthorized(self) -> bool:
        ...


class NoAuth:
    def apply(self, headers: dict[str, str]) -> None:
        return

    def on_unauthorized(self) -> bool:
        return False


@dataclass
class StaticTokenAuth:
    token: str
    header_name: str = "Authorization"
    scheme: str = "Bearer"

    def apply(self, headers: dict[str, str]) -> None:
        if self.token:
            headers[self.header_name] = f"{self.scheme} {self.token}"

    def on_unauthorized(self) -> bool:
        return False


@dataclass
class RefreshingTokenAuth(StaticTokenAuth):
    refresh_callback: Callable[[], str | None] | None = None

    def on_unauthorized(self) -> bool:
        if self.refresh_callback is None:
            return False
        new_token = self.refresh_callback()
        if not new_token:
            return False
        self.token = new_token
        return True

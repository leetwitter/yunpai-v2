"""Shared fake httpx plumbing for M1 HTTP adapter tests.

Mirrors the established test idiom (tests/test_http_adapter.py and
tests/test_m3_m4_tool_integration.py): replace ``httpx.AsyncClient`` with a
context manager whose ``request`` delegates to a backend object.
"""

from __future__ import annotations

from typing import Any

import httpx

BASE = "http://m1.test"


class _Entry:
    def __init__(self, value: Any, status_code: int, repeat: bool):
        self.value = value
        self.status_code = status_code
        self.repeat = repeat


class Backend:
    """Route by (method, path) to canned responses and record every call."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, str], list[_Entry]] = {}

    def route(self, method: str, path: str, value: Any, status_code: int = 200, *, repeat: bool = False):
        """Register a response.  ``repeat`` keeps the last entry reusable; without
        it entries are consumed in order (one call per ``route`` registration)."""
        self._routes.setdefault((method.upper(), path), []).append(_Entry(value, status_code, repeat))

    async def request(self, method: str, url: str, **kwargs):
        path = httpx.URL(url).path
        self.calls.append({"method": method.upper(), "url": url, "path": path, **kwargs})
        entries = self._routes.get((method.upper(), path))
        if not entries:
            raise AssertionError(f"unexpected request: {method} {url}")
        entry = entries[0]
        if not entry.repeat:
            entries.pop(0)
        return httpx.Response(
            entry.status_code,
            request=httpx.Request(method, url),
            json=entry.value if entry.value is not None else {},
        )


class FakeClient:
    def __init__(self, backend: Backend, **kwargs):
        self.backend = backend

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method: str, url: str, **kwargs):
        return await self.backend.request(method, url, **kwargs)

    async def get(self, url: str, headers=None, **kwargs):
        return await self.backend.request("GET", url, headers=headers or {}, **kwargs)


def status_error(method: str, url: str, status_code: int, detail: Any) -> httpx.HTTPStatusError:
    request = httpx.Request(method, url)
    response = httpx.Response(status_code, request=request, json={"detail": detail})
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def install(monkeypatch, backend: Backend):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient(backend, **kwargs))
    return backend

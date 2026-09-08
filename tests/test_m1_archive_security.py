"""M1 archive/upload transport acceptance (adapter side).

Server-side archive hardening (path traversal, zip bombs, recursion, symlinks,
per-entry budgets) is implemented by the standalone M1 service and covered by
its own ``test_archive_security.py`` suite, which runs during service
deployment.  This file covers what the orchestrator adapter must guarantee:
multipart fidelity, fail-closed size/format errors, and never leaking client
file paths into tool results.
"""

from __future__ import annotations

import base64

import httpx
import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install, status_error

CTX = {"task_id": "TASK-ROOT", "tenant_id": "TENANT-1"}


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


def _archive(filename: str = "docs.zip", content: bytes = b"PK\x03\x04fakezip") -> dict:
    return {"filename": filename, "content_type": "application/zip", "content_b64": base64.b64encode(content).decode()}


@pytest.mark.asyncio
async def test_archive_upload_is_multipart_with_filename_and_bytes(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/archive", {
        "task_id": "parent-1", "status": "processing", "processing_stage": "archive_extracting",
        "child_count": 2, "child_ids": ["c-1", "c-2"], "async": True,
    })
    install(monkeypatch, backend)
    registry = _registry()
    content = b"PK\x03\x04real-zip-content"
    result = await registry.call("ingest_m1_archive", {"file": _archive(content=content), "doc_type_hint": "orders"}, CTX)
    assert result["child_count"] == 2
    call = backend.calls[0]
    field, file_tuple = call["files"][0]
    assert field == "file"
    assert file_tuple == ("docs.zip", content, "application/zip")
    assert call["data"]["doc_type_hint"] == "orders"


@pytest.mark.asyncio
async def test_oversized_upload_maps_413_to_stable_tool_error(monkeypatch):
    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise status_error(method, url, 413, "上传超过大小限制")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("ingest_m1_archive", {"file": _archive()}, CTX)
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 413
    assert "上传超过大小限制" in str(error.value)


@pytest.mark.asyncio
async def test_unsupported_format_fails_closed(monkeypatch):
    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise status_error(method, url, 415, {"detail": {"code": "M1_UNSUPPORTED_KIND", "message": "不支持的文件类型", "details": {"reason": "not in allowed_extensions"}}})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("ingest_m1_archive", {"file": _archive(filename="weird.xyz")}, CTX)
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 415
    assert "M1_UNSUPPORTED_KIND" in str(error.value)


@pytest.mark.asyncio
async def test_adapter_never_forwards_client_paths_as_plain_text(monkeypatch):
    """Path traversal attempts must stay inside the file bytes, never appear as
    query/JSON fields the model could echo back as a server path."""
    backend = Backend()
    backend.route("POST", "/ingest/archive", {
        "task_id": "parent-1", "status": "processing", "processing_stage": "archive_extracting",
        "child_count": 0, "child_ids": [], "async": True,
    })
    install(monkeypatch, backend)
    registry = _registry()
    evil = _archive(filename="../../etc/passwd")
    await registry.call("ingest_m1_archive", {"file": evil}, CTX)
    call = backend.calls[0]
    # The client filename only rides as multipart file metadata for the service
    # to classify/record; traversal characters never appear in query/JSON/form
    # fields the agent could echo back, and the service stores uploads under
    # its own task-scoped names (enforced in its archive extraction suite).
    assert call["files"][0][1][0] == "../../etc/passwd"
    serialized = f"{call.get('json')}{call.get('params')}{call.get('data')}"
    assert "passwd" not in serialized

"""M1 Tool binding acceptance: 17/17 bound, local fixture never masquerades."""

from __future__ import annotations

import base64

import pytest

from yunpai_orchestrator.m1_tooling import M1_ADAPTER_TOOL_NAMES, M1_TOOL_NAMES
from yunpai_orchestrator.m1_http_adapter import bind_m1_http
from yunpai_orchestrator.registry import build_default_registry, build_runtime_registry
from yunpai_orchestrator.workers import m1_parse

from m1_fake_http import BASE, Backend, install


def test_default_registry_binds_all_seventeen_m1_tools():
    registry = build_default_registry()
    assert all(name in registry.handlers for name in M1_TOOL_NAMES)
    assert all(name in registry.handlers for name in M1_ADAPTER_TOOL_NAMES)
    assert len([name for name in registry.handlers if registry.specs[name].module == "m1"]) == 17


def test_production_http_runtime_binds_all_m1_tools_with_dedicated_adapter(monkeypatch):
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "http")
    monkeypatch.setenv("YUNPAI_HTTP_MODULES", "m1")
    monkeypatch.setenv("M1_URL", BASE)
    registry = build_runtime_registry()
    assert all(name in registry.handlers for name in M1_TOOL_NAMES)


@pytest.mark.asyncio
async def test_local_fixture_ingest_succeeds_only_for_json_and_is_marked_fixture():
    registry = build_default_registry()
    payload = {
        "file": {
            "filename": "order.json",
            "content_type": "application/json",
            "content_b64": base64.b64encode(b'{"order_id":"SO-1","quantity":5}').decode(),
        }
    }
    result = await registry.call("ingest_document", payload, {"task_id": "T-1", "tenant_id": "TENANT-1"})
    assert result["status"] in {"done", "needs_review"}
    assert result["provider"] == "local_fixture"
    assert result["fixture"] is True


@pytest.mark.asyncio
async def test_local_fixture_fails_closed_on_non_json_binary_without_masquerading():
    registry = build_default_registry()
    payload = {
        "file": {
            "filename": "scan.pdf",
            "content_type": "application/pdf",
            "content_b64": base64.b64encode(b"%PDF-1.4 not really a parsed document").decode(),
        }
    }
    result = await registry.call("ingest_document", payload, {"task_id": "T-1", "tenant_id": "TENANT-1"})
    assert result["status"] == "failed"
    assert result["code"] == "LOCAL_FIXTURE_UNSUPPORTED_FORMAT"
    assert result["document"] is None
    assert result["provider"] == "local_fixture"
    # A failed fixture must never look like a completed full M1 parse.
    assert result["needs_review"] is False


@pytest.mark.asyncio
async def test_production_transport_replaces_fixture_with_real_m1_adapter(monkeypatch):
    backend = Backend()
    backend.route(
        "GET", "/tasks",
        [
            {"task_id": "task-1", "filename": "order.pdf", "status": "done", "needs_review": False, "overall_confidence": 0.97},
        ],
    )
    install(monkeypatch, backend)
    registry = build_default_registry()
    bind_m1_http(registry, urls={"m1": BASE}, tool_names=frozenset(M1_TOOL_NAMES), overwrite=True)
    result = await registry.call("list_m1_tasks", {"limit": 10}, {"task_id": "T-1", "tenant_id": "TENANT-1"})
    assert result[0]["task_id"] == "task-1"
    call = backend.calls[-1]
    # The dedicated M1 adapter (not the generic one) must have handled it.
    assert call["headers"]["X-Tenant-ID"] == "TENANT-1"

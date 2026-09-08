"""Real M1 service contract acceptance (opt-in, environment-gated).

Local HTTP mocks prove transport/schema behaviour but never count as
production acceptance.  When a real standalone M1 service is reachable, run
with ``M1_REAL_TEST=1`` (and optionally ``M1_URL``) to prove /health, /ready
and a tenant-scoped tool call against the live service.  Without the env flag
or a reachable service the tests skip and the acceptance record must state
``生产未验收`` for real-service items.
"""

from __future__ import annotations

import os

import httpx
import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import build_default_registry

from m1_fake_http import BASE

M1_URL = os.getenv("M1_URL", "http://127.0.0.1:8080").rstrip("/")


def _reachable() -> bool:
    if os.getenv("M1_REAL_TEST") != "1":
        return False
    try:
        response = httpx.get(f"{M1_URL}/health", timeout=3)
        return response.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="M1_REAL_TEST 未开启或真实 M1 服务不可达（生产未验收）")


def test_health_and_ready_contract():
    health = httpx.get(f"{M1_URL}/health", timeout=5).json()
    assert health.get("status") == "ok"
    ready = httpx.get(f"{M1_URL}/ready", timeout=5).json()
    assert ready.get("status") in {"ok", "ready"}


@pytest.mark.asyncio
async def test_tenant_scoped_m1_tool_call_against_live_service():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": M1_URL}, overwrite=True)
    tenant = os.getenv("M1_REAL_TENANT", "default")
    result = await registry.call("list_m1_tasks", {"limit": 5}, {"task_id": "real-probe", "tenant_id": tenant})
    assert isinstance(result, list)

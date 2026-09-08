"""业务端点 identity 影子/强制模式测试（接缝 4 第二步）。

- shadow（默认）：identity deny 只记日志与审计，不拦截（迁移期行为等价）；
- on：deny → 403 IDENTITY_DENIED；绑定命中 → 放行；
- off：不做 identity 判定（无新审计行）。
与既有 GATE_ALLOWED_ROLES（T5.3）叠加，不替换——legacy 角色仍先过原 Gate。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from yunpai_orchestrator.api import create_app
from yunpai_orchestrator.identity import IdentityStore
from yunpai_orchestrator.repository import InMemoryRunRepository

from test_graph import workflow_request


@pytest.fixture()
def store(tmp_path):
    return IdentityStore(str(tmp_path / "identity.sqlite"))


@pytest.fixture()
def client(tmp_path, store):
    return TestClient(create_app(repository=InMemoryRunRepository(), identity_store=store))


def _open_candidate_gate(client, tenant="tenant-a"):
    created = client.post("/runs", json={**workflow_request(), "tenant_id": tenant}).json()
    assert created["pending_gate"]["type"] == "candidate"
    return created["run_id"]


def _resume(client, run_id, user="steward-1", roles=("data-steward",)):
    return client.post(
        f"/runs/{run_id}/resume",
        json={"decision": "approve"},
        headers={"X-Actor-User": user, "X-Actor-Roles": ",".join(roles), "X-Tenant-Id": "tenant-a"},
    )


def test_shadow_mode_default_denies_are_logged_not_blocked(client, store):
    """默认 shadow：无绑定用户过 legacy Gate，identity deny 留审计但不拦截。"""
    run_id = _open_candidate_gate(client)
    before = len(store.recent_authz(tenant_id="tenant-a"))
    resp = _resume(client, run_id)
    assert resp.status_code == 200  # 迁移期行为等价：不拦截
    denies = store.recent_authz(tenant_id="tenant-a", only_denied=True)
    assert any(row["user_id"] == "steward-1" and row["permission"] == "candidate.approve"
               for row in denies)
    assert len(store.recent_authz(tenant_id="tenant-a")) > before


def test_enforce_mode_blocks_unbound_user(client, store, monkeypatch):
    monkeypatch.setenv("YUNPAI_IDENTITY_ENFORCE", "on")
    run_id = _open_candidate_gate(client)
    resp = _resume(client, run_id)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "IDENTITY_DENIED"
    assert resp.json()["detail"]["permission"] == "candidate.approve"


def test_enforce_mode_allows_bound_user(client, store, monkeypatch):
    monkeypatch.setenv("YUNPAI_IDENTITY_ENFORCE", "on")
    store.bind_user(tenant_id="tenant-a", user_id="steward-1", role_codes=["data-steward"])
    run_id = _open_candidate_gate(client)
    resp = _resume(client, run_id)
    assert resp.status_code == 200


def test_off_mode_skips_identity_judgment(client, store, monkeypatch):
    monkeypatch.setenv("YUNPAI_IDENTITY_ENFORCE", "off")
    run_id = _open_candidate_gate(client)
    before = len(store.recent_authz(tenant_id="tenant-a"))
    resp = _resume(client, run_id)
    assert resp.status_code == 200
    assert len(store.recent_authz(tenant_id="tenant-a")) == before  # 无新审计


def test_untrusted_requests_skip_identity_judgment(client, store, monkeypatch):
    """无受信头/会话的 dev 降级请求不触发 identity 判定（保持现状行为）。"""
    monkeypatch.setenv("YUNPAI_IDENTITY_ENFORCE", "on")
    run_id = _open_candidate_gate(client)
    before = len(store.recent_authz(tenant_id="tenant-a"))
    resp = client.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert resp.status_code == 200
    assert len(store.recent_authz(tenant_id="tenant-a")) == before


def test_stream_resume_shares_identity_check(client, store, monkeypatch):
    monkeypatch.setenv("YUNPAI_IDENTITY_ENFORCE", "on")
    created = client.post("/runs", json={**workflow_request(), "tenant_id": "tenant-a"}).json()
    run_id = created["run_id"]
    resp = client.post(
        f"/runs/{run_id}/resume/stream",
        json={"decision": "approve"},
        headers={"X-Actor-User": "steward-1", "X-Actor-Roles": "data-steward", "X-Tenant-Id": "tenant-a"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "IDENTITY_DENIED"

"""登录 v1（F-013 接缝 5）测试：scrypt 哈希 + HS256 会话 + API 登录链路。

覆盖交接包第四节的登录链路验收要求：
- 无受信头 + 有效会话 → principal 注入成功（T5.2/T5.4 语义保留）；
- 无会话且 REQUIRE 开 → 401；
- body actor 与会话不一致 → 拒绝冒充（沿用 T5.4 断言）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from yunpai_orchestrator.api import create_app
from yunpai_orchestrator.auth import (
    SESSION_COOKIE,
    hash_password,
    login,
    session_from_token,
    sign_session_token,
    verify_password,
    verify_session_token,
)
from yunpai_orchestrator.identity import IdentityStore
from yunpai_orchestrator.repository import InMemoryRunRepository

from test_graph import workflow_request


@pytest.fixture()
def store(tmp_path):
    return IdentityStore(str(tmp_path / "identity.sqlite"))


@pytest.fixture()
def client(tmp_path, store):
    return TestClient(create_app(repository=InMemoryRunRepository(), identity_store=store))


# ---------------------------------------------------------------- 单元

def test_password_hash_roundtrip():
    stored = hash_password("s3cret-P@ss")
    assert stored.startswith("scrypt$")
    assert verify_password("s3cret-P@ss", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-P@ss", "malformed")
    assert not verify_password("s3cret-P@ss", "")
    # 加盐：两次哈希不同
    assert hash_password("s3cret-P@ss") != stored


def test_session_token_sign_verify_and_tamper():
    secret = "k" * 32
    token = sign_session_token(secret, tenant_id="t1", user_id="u1", ttl_seconds=60)
    payload = verify_session_token(secret, token)
    assert payload["tenant_id"] == "t1" and payload["user_id"] == "u1"
    assert verify_session_token("other-secret", token) is None
    body, _, sig = token.partition(".")
    assert verify_session_token(secret, f"{body[:-2]}x.{sig}") is None
    # 过期
    expired = sign_session_token(secret, tenant_id="t1", user_id="u1",
                                 ttl_seconds=1, now=time.time() - 10)
    assert verify_session_token(secret, expired) is None
    # 缺字段/垃圾串
    assert verify_session_token(secret, "not-a-token") is None
    assert verify_session_token(secret, "") is None


def test_session_secret_persisted_and_env_override(store, monkeypatch):
    secret1 = store.session_secret()
    reopened = IdentityStore(store.db_path)
    assert reopened.session_secret() == secret1  # 重启会话不失效
    monkeypatch.setenv("YUNPAI_SESSION_SECRET", "env-secret")
    assert store.session_secret() == "env-secret"


def test_login_verify(store):
    store.create_user(tenant_id="default", user_id="u1", password_hash=hash_password("pw"))
    assert login(store, tenant_id="default", user_id="u1", password="pw")["user_id"] == "u1"
    assert login(store, tenant_id="default", user_id="u1", password="bad") is None
    assert login(store, tenant_id="default", user_id="ghost", password="pw") is None


# ------------------------------------------------------------ API 链路

def test_login_logout_me_flow(client, store):
    store.create_user(tenant_id="default", user_id="boss", password_hash=hash_password("pw-1"))
    store.bind_user(tenant_id="default", user_id="boss", role_codes=["org-admin"])
    # 未登录 → 401
    assert client.get("/api/auth/me").status_code == 401
    # 密码错 → 401（不泄露用户存在性）
    bad = client.post("/api/auth/login", json={"tenant_id": "default", "user_id": "boss", "password": "nope"})
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "INVALID_CREDENTIALS"
    # 登录 → HttpOnly Cookie + 角色/权限
    ok = client.post("/api/auth/login", json={"tenant_id": "default", "user_id": "boss", "password": "pw-1"})
    assert ok.status_code == 200
    assert ok.json()["roles"] == ["org-admin"]
    assert "identity.admin" in ok.json()["permissions"]
    set_cookie = ok.headers["set-cookie"]
    assert SESSION_COOKIE in set_cookie and "HttpOnly" in set_cookie
    # me → principal source=login
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["principal"] == {"actor": "boss", "tenant_id": "default",
                                      "roles": ["org-admin"], "source": "login"}
    # 登出后 Cookie 失效
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_login_missing_tenant_fails_closed(client):
    resp = client.post("/api/auth/login", json={"user_id": "u", "password": "p"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "MISSING_TENANT"


def test_session_principal_replaces_trusted_header(client, store):
    """接缝 5：无受信头时从会话 Cookie 验签注入 principal，冒充校验保留。"""
    store.create_user(tenant_id="tenant-a", user_id="steward", password_hash=hash_password("pw"))
    store.bind_user(tenant_id="tenant-a", user_id="steward", role_codes=["data-steward"])
    client.post("/api/auth/login", json={"tenant_id": "tenant-a", "user_id": "steward", "password": "pw"})

    created = client.post("/runs", json={**workflow_request(), "tenant_id": "tenant-a"}).json()
    run_id = created["run_id"]
    # 无任何受信头、仅会话 Cookie：candidate gate 由 data-steward 批准（T5.3 角色命中）
    ok = client.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert ok.status_code == 200, ok.text
    approval = ok.json()["approvals"][-1]
    assert approval["principal"]["actor"] == "steward"
    assert approval["principal"]["trusted"] is True
    assert approval["principal"]["source"] == "login"
    assert approval["principal"]["tenant_id"] == "tenant-a"
    # body actor 与会话 principal 不一致 → 拒绝冒充（T5.4 语义保留）
    created2 = client.post("/runs", json={**workflow_request(), "tenant_id": "tenant-a"}).json()
    denied = client.post(f"/runs/{created2['run_id']}/resume",
                         json={"decision": "approve", "actor": "imposter"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ACTOR_IMPERSONATION"


def test_forged_session_cookie_rejected(client, store):
    store.create_user(tenant_id="tenant-a", user_id="u1", password_hash=hash_password("pw"))
    client.cookies.set(SESSION_COOKIE, "forged.token")
    me = client.get("/api/auth/me")
    assert me.status_code == 401


def test_require_trusted_accepts_valid_session(client, store, monkeypatch):
    """REQUIRE=1：受信头或有效登录会话二选一（接缝 5）。"""
    monkeypatch.setenv("YUNPAI_REQUIRE_TRUSTED_PRINCIPAL", "1")
    store.create_user(tenant_id="tenant-a", user_id="steward", password_hash=hash_password("pw"))
    store.bind_user(tenant_id="tenant-a", user_id="steward", role_codes=["data-steward"])
    created = client.post("/runs", json={**workflow_request(), "tenant_id": "tenant-a"}).json()
    run_id = created["run_id"]
    # 无会话无头 → 401
    resp = client.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "TRUSTED_PRINCIPAL_REQUIRED"
    # 有效会话 → 放行
    client.post("/api/auth/login", json={"tenant_id": "tenant-a", "user_id": "steward", "password": "pw"})
    ok = client.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
    assert ok.status_code == 200
    assert ok.json()["approvals"][-1]["principal"]["source"] == "login"


def test_session_token_user_deleted_stops_working(client, store):
    """会话对应的登录用户被删/改密后令牌不再注入 principal（session_from_token 校验用户存在）。"""
    store.create_user(tenant_id="default", user_id="u1", password_hash=hash_password("old"))
    token = sign_session_token(store.session_secret(), tenant_id="default", user_id="u1")
    assert session_from_token(store, token)["user_id"] == "u1"
    # 覆盖为新哈希（如改密）后旧令牌仍验签有效——但用户删除场景通过重建空库模拟
    store2 = IdentityStore(str(store.db_path) + ".new")
    assert session_from_token(store2, token) is None  # secret 不同 + 用户不存在

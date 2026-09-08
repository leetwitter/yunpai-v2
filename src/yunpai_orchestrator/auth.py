"""登录 v1（F-013 接缝 5）：账号密码 + HS256 签名会话 Cookie。

选型说明（交接包接缝 5 为〔设计推定〕，本实现做最小登录服务，不重造 0825
BFF 的 Redis/OIDC/网关件）：

- 密码哈希用标准库 ``hashlib.scrypt``（内存硬化 KDF，与 bcrypt/argon2 同级
  防御；venv 无 bcrypt/argon2/passlib，为零新增依赖选 stdlib），哈希串自带
  参数标签 ``scrypt$N$r$p$salt$hash``，后续可平滑迁移 bcrypt/argon2；
- 会话令牌为 HS256 风格签名令牌（base64url(payload).base64url(HMAC-SHA256)），
  放 HttpOnly Cookie（``yunpai_session``）；单服务足够，拆服务时升 RS256；
- ``api.py`` 在受信头之前接受会话 Cookie 验签注入 principal（trusted=login，
  替换而非新造——T5.2/T5.4 冒充校验与审计标记语义全部保留）；
- 用户表挂 identity store（tenant_id + user_id + 密码哈希 + org_id），初始
  管理员经 ``IDENTITY_BOOTSTRAP_ADMIN`` 或 CLI ``create-admin`` 建立。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from .identity import IdentityStore

#: 会话 Cookie 名（HttpOnly；前端 httpClient 需带凭据）。
SESSION_COOKIE = "yunpai_session"

#: 默认会话有效期 12 小时（秒），可用 ``YUNPAI_SESSION_TTL_SECONDS`` 覆盖。
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 16_384, 8, 1, 32


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """scrypt 加盐哈希（参数内嵌，格式 ``scrypt$N$r$p$salt_b64$hash_b64``）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return "$".join((
        "scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        _b64encode(salt), _b64encode(digest),
    ))


def verify_password(password: str, stored: str) -> bool:
    parts = str(stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = _b64decode(parts[4]), _b64decode(parts[5])
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _session_ttl() -> int:
    try:
        return max(60, int(os.getenv("YUNPAI_SESSION_TTL_SECONDS", "").strip() or DEFAULT_SESSION_TTL_SECONDS))
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS


def sign_session_token(secret: str, *, tenant_id: str, user_id: str,
                       ttl_seconds: int | None = None, now: float | None = None) -> str:
    """签发会话令牌：payload{iat,exp,jti,tenant_id,user_id} + HMAC-SHA256 签名。"""
    issued = int(now if now is not None else time.time())
    payload = {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "iat": issued,
        "exp": issued + (ttl_seconds if ttl_seconds is not None else _session_ttl()),
        "jti": secrets.token_urlsafe(12),
    }
    body = _b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    signature = _b64encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                                    hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_session_token(secret: str, token: str, *, now: float | None = None) -> dict[str, Any] | None:
    """验签会话令牌：签名不符/过期/字段缺失/格式非法返回 None（不抛异常）。"""
    text = str(token or "").strip()
    body, _, signature = text.partition(".")
    if not body or not signature:
        return None
    try:
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            return None
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        # 含 base64/json 解析的全部畸形输入（binascii.Error 是 ValueError 子类）。
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("tenant_id") or not payload.get("user_id"):
        return None
    current = time.time() if now is None else now
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or current >= float(exp):
        return None
    return payload


def login(store: IdentityStore, *, tenant_id: str, user_id: str,
          password: str) -> dict[str, Any] | None:
    """账号密码登录：验证通过返回用户记录，否则 None（不区分用户不存在/密码错）。"""
    user = store.get_user(tenant_id=tenant_id, user_id=str(user_id or ""))
    if not user:
        # 空跑一次哈希，避免「用户不存在」与「密码错误」的响应时间差。
        verify_password(str(password or ""), hash_password(str(password or "")))
        return None
    if not verify_password(str(password or ""), str(user.get("password_hash") or "")):
        return None
    return user


def session_from_token(store: IdentityStore, token: str) -> dict[str, Any] | None:
    """会话令牌 → {tenant_id, user_id}（无效/过期/用户已不存在返回 None）。"""
    payload = verify_session_token(store.session_secret(), token)
    if payload is None:
        return None
    user = store.get_user(tenant_id=payload["tenant_id"], user_id=payload["user_id"])
    if not user:
        return None
    return {"tenant_id": payload["tenant_id"], "user_id": payload["user_id"],
            "display_name": user.get("display_name"), "org_id": user.get("org_id")}

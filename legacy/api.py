from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from .auth import SESSION_COOKIE, login, session_from_token, sign_session_token
from .evolution.api import create_evolution_router
from .evolution.llm_tasks import EvolutionLLM
from .evolution.repository import EvolutionRepository
from .graph import YunpaiGraph
from .guided_chat import SCALE_OPTIONS, handle_message as handle_guidance_message
from .guided_setup import catalog_payload
from .identity import IdentityStore, authorize, permission_for_gate
from .models import new_state
from .registry import ToolRegistry, build_runtime_registry
from .repository import RunRepository, SQLiteRunRepository

logger = logging.getLogger("yunpai.api")

# 端点签名需要 Request 注解（会话 Cookie 读取）；延迟字符串注解由 FastAPI 按
# 模块全局解析，故在模块级条件导入（无 fastapi 环境仍可 import 本模块）。
try:  # pragma: no cover - 视环境而定
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = None  # type: ignore[assignment]


def create_app(*, repository: RunRepository | None = None, registry: ToolRegistry | None = None,
               identity_store: IdentityStore | None = None, evolution: EvolutionRepository | None = None):
    try:
        from fastapi import FastAPI, File, Form, Header, HTTPException
        from fastapi.responses import JSONResponse, StreamingResponse
    except ImportError as exc: raise RuntimeError("安装 fastapi 后才能启动 HTTP API") from exc
    if repository is None:
        db_path = Path(os.getenv("YUNPAI_RUN_DB", "runtime/yunpai-runs.sqlite"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteRunRepository(db_path)
    # 知识自进化演进层（EV-1）。观察钩子默认关闭，YUNPAI_EVOLUTION_ENABLED=1 开启；
    # 显式传入 evolution= 时始终开启。决策/治理/快捷操作路由始终可用。
    evolution_store = evolution or EvolutionRepository(os.getenv("YUNPAI_EVOLUTION_DB", "runtime/yunpai-evolution.sqlite"))
    evolution_observe = evolution is not None or os.getenv("YUNPAI_EVOLUTION_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    evolution_llm = EvolutionLLM()
    graph = YunpaiGraph(registry or build_runtime_registry(), repository, evolution=evolution_store if evolution_observe else None)
    # 身份/组织/权限事实源（F-013/F-014/F-015 正式版，2026-09-07）。
    if identity_store is None:
        identity_store = IdentityStore(os.getenv("YUNPAI_IDENTITY_DB", "runtime/yunpai-identity.sqlite"))
    app = FastAPI(title="Yunpai LangGraph", version="0.2.0")
    # 测试前端可跨源调用（简单前端同源托管于 /ui，此处仅为兼容 file:// 直开）。
    try:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    except Exception:  # pragma: no cover
        pass
    app.include_router(create_evolution_router(evolution_store, evolution_llm))
    # 简单测试前端（存在才挂载；纯静态，无构建）。
    _ui_dir = os.getenv("YUNPAI_UI_DIR", Path(__file__).resolve().parents[2] / "frontend-test")
    if Path(_ui_dir).is_dir():
        try:
            from fastapi.staticfiles import StaticFiles
            app.mount("/ui", StaticFiles(directory=str(_ui_dir), html=True), name="ui")
        except Exception:  # pragma: no cover
            pass

    def _principal_from_headers(headers: dict[str, str],
                                cookies: dict[str, str] | None = None) -> tuple[dict[str, Any], bool]:
        """从受信反向代理/认证中间件读取审批 principal（T5.2）。

        优先 ``X-Yunpai-Principal``（JSON：actor/roles/tenant_id），其次拆分头
        ``X-Actor-User`` + ``X-Actor-Roles`` + ``X-Tenant-Id``；无受信头时尝试
        登录会话 Cookie（接缝 5：验签解出 principal，trusted=login，角色取
        identity 绑定解析结果）注入现有链路——替换而非新造，冒充校验语义
        不变。返回 (principal, trusted)；两者皆无且未强制受信时 actor 交由
        调用方从 body 取（dev/preview 降级），审计标记 principal_source=untrusted_body。
        """
        header = headers.get("x-yunpai-principal")
        if header:
            try:
                value = json.loads(header)
                actor = str(value.get("actor") or "")
                roles = value.get("roles") if isinstance(value.get("roles"), list) else [str(value.get("role") or "")]
                tenant = str(value.get("tenant_id") or value.get("tenant") or "")
                if actor:
                    return {"actor": actor, "roles": [str(r) for r in roles], "tenant_id": tenant, "source": "trusted_header"}, True
            except (ValueError, TypeError):
                raise HTTPException(400, {"code": "INVALID_PRINCIPAL", "message": "X-Yunpai-Principal 不是合法 JSON"})
        user = headers.get("x-actor-user") or headers.get("x-yunpai-actor-user")
        if user:
            roles = [item.strip() for item in (headers.get("x-actor-roles") or "").split(",") if item.strip()]
            tenant = headers.get("x-tenant-id") or headers.get("x-yunpai-tenant-id") or ""
            return {"actor": str(user), "roles": roles, "tenant_id": str(tenant), "source": "trusted_header"}, True
        session_token = str((cookies or {}).get(SESSION_COOKIE) or "")
        if session_token:
            session = session_from_token(identity_store, session_token)
            if session:
                resolved = identity_store.resolve(
                    tenant_id=session["tenant_id"], user_id=session["user_id"])
                return {
                    "actor": session["user_id"],
                    "roles": list(resolved["roles"]),
                    "tenant_id": session["tenant_id"],
                    "source": "login",
                }, True
        require_trusted = os.getenv("YUNPAI_REQUIRE_TRUSTED_PRINCIPAL", "0").lower() in {"1", "true", "yes"}
        if require_trusted:
            # 接缝 5：语义升级为「受信头或有效登录会话二选一」，无两者 401。
            raise HTTPException(401, {"code": "TRUSTED_PRINCIPAL_REQUIRED",
                                      "message": "必须提供受信认证头（X-Yunpai-Principal/X-Actor-User）或有效登录会话 Cookie"})
        return {}, False

    def _resolve_tenant(*, explicit: Any, headers: dict[str, str]) -> str:
        """运行入口租户解析（P1.2，与 M1 适配器 fail-closed 对齐）。

        解析顺序：显式参数（body/form/query 的 tenant_id）→ 租户头
        （X-Yunpai-Tenant-ID / X-Tenant-ID）→ 兼容开关 ``YUNPAI_DEFAULT_TENANT``
        （单租户内网部署显式声明，请求时读取）→ 都没有则 400 MISSING_TENANT，
        不再静默落入 default 租户。
        """
        tenant = str(explicit or "").strip()
        if not tenant:
            tenant = str(headers.get("x-yunpai-tenant-id") or headers.get("x-tenant-id") or "").strip()
        if not tenant:
            fallback = os.getenv("YUNPAI_DEFAULT_TENANT", "").strip()
            if fallback:
                return fallback
            raise HTTPException(400, {
                "code": "MISSING_TENANT",
                "message": "缺少租户上下文（tenant_id 参数或 X-Yunpai-Tenant-ID 头），已失败关闭；"
                           "单租户内网部署可设置 YUNPAI_DEFAULT_TENANT 显式兼容",
            })
        return tenant

    def _principal_actor(principal: dict[str, Any], trusted: bool, body: dict[str, Any]) -> tuple[str, list[str]]:
        body_actor = str(body.get("actor") or "")
        if trusted:
            actor = str(principal.get("actor") or "")
            if body_actor and body_actor != actor:
                # T5.4：body 冒充受信 principal 必须拒绝并记录审计。
                raise HTTPException(403, {"code": "ACTOR_IMPERSONATION",
                                          "message": f"请求体 actor={body_actor} 与受信 principal={actor} 不一致，拒绝冒充审批"})
            return actor, list(principal.get("roles") or [])
        # dev/preview 降级：无受信头时 body actor 不构成受信身份；若部署方
        # 要求受信（YUNPAI_REQUIRE_TRUSTED_PRINCIPAL=1）已在解析处拒绝。
        return body_actor or "operator", []

    @app.get("/health")
    async def health():
        from collections import Counter

        specs = graph.registry.specs
        bound = graph.registry.handlers
        spec_by_module = Counter(spec.module for spec in specs.values())
        bound_by_module = Counter()
        for name in bound:
            spec = specs.get(name)
            if spec is not None:
                bound_by_module[spec.module] += 1
        modules = {
            module: {"spec": int(spec_by_module.get(module, 0)), "bound": int(bound_by_module.get(module, 0))}
            for module in sorted(set(spec_by_module) | set(bound_by_module))
        }
        payload = {"status": "ok", "module": "yunpai-langgraph", "tools": len(specs), "bound_tools": len(bound), "skills": len(graph.skills.specs), "planner_model": graph.planner.router.config.public(), "modules": modules}
        environment = getattr(graph.registry, "environment", None)
        if environment:
            payload["environment"] = environment
        return payload

    @app.get("/tools")
    async def list_tools(module: str | None = None):
        catalog = graph.registry.catalog()
        return {"tools": [item for item in catalog if module is None or item["module"] == module]}

    @app.get("/skills")
    async def list_skills():
        return {"skills": graph.skills.catalog()}

    @app.post("/runs")
    async def create_run(body: dict[str, Any],
                         x_yunpai_tenant_id: str | None = Header(None),
                         x_tenant_id: str | None = Header(None)):
        request = body.get("request", body)
        if not isinstance(request, dict):
            raise HTTPException(422, "request must be an object")
        tenant_id = _resolve_tenant(explicit=body.get("tenant_id"), headers={
            "x-yunpai-tenant-id": x_yunpai_tenant_id or "",
            "x-tenant-id": x_tenant_id or "",
        })
        try:
            state = await graph.run(new_state(request, tenant_id=tenant_id))
            return graph._public_state(state)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    def ndjson_response(events):
        async def body():
            async for event in events:
                yield json.dumps(event, ensure_ascii=False) + "\n"

        return StreamingResponse(
            body(),
            media_type="application/x-ndjson",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    @app.post("/runs/stream")
    async def create_streaming_run(body: dict[str, Any],
                                   x_yunpai_tenant_id: str | None = Header(None),
                                   x_tenant_id: str | None = Header(None)):
        request = body.get("request", body)
        if not isinstance(request, dict):
            raise HTTPException(422, "request must be an object")
        tenant_id = _resolve_tenant(explicit=body.get("tenant_id"), headers={
            "x-yunpai-tenant-id": x_yunpai_tenant_id or "",
            "x-tenant-id": x_tenant_id or "",
        })
        state = new_state(request, tenant_id=tenant_id)
        graph.repository.save(state)
        return ndjson_response(graph.stream(state))

    @app.post("/runs/upload")
    async def upload_run(file: Any = File(...), message: str = "请解析并验证这份订单", tenant_id: str = "",
                         workflow: str | None = None,
                         x_yunpai_tenant_id: str | None = Header(None),
                         x_tenant_id: str | None = Header(None)):
        """单文件上传入口：保存原字节/哈希/类型/相对路径为 attachment reference，
        再交给 Planner 选择 workflow。API 层不做固定 XLSX 解析；支持的实际类型
        以 M1 工具合同为准（PDF/图片/XLS*/CSV/DOCX/DXF-DWG/ZIP-TAR-RAR-7Z 等）。
        """
        from .file_sniff import sniff_format
        from .uploads import MAX_FILE_BYTES, sha256_of

        tenant_id = _resolve_tenant(explicit=tenant_id, headers={
            "x-yunpai-tenant-id": x_yunpai_tenant_id or "",
            "x-tenant-id": x_tenant_id or "",
        })
        raw = await file.read()
        if not raw:
            raise HTTPException(400, "uploaded file is empty")
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, {"code": "FILE_TOO_LARGE", "message": f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 上限"})
        filename = str(file.filename or "upload.bin")
        verdict = sniff_format(raw, filename)
        if verdict.detected_format == "unknown" or not verdict.match:
            raise HTTPException(415, {
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": f"无法识别的文件类型（声明 .{verdict.declared_suffix.strip('.')}，嗅探 {verdict.detected_format}）；请上传 M1 支持的订单/业务资料格式",
            })
        attachment = {
            "id": "upload-1",
            "kind": "order",
            "filename": filename,
            "relative_path": filename,
            "content_type": verdict.mime_type,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "size": len(raw),
            "sha256": sha256_of(raw),
            "detected_format": verdict.detected_format,
        }
        request: dict[str, Any] = {
            "message": message,
            "attachments": [attachment],
        }
        if workflow:
            request["workflow"] = workflow
        try:
            state = await graph.run(new_state(request, tenant_id=tenant_id))
            return graph._public_state(state)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/runs/upload/batch")
    async def upload_batch_run(files: list[Any] = File(...), message: str = Form("请识别并登记这些业务资料"),
                               mode: str = Form(...), tenant_id: str = Form(""),
                               x_yunpai_tenant_id: str | None = Header(None),
                               x_tenant_id: str | None = Header(None)):
        """目录/基础资料/订单批量上传：显式 mode，逐文件返回状态与批次摘要。

        与单文件 /runs/upload 不同，本端点不靠用户文案猜测模式；缺 content_b64、
        坏 base64、超限或空文件都会以 skipped/parse_failed 状态逐文件返回，
        而不是静默继续或整批 400。mode 必须是 order/master_data/directory。
        """
        from .uploads import (
            MAX_BATCH_BYTES,
            MAX_BATCH_FILES,
            MAX_FILE_BYTES,
            UPLOAD_MODES,
            UploadSummary,
            sha256_of,
            to_attachment_record,
        )

        tenant_id = _resolve_tenant(explicit=tenant_id, headers={
            "x-yunpai-tenant-id": x_yunpai_tenant_id or "",
            "x-tenant-id": x_tenant_id or "",
        })
        mode = str(mode).strip()
        if mode not in UPLOAD_MODES:
            raise HTTPException(422, {"code": "INVALID_UPLOAD_MODE", "message": f"mode 必须为 {'/'.join(UPLOAD_MODES)} 之一", "allowed": list(UPLOAD_MODES)})
        if len(files) > MAX_BATCH_FILES:
            raise HTTPException(413, {"code": "BATCH_TOO_LARGE", "message": f"单批文件数超过 {MAX_BATCH_FILES}"})
        summary = UploadSummary(mode=mode)
        attachments: list[dict[str, Any]] = []
        batch_bytes = 0
        for index, file in enumerate(files, start=1):
            raw = await file.read()
            if not raw:
                summary.add(to_attachment_record({"filename": str(file.filename or f"upload-{index}.bin")}, mode=mode, status="skipped", reason="empty_content"))
                continue
            if len(raw) > MAX_FILE_BYTES:
                summary.add(to_attachment_record({"filename": str(file.filename or f"upload-{index}.bin"), "size": len(raw)}, mode=mode, status="skipped", reason=f"exceeds_single_file_limit_{MAX_FILE_BYTES}"))
                continue
            batch_bytes += len(raw)
            if batch_bytes > MAX_BATCH_BYTES:
                summary.add(to_attachment_record({"filename": str(file.filename or f"upload-{index}.bin"), "size": len(raw)}, mode=mode, status="skipped", reason="exceeds_batch_size_limit"))
                continue
            digest = sha256_of(raw)
            attachment = {
                "id": f"{str(file.filename or f'upload-{index}')}-{index}",
                "kind": mode if mode in {"order", "master_data"} else "master_data",
                "filename": str(file.filename or f"upload-{index}.bin"),
                "relative_path": str(file.filename or f"upload-{index}.bin"),
                "content_type": file.content_type or "application/octet-stream",
                "content_b64": base64.b64encode(raw).decode("ascii"),
                "size": len(raw),
                "sha256": digest,
            }
            attachments.append(attachment)
            summary.add(to_attachment_record(attachment, mode=mode, status="accepted"))
        if not attachments:
            return {
                "run_id": "", "task_id": "", "status": "failed",
                "upload_summary": summary.as_dict(),
                "error": {"code": "NO_ACCEPTED_FILES", "message": "没有可识别的文件"},
            }
        request: dict[str, Any] = {
            "message": message,
            "upload_mode": mode,
            "attachments": attachments,
        }
        try:
            state = await graph.run(new_state(request, tenant_id=tenant_id))
            public = graph._public_state(state)
            public["upload_summary"] = summary.as_dict()
            return public
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/runs")
    async def list_runs(tenant_id: str | None = None, limit: int = 100):
        return {"runs": [graph._public_state(state) for state in graph.repository.list(tenant_id=tenant_id, limit=limit)]}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        state = graph.repository.get(run_id)
        if state is None: raise HTTPException(404, "run not found")
        return graph._public_state(state)

    def _request_principal(request) -> tuple[dict[str, Any], bool]:
        return _principal_from_headers({
            "x-yunpai-principal": request.headers.get("x-yunpai-principal", ""),
            "x-actor-user": request.headers.get("x-actor-user", ""),
            "x-actor-roles": request.headers.get("x-actor-roles", ""),
            "x-tenant-id": request.headers.get("x-tenant-id", "")
            or request.headers.get("x-yunpai-tenant-id", ""),
        }, cookies=dict(request.cookies))

    def _identity_gate_check(state: dict[str, Any], *, actor: str, roles: list[str],
                             principal: dict[str, Any], trusted: bool) -> None:
        """业务端点 identity 判定（接缝 4 第二步）。

        ``YUNPAI_IDENTITY_ENFORCE``：``off`` 跳过；``shadow``（默认）只记
        判定结果与审计不拦截（跑一个验收轮）；``on`` 强制（deny → 403）。
        与既有 GATE_ALLOWED_ROLES（T5.3）叠加，不替换。
        """
        mode = os.getenv("YUNPAI_IDENTITY_ENFORCE", "shadow").strip().lower()
        if mode in {"", "off"} or not trusted or not str(actor or "").strip():
            return
        gate_type = str((state.get("pending_gate") or {}).get("type") or "")
        permission = permission_for_gate(gate_type)
        if not permission:
            return
        tenant = str(principal.get("tenant_id") or state.get("tenant_id") or "default")
        decision = authorize(identity_store, tenant_id=tenant, user_id=actor,
                             permission=permission, legacy_roles=list(roles or []))
        if decision["allowed"]:
            return
        if mode == "on":
            raise HTTPException(403, {"code": "IDENTITY_DENIED",
                                      "message": f"gate {gate_type} 需要 {decision['resolved']['role_names'] or '已绑定角色'} 之外的权限 {permission}",
                                      "permission": permission, "reason": decision["reason"]})
        logger.warning("identity shadow deny tenant=%s user=%s gate=%s permission=%s reason=%s",
                       tenant, actor, gate_type, permission, decision["reason"])

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, body: dict[str, Any], request: Request,
                         x_yunpai_principal: str | None = Header(None),
                         x_actor_user: str | None = Header(None),
                         x_actor_roles: str | None = Header(None),
                         x_tenant_id: str | None = Header(None)):
        state = graph.repository.get(run_id)
        if state is None: raise HTTPException(404, "run not found")
        principal, trusted = _principal_from_headers({
            "x-yunpai-principal": x_yunpai_principal or "",
            "x-actor-user": x_actor_user or "",
            "x-actor-roles": x_actor_roles or "",
            "x-tenant-id": x_tenant_id or "",
        }, cookies=dict(request.cookies))
        actor, roles = _principal_actor(principal, trusted, body)
        decision = str(body.get("decision", "allow"))
        human_override = bool(body.get("human_override", False))
        override_reason = str(body.get("override_reason") or "")
        try:
            graph.validate_resume_decision(
                state, decision, body.get("supplement"), human_override=human_override
            )
            if trusted:
                # 只有受信 principal 才做角色/租户 Gate；本地无认证降级路径
                # 保留操作能力但审计标记 untrusted_body（生产强制受信）。
                graph.authorize_gate(state, actor=actor, roles=roles,
                                     tenant_id=principal.get("tenant_id") or None)
        except ValueError as exc:
            status = 403 if any(token in str(exc) for token in ("role", "tenant", "anonymous")) else 409
            raise HTTPException(status, str(exc)) from exc
        _identity_gate_check(state, actor=actor, roles=roles, principal=principal, trusted=trusted)
        try:
            resumed = await graph.resume(
                state,
                decision,
                body.get("supplement"),
                actor=actor,
                human_override=human_override,
                override_reason=override_reason,
            )
            if resumed.get("approvals"):
                resumed["approvals"][-1].setdefault("principal", {
                    "trusted": trusted, "actor": actor, "roles": roles,
                    "tenant_id": principal.get("tenant_id") or "",
                })
                if principal.get("source"):
                    resumed["approvals"][-1]["principal"]["source"] = principal["source"]
                if not trusted:
                    resumed["approvals"][-1]["principal"]["source"] = "untrusted_body"
            return graph._public_state(resumed)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/runs/{run_id}/resume/stream")
    async def resume_stream(run_id: str, body: dict[str, Any], request: Request,
                            x_yunpai_principal: str | None = Header(None),
                            x_actor_user: str | None = Header(None),
                            x_actor_roles: str | None = Header(None),
                            x_tenant_id: str | None = Header(None)):
        state = graph.repository.get(run_id)
        if state is None:
            raise HTTPException(404, "run not found")
        principal, trusted = _principal_from_headers({
            "x-yunpai-principal": x_yunpai_principal or "",
            "x-actor-user": x_actor_user or "",
            "x-actor-roles": x_actor_roles or "",
            "x-tenant-id": x_tenant_id or "",
        }, cookies=dict(request.cookies))
        actor, roles = _principal_actor(principal, trusted, body)
        decision = str(body.get("decision", "allow"))
        human_override = bool(body.get("human_override", False))
        override_reason = str(body.get("override_reason") or "")
        if state.get("status") != "waiting_human" or not state.get("pending_gate"):
            raise HTTPException(409, "run is not waiting_human")
        if decision not in {"allow", "approve", "continue", "retry", "reject", "stop"}:
            raise HTTPException(409, "unsupported gate decision")
        try:
            graph.validate_resume_decision(
                state, decision, body.get("supplement"), human_override=human_override
            )
            if trusted:
                graph.authorize_gate(state, actor=actor, roles=roles,
                                     tenant_id=principal.get("tenant_id") or None)
        except ValueError as exc:
            status = 403 if any(token in str(exc) for token in ("role", "tenant", "anonymous")) else 409
            raise HTTPException(status, str(exc)) from exc
        _identity_gate_check(state, actor=actor, roles=roles, principal=principal, trusted=trusted)
        return ndjson_response(
            graph.stream_resume(
                state,
                decision,
                body.get("supplement"),
                actor=actor,
                human_override=human_override,
                override_reason=override_reason,
            )
        )

    @app.get("/m0/readback/{batch_id}")
    async def m0_readback(batch_id: str):
        """M0 canonical 回读报告（dry-run 语义）。

        返回当前 transport 是否具备真实 M0 canonical/ledger/outbox 回读；
        没有回读证据时绝不写“生产完成”。
        """
        from .m0_sandbox import M0SandboxStore

        db_path = Path(os.getenv("YUNPAI_M0_SANDBOX_DB", "runtime/yunpai-m0-sandbox.sqlite"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        transport = os.getenv("YUNPAI_TOOL_TRANSPORT", "local").lower()
        real_available = transport == "http" and bool(os.getenv("M0_URL"))
        return M0SandboxStore(db_path).readback_report(batch_id, real_m0_available=real_available)
    # ------------------------------------------------- 登录 v1（F-013 接缝 5）

    def _require_identity_permission(permission: str, *, tenant_id: str,
                                     request) -> dict[str, Any]:
        """identity 管理 API 的鉴权门（接缝 4 第一步：identity 自有 API 全过
        authorize）。接受受信头（legacy 角色）或登录会话；deny → 403 且已留审计。"""
        principal, trusted = _request_principal(request)
        actor = str(principal.get("actor") or "")
        if not trusted or not actor:
            raise HTTPException(401, {"code": "AUTHENTICATION_REQUIRED",
                                      "message": "需要受信认证头或有效登录会话"})
        principal_tenant = str(principal.get("tenant_id") or "")
        if principal_tenant and principal_tenant != tenant_id:
            raise HTTPException(403, {"code": "CROSS_TENANT",
                                      "message": f"principal 租户 {principal_tenant} 无权管理租户 {tenant_id}"})
        decision = authorize(identity_store, tenant_id=tenant_id, user_id=actor,
                             permission=permission,
                             legacy_roles=list(principal.get("roles") or []))
        if not decision["allowed"]:
            raise HTTPException(403, {"code": "IDENTITY_DENIED",
                                      "message": f"缺少权限 {permission}（reason={decision['reason']}）",
                                      "permission": permission, "reason": decision["reason"]})
        return principal

    def _header_tenant(request) -> dict[str, str]:
        if request is None:
            return {}
        return {
            "x-yunpai-tenant-id": request.headers.get("x-yunpai-tenant-id", ""),
            "x-tenant-id": request.headers.get("x-tenant-id", ""),
        }

    def _tenant_for_request(explicit: Any, request) -> str:
        """identity 端点租户解析：显式参数 → 会话/受信头 principal 租户 →
        租户头 → YUNPAI_DEFAULT_TENANT → 400 MISSING_TENANT。"""
        if request is not None and not str(explicit or "").strip():
            principal, _ = _request_principal(request)
            explicit = str(principal.get("tenant_id") or "")
        return _resolve_tenant(explicit=explicit, headers=_header_tenant(request))

    @app.post("/api/auth/login")
    async def auth_login(body: dict[str, Any],
                         x_yunpai_tenant_id: str | None = Header(None),
                         x_tenant_id: str | None = Header(None)):
        tenant_id = _resolve_tenant(explicit=body.get("tenant_id"), headers={
            "x-yunpai-tenant-id": x_yunpai_tenant_id or "",
            "x-tenant-id": x_tenant_id or "",
        })
        user_id = str(body.get("user_id") or "")
        user = login(identity_store, tenant_id=tenant_id, user_id=user_id,
                     password=str(body.get("password") or ""))
        if not user:
            raise HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"})
        token = sign_session_token(identity_store.session_secret(),
                                   tenant_id=tenant_id, user_id=user_id)
        resolved = identity_store.resolve(tenant_id=tenant_id, user_id=user_id)
        response = JSONResponse({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "display_name": user.get("display_name"),
            "roles": resolved["roles"],
            "role_names": resolved["role_names"],
            "permissions": resolved["permissions"],
        })
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/")
        return response

    @app.post("/api/auth/logout")
    async def auth_logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        principal, trusted = _request_principal(request)
        actor = str(principal.get("actor") or "")
        if not trusted or not actor:
            raise HTTPException(401, {"code": "AUTHENTICATION_REQUIRED",
                                      "message": "未登录（无有效会话或受信头）"})
        tenant_id = str(principal.get("tenant_id") or "")
        if not tenant_id:
            raise HTTPException(401, {"code": "AUTHENTICATION_REQUIRED",
                                      "message": "principal 缺租户上下文"})
        resolved = identity_store.resolve(tenant_id=tenant_id, user_id=actor)
        return {"principal": {"actor": actor, "tenant_id": tenant_id,
                              "roles": list(principal.get("roles") or []),
                              "source": principal.get("source")},
                **resolved}

    # ------------------------------------------- identity 管理 API（P-011/P-012）

    @app.get("/api/identity/catalog")
    async def identity_catalog():
        """权限清单 + 种子角色（只读静态目录，引导AI/前端共用，不涉敏感数据）。"""
        return catalog_payload()

    @app.get("/api/identity/org")
    async def identity_org_tree(tenant_id: str = "", request: Request = None):
        tenant = _tenant_for_request(tenant_id, request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        return {"tenant_id": tenant, "org": identity_store.org_tree(tenant_id=tenant)}

    @app.post("/api/identity/org")
    async def identity_org_upsert(body: dict[str, Any], request: Request):
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        node = identity_store.upsert_org(
            tenant_id=tenant,
            org_id=str(body.get("org_id") or ""),
            name=str(body.get("name") or ""),
            parent_id=body.get("parent_id"),
            org_type=str(body.get("org_type") or "dept"),
            source="manual",
        )
        return node

    @app.post("/api/identity/org/derive")
    async def identity_org_derive(body: dict[str, Any], request: Request):
        """从 canonical worker 实体派生组织树（接缝 3；手工节点不覆盖）。"""
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        from .m0_backend import M0Store

        m0_db = Path(os.getenv("YUNPAI_M0_DB", "runtime/yunpai-m0.sqlite"))
        workers = [entity.get("payload_json") or {}
                   for entity in M0Store(m0_db).list_entities("worker", tenant).get("entities", [])]
        if not workers:
            raise HTTPException(409, {"code": "NO_WORKER_ENTITIES",
                                      "message": f"tenant={tenant} canonical 无 worker 实体，无法派生"})
        result = identity_store.derive_org_from_workers(workers, tenant_id=tenant)
        return {"tenant_id": tenant, "workers": len(workers),
                "created": result["created"],
                "skipped_manual": result["skipped_manual"],
                "departments": result["departments"]}

    @app.get("/api/identity/roles")
    async def identity_roles(tenant_id: str = "", request: Request = None):
        tenant = _tenant_for_request(tenant_id, request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        return {"tenant_id": tenant, "roles": identity_store.list_roles(tenant_id=tenant)}

    @app.post("/api/identity/roles")
    async def identity_role_upsert(body: dict[str, Any], request: Request):
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        try:
            role = identity_store.upsert_role(
                tenant_id=tenant,
                role_code=str(body.get("role_code") or ""),
                name=str(body.get("name") or ""),
                permissions=[str(p) for p in (body.get("permissions") or [])],
            )
        except ValueError as exc:
            raise HTTPException(422, {"code": "INVALID_ROLE", "message": str(exc)}) from exc
        return role

    @app.get("/api/identity/bindings")
    async def identity_bindings(tenant_id: str = "", user_id: str = "",
                                request: Request = None):
        tenant = _tenant_for_request(tenant_id, request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        return {"tenant_id": tenant,
                "bindings": identity_store.list_bindings(tenant_id=tenant, user_id=user_id or None)}

    @app.post("/api/identity/bindings")
    async def identity_bind_user(body: dict[str, Any], request: Request):
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        try:
            bound = identity_store.bind_user(
                tenant_id=tenant,
                user_id=str(body.get("user_id") or ""),
                role_codes=[str(r) for r in (body.get("role_codes") or [])],
                org_id=body.get("org_id"),
                skill=body.get("skill"),
            )
        except ValueError as exc:
            raise HTTPException(422, {"code": "INVALID_BINDING", "message": str(exc)}) from exc
        return bound

    @app.post("/api/identity/bindings/bulk")
    async def identity_bind_users_bulk(body: dict[str, Any], request: Request):
        """按部门批量授权（接缝 3 阶段③）：逐条绑定，任何一条失败整批 422。"""
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        items = body.get("bindings")
        if not isinstance(items, list) or not items:
            raise HTTPException(422, {"code": "INVALID_BULK_BINDING", "message": "bindings 必须为非空数组"})
        try:
            return identity_store.bind_users_bulk(
                tenant_id=tenant, bindings=[item for item in items if isinstance(item, dict)])
        except ValueError as exc:
            raise HTTPException(422, {"code": "INVALID_BULK_BINDING", "message": str(exc)}) from exc

    @app.get("/api/identity/resolve")
    async def identity_resolve(tenant_id: str = "", user_id: str = "",
                               request: Request = None):
        tenant = _tenant_for_request(tenant_id, request)
        principal = _request_principal(request)[0]
        actor = str(principal.get("actor") or "")
        target = user_id or actor
        if target != actor:
            _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        elif not actor:
            raise HTTPException(401, {"code": "AUTHENTICATION_REQUIRED", "message": "未认证"})
        return identity_store.resolve(tenant_id=tenant, user_id=target)

    @app.get("/api/identity/authz/recent")
    async def identity_authz_recent(tenant_id: str = "", limit: int = 50,
                                    request: Request = None):
        """最近授权判定（deny 留痕查询；identity.admin）。"""
        tenant = _tenant_for_request(tenant_id, request)
        _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        return {"tenant_id": tenant,
                "decisions": identity_store.recent_authz(tenant_id=tenant, limit=max(1, min(limit, 500)))}

    # --------------------------------------------- 引导AI（F-015 / P-013）

    def _load_workers(body: dict[str, Any], tenant: str) -> list[dict[str, Any]]:
        explicit = body.get("workers")
        if isinstance(explicit, list) and explicit:
            return [item for item in explicit if isinstance(item, dict)]
        from .m0_backend import M0Store

        m0_db = Path(os.getenv("YUNPAI_M0_DB", "runtime/yunpai-m0.sqlite"))
        return [entity.get("payload_json") or {}
                for entity in M0Store(m0_db).list_entities("worker", tenant).get("entities", [])]

    # --------------------------------------------- 引导AI 轻量对话（F-015 重设计）

    @app.get("/api/guidance/presets")
    async def guidance_presets():
        """规模三档选项（只读，前端渲染首开三选一按钮；具体架构由引导模型生成）。"""
        return {"presets": [dict(o) for o in SCALE_OPTIONS]}

    @app.post("/api/guidance/chat")
    async def guidance_chat(body: dict[str, Any], request: Request):
        """引导AI 对话入口（LLM 驱动）：规模三选一 → 模型生成架构与分配判断 →
        对话增删改 → 确认落地。state 为客户端回传的不透明 JSON，服务端无会话表。"""
        tenant = _tenant_for_request(body.get("tenant_id"), request)
        principal = _require_identity_permission("identity.admin", tenant_id=tenant, request=request)
        roster = _load_workers(body, tenant)
        router = getattr(graph.planner, "router", None)
        result = await handle_guidance_message(
            identity_store, tenant_id=tenant, user_id=str(principal.get("actor") or ""),
            message=str(body.get("message") or ""), roster=roster,
            state=body.get("state"), router=router,
        )
        return {"tenant_id": tenant, **result}

    return app


def main() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=9000)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .gate import promotion_checks
from .injection import build_context, retrieve_active_knowledge, retrieve_shortcuts
from .redline import RedlineMonitor
from .repository import EvolutionRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor_from_headers(headers: dict[str, str]) -> str:
    header = headers.get("x-yunpai-principal")
    if header:
        try:
            value = json.loads(header)
            actor = str(value.get("actor") or "")
            if actor:
                return actor
        except (ValueError, TypeError):
            pass
    return headers.get("x-actor-user") or headers.get("x-yunpai-actor-user") or "operator"


def _cooldown_until_iso(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def create_evolution_router(store: EvolutionRepository, llm: Any | None = None) -> Any:
    try:
        from fastapi import APIRouter, Body, Header, HTTPException, Query
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("安装 fastapi 后才能启动 HTTP API") from exc

    router = APIRouter(prefix="/api", tags=["evolution"])

    def _tenant(tenant_id: str | None, headers: dict[str, str]) -> str:
        return (tenant_id or headers.get("x-yunpai-tenant-id") or headers.get("x-tenant-id") or "default").strip() or "default"

    @router.get("/evolution/status")
    async def status():
        return {
            "llm": llm.public() if llm is not None else None,
            "store": "evolution",
        }

    @router.get("/evolution/candidates")
    async def list_candidates(status: str | None = None, kind: str | None = None,
                              tenant_id: str | None = None, limit: int = 100,
                              x_yunpai_tenant_id: str | None = Header(None),
                              x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        items = store.list_candidates(tenant_id=tenant, status=status, kind=kind, limit=limit)
        return {"candidates": items, "count": len(items)}

    @router.get("/evolution/pending")
    async def list_pending(tenant_id: str | None = None, limit: int = 50,
                           x_yunpai_tenant_id: str | None = Header(None),
                           x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        items = store.list_pending(tenant_id=tenant, limit=limit)
        return {"pending": items, "count": len(items)}

    @router.post("/evolution/candidates/{candidate_id}/decision")
    async def decide_candidate(candidate_id: str, body: dict[str, Any] = Body(...),
                               x_yunpai_tenant_id: str | None = Header(None),
                               x_tenant_id: str | None = Header(None),
                               x_actor_user: str | None = Header(None)):
        candidate = store.get_candidate(candidate_id)
        if candidate is None:
            raise HTTPException(404, {"code": "CANDIDATE_NOT_FOUND", "message": "候选不存在"})
        tenant = candidate.get("tenant_id") or _tenant(None, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        actor = _actor_from_headers({"x-actor-user": x_actor_user or ""})
        decision = str(body.get("decision") or "")
        reason = str(body.get("reason") or "")
        if decision not in {"approve", "approve_modified", "reject", "later"}:
            raise HTTPException(422, {"code": "INVALID_DECISION", "message": "decision 必须为 approve/approve_modified/reject/later"})

        if decision == "reject":
            store.set_candidate_status(candidate_id, "rejected")
            store.record_action(action_id=f"ACT-{uuid4().hex[:8]}", tenant_id=tenant, knowledge_id="",
                                candidate_id=candidate_id, action="reject", actor=actor, reason=reason)
            return {"candidate_id": candidate_id, "status": "rejected"}
        if decision == "later":
            store.set_candidate_status(candidate_id, "observed", last_error=f"later:{_now()}")
            store.record_action(action_id=f"ACT-{uuid4().hex[:8]}", tenant_id=tenant, knowledge_id="",
                                candidate_id=candidate_id, action="later", actor=actor, reason=reason)
            return {"candidate_id": candidate_id, "status": "observed", "cooldown": True}

        # approve / approve_modified：晋升门禁检查后落 active 知识。
        problems = promotion_checks(candidate)
        if problems:
            raise HTTPException(422, {"code": "PROMOTION_BLOCKED", "message": "晋升检查未通过", "problems": problems})
        content = candidate.get("content") or {}
        if decision == "approve_modified" and isinstance(body.get("content"), dict):
            content = body["content"]
        version = store.next_knowledge_version(candidate_id)
        knowledge_id = f"K-{candidate['kind']}-{version}-{uuid4().hex[:8]}"
        previous = store.knowledge_by_candidate(candidate_id)
        supersedes = str(previous[0]["knowledge_id"]) if previous and previous[0].get("status") == "active" else ""
        if supersedes:
            store.set_knowledge_status(supersedes, "deprecated")
        store.create_knowledge(
            knowledge_id=knowledge_id, tenant_id=tenant, candidate_id=candidate_id,
            version=version, kind=str(candidate["kind"]), content=content,
            applicability=candidate.get("applicability") or {}, scope=str(candidate.get("scope") or "tenant"),
            approved_by=actor, supersedes=supersedes,
        )
        store.set_candidate_status(candidate_id, "active")
        store.record_action(action_id=f"ACT-{uuid4().hex[:8]}", tenant_id=tenant, knowledge_id=knowledge_id,
                            candidate_id=candidate_id, action=decision, actor=actor, reason=reason)
        return {"candidate_id": candidate_id, "knowledge_id": knowledge_id, "version": version, "status": "active"}

    @router.get("/evolution/knowledge")
    async def list_knowledge(kind: str | None = None, status: str = "active", tenant_id: str | None = None,
                             limit: int = 200, x_yunpai_tenant_id: str | None = Header(None),
                             x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        return {"knowledge": store.list_knowledge(tenant_id=tenant, kind=kind, status=status, limit=limit)}

    @router.post("/evolution/knowledge/{knowledge_id}/{action}")
    async def knowledge_action(knowledge_id: str, action: str, body: dict[str, Any] = Body(default={}),
                               x_actor_user: str | None = Header(None)):
        item = store.get_knowledge(knowledge_id)
        if item is None:
            raise HTTPException(404, {"code": "KNOWLEDGE_NOT_FOUND", "message": "知识不存在"})
        actor = _actor_from_headers({"x-actor-user": x_actor_user or ""})
        reason = str(body.get("reason") or "")
        if action in {"suspend", "resume", "deprecate"}:
            store.set_knowledge_status(knowledge_id, action)
            store.record_action(action_id=f"ACT-{uuid4().hex[:8]}", tenant_id=item["tenant_id"],
                                knowledge_id=knowledge_id, candidate_id=item["candidate_id"],
                                action=action, actor=actor, reason=reason)
            return {"knowledge_id": knowledge_id, "status": action}
        if action == "rollback":
            # 回滚 = 废弃当前版本，恢复其 supersedes 指向的上一版本。
            store.set_knowledge_status(knowledge_id, "deprecated")
            if item.get("supersedes"):
                store.set_knowledge_status(str(item["supersedes"]), "active")
            store.record_action(action_id=f"ACT-{uuid4().hex[:8]}", tenant_id=item["tenant_id"],
                                knowledge_id=knowledge_id, candidate_id=item["candidate_id"],
                                action="rollback", actor=actor, reason=reason,
                                payload={"restored": item.get("supersedes") or ""})
            return {"knowledge_id": knowledge_id, "status": "deprecated", "restored": item.get("supersedes") or ""}
        raise HTTPException(422, {"code": "INVALID_ACTION", "message": "action 必须为 suspend/resume/deprecate/rollback"})

    @router.get("/evolution/metrics")
    async def metrics(tenant_id: str | None = None, x_yunpai_tenant_id: str | None = Header(None),
                      x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        return {"metrics": store.metrics(tenant_id=tenant)}

    @router.get("/evolution/redline/check")
    async def redline_check():
        monitor = RedlineMonitor()
        return monitor.check()

    @router.get("/shortcuts")
    async def list_shortcuts(tenant_id: str | None = None, message: str | None = None,
                             x_yunpai_tenant_id: str | None = Header(None),
                             x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        context = build_context({"message": message or ""}, tenant_id=tenant)
        items = retrieve_shortcuts(store, tenant_id=tenant, context=context) if message else store.list_shortcuts(tenant_id=tenant)
        return {"shortcuts": items}

    @router.post("/shortcuts/{shortcut_id}/use")
    async def use_shortcut(shortcut_id: str):
        shortcut = store.get_shortcut(shortcut_id)
        if shortcut is None:
            raise HTTPException(404, {"code": "SHORTCUT_NOT_FOUND", "message": "快捷操作不存在"})
        store.record_shortcut_use(shortcut_id)
        return {"shortcut_id": shortcut_id, "steps": shortcut.get("steps") or []}

    @router.post("/shortcuts/{shortcut_id}/disable")
    async def disable_shortcut(shortcut_id: str):
        store.disable_shortcut(shortcut_id)
        return {"shortcut_id": shortcut_id, "status": "disabled"}

    # ------------------------------------------------------------------
    # 产品理解（Profile）
    # ------------------------------------------------------------------

    @router.get("/evolution/profiles")
    async def list_profiles(tenant_id: str | None = None, limit: int = 200,
                            x_yunpai_tenant_id: str | None = Header(None),
                            x_tenant_id: str | None = Header(None)):
        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        return {"profiles": store.list_profiles(tenant_id=tenant, limit=limit)}

    @router.get("/evolution/profiles/{product_code}")
    async def get_profile(product_code: str, tenant_id: str | None = None,
                          name: str = "", category: str | None = None,
                          x_yunpai_tenant_id: str | None = Header(None),
                          x_tenant_id: str | None = Header(None)):
        from .profile_query import build_product_chain

        tenant = _tenant(tenant_id, {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        return build_product_chain(store, tenant_id=tenant, product_code=product_code,
                                   name=name, category=category)

    @router.post("/evolution/profiles/extract")
    async def extract_profile(body: dict[str, Any] = Body(...),
                              x_yunpai_tenant_id: str | None = Header(None),
                              x_tenant_id: str | None = Header(None)):
        """触发一次理解管线（27B/35B 增强 + 确定性回退）。kind ∈ {bom, sop, drawing}。"""
        import base64

        tenant = _tenant(body.get("tenant_id"), {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        kind = str(body.get("kind") or "").lower()
        filename = str(body.get("filename") or "document.bin")
        content_b64 = str(body.get("content_b64") or "")
        if not content_b64:
            raise HTTPException(422, {"code": "MISSING_CONTENT", "message": "content_b64 不能为空"})
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except ValueError:
            raise HTTPException(422, {"code": "INVALID_BASE64", "message": "content_b64 不是合法 base64"})
        if kind == "bom":
            from .pipelines.bom_pipeline import ingest_bom_profiles_llm
            return {"summary": await ingest_bom_profiles_llm(store, llm, tenant_id=tenant, raw=raw, filename=filename, document_ref=filename)}
        if kind == "sop":
            from .pipelines.sop_pipeline import parse_sop_pdf, ingest_sop_profiles_llm
            import fitz
            product_code = str(body.get("product_code") or "")
            if not product_code:
                raise HTTPException(422, {"code": "MISSING_PRODUCT_CODE", "message": "SOP 理解需要 product_code"})
            doc = fitz.open(stream=raw, filetype="pdf")
            try:
                pages = [page.get_text() for page in doc]
            finally:
                doc.close()
            stations = parse_sop_pdf(pages)
            return {"summary": await ingest_sop_profiles_llm(store, llm, tenant_id=tenant, product_code=product_code, stations=stations, document_ref=filename)}
        if kind == "drawing":
            from .pipelines.drawing_pipeline import ingest_pdf_drawing_llm
            product_code = str(body.get("product_code") or "")
            if not product_code:
                raise HTTPException(422, {"code": "MISSING_PRODUCT_CODE", "message": "图纸理解需要 product_code"})
            return {"summary": await ingest_pdf_drawing_llm(store, llm, tenant_id=tenant, product_code=product_code, raw=raw, filename=filename)}
        raise HTTPException(422, {"code": "UNSUPPORTED_KIND", "message": "kind 必须为 bom/sop/drawing"})

    @router.post("/evolution/demo/observe")
    async def demo_observe(body: dict[str, Any] = Body(...),
                           x_yunpai_tenant_id: str | None = Header(None),
                           x_tenant_id: str | None = Header(None)):
        """自进化演示：构造合成业务 run 并 observe_run，触发经验沉淀四信号。

        body: {operation: order_full|bom_sop|scheduling, repeat: 1..10,
               errors?: [...], approvals?: [...]}
        """
        from ..models import new_state
        from .signals import observe_run

        tenant = _tenant(body.get("tenant_id"), {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        operation = str(body.get("operation") or "order_full")
        repeat = max(1, min(10, int(body.get("repeat") or 1)))
        presets = {
            "order_full": ["ingest_document", "run_bom_sop_workflow", "run_m3_procurement_requirements", "solve_scheduling"],
            "bom_sop": ["ingest_document", "run_bom_sop_workflow"],
            "scheduling": ["run_bom_sop_workflow", "solve_scheduling"],
        }
        tools = presets.get(operation, presets["order_full"])
        for i in range(repeat):
            state = new_state({"message": f"演示 {operation}"}, tenant_id=tenant)
            state["task_id"] = f"task-demo-{operation}-{i}"
            state["steps"] = [{"id": f"s{j}", "module": "m0", "tool": t, "status": "completed"}
                              for j, t in enumerate(tools)]
            state["errors"] = body.get("errors") or []
            state["approvals"] = body.get("approvals") or []
            observe_run(store, state)
        return {
            "operation": operation, "repeat": repeat, "tools": tools,
            "candidates": store.list_candidates(tenant_id=tenant, limit=100),
            "pending": store.list_pending(tenant_id=tenant, limit=50),
            "knowledge": store.list_knowledge(tenant_id=tenant, status="active", limit=50),
        }

    @router.post("/evolution/demo/seed")
    async def demo_seed(body: dict[str, Any] = Body(default={}),
                        x_yunpai_tenant_id: str | None = Header(None),
                        x_tenant_id: str | None = Header(None)):
        """一键喂样例资料：读取 demo-产品资料/ 并入库（BOM/SOP/图纸）。"""
        import base64
        from pathlib import Path

        tenant = _tenant(body.get("tenant_id"), {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        demo_dir = Path(os.getenv("YUNPAI_DEMO_DIR", Path(__file__).resolve().parents[3] / "demo-产品资料"))
        summary: dict[str, Any] = {}

        bom_path = demo_dir / "BOM-中性HDTV2.0光纤线.xlsx"
        if bom_path.exists():
            from .pipelines.bom_pipeline import ingest_bom_profiles_llm
            summary["bom"] = await ingest_bom_profiles_llm(
                store, llm, tenant_id=tenant, raw=bom_path.read_bytes(),
                filename=bom_path.name, document_ref=bom_path.name)

        sop_path = demo_dir / "SOP-80806-129.pdf"
        if sop_path.exists():
            import fitz
            from .pipelines.sop_pipeline import parse_sop_pdf, ingest_sop_profiles_llm
            doc = fitz.open(sop_path)
            try:
                pages = [page.get_text() for page in doc]
            finally:
                doc.close()
            stations = parse_sop_pdf(pages)
            summary["sop"] = await ingest_sop_profiles_llm(
                store, llm, tenant_id=tenant, product_code="W-H410",
                stations=stations, document_ref=sop_path.name)

        drawing_path = demo_dir / "工程图-HDMI8K光纤线.pdf"
        if drawing_path.exists():
            from .pipelines.drawing_pipeline import ingest_pdf_drawing_llm
            summary["drawing"] = await ingest_pdf_drawing_llm(
                store, llm, tenant_id=tenant, product_code="W-H909",
                raw=drawing_path.read_bytes(), filename=drawing_path.name)

        return {"seeded": bool(summary), "summary": summary,
                "profiles": store.list_profiles(tenant_id=tenant, limit=200)}

    @router.post("/evolution/chat")
    async def chat(body: dict[str, Any] = Body(...),
                   x_yunpai_tenant_id: str | None = Header(None),
                   x_tenant_id: str | None = Header(None)):
        """对话入口：产品感知——消息里带型号时，把产品知识档案注入上下文再回答。"""
        import re

        tenant = _tenant(body.get("tenant_id"), {"x-yunpai-tenant-id": x_yunpai_tenant_id or "", "x-tenant-id": x_tenant_id or ""})
        message = str(body.get("message") or "").strip()
        if not message:
            raise HTTPException(422, {"code": "EMPTY_MESSAGE", "message": "message 不能为空"})

        # 提取产品型号并注入产品知识
        product_code = ""
        context_text = ""
        codes = re.findall(r"\b[A-Z]{1,4}-[A-Z0-9]{2,}\b", message)
        if codes:
            from .profile_query import build_product_chain
            product_code = codes[0]
            chain = build_product_chain(store, tenant_id=tenant, product_code=product_code, name=message)
            context_text = json.dumps(chain, ensure_ascii=False, default=str)[:3000]

        system = (
            "你是云湃制造业务助手。用中文简洁、准确地回答用户。"
            "如果能从下面提供的产品知识档案找到依据，就依据它回答；"
            "档案没有的就老实说不知道，绝不编造。"
        )
        if context_text:
            system += f"\n\n产品知识档案（{product_code}）：\n{context_text}"

        if llm is not None:
            result = await llm.chat(system=system, user=message, max_tokens=600)
            if result.get("ok"):
                return {"reply": result["reply"], "product_code": product_code,
                        "has_context": bool(context_text), "latency_s": result.get("latency_s")}
            return {"reply": f"（模型不可用：{result.get('error')}）", "product_code": product_code,
                    "has_context": bool(context_text), "model_error": result.get("error")}
        return {"reply": "（未接入模型）", "product_code": product_code, "has_context": bool(context_text)}

    return router

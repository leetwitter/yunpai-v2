"""FastAPI 应用（书二 §8）——对外契约与旧版一致（legacy api.py 端点集核心子集）。

已实现：/health /tools /skills /runs /runs/stream /runs/{id} /runs/{id}/resume
        /runs/{id}/resume/stream /runs/upload /runs/upload/batch + evolution 路由。
后续批次（V2-M3 注册期）：/m0/readback/{batch_id}、identity/guidance 端点组。

执行模型：图编译期绑定**同步** SqliteSaver；端点经线程池跑 graph.invoke/stream
（跨事件循环稳定，见 checkpointer.py 注记）。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from starlette.concurrency import run_in_threadpool

from ..checkpointer import default_checkpointer
from ..config import OrchestratorConfig
from ..graph import GraphDeps, build_graph, compute_recursion_limit, default_deps
from ..models import summarize
from ..registry import build_runtime_registry
from ..repository import SQLiteRunRepository
from ..skills import build_default_skill_registry
from ..state import new_state_v2, public_state


def create_app(*, repository: Any = None, registry: Any = None, identity_store: Any = None,
               evolution: Any = None, checkpointer: Any = None, config: Any = None):
    cfg: OrchestratorConfig = config or OrchestratorConfig()
    if repository is None:
        db_path = Path(cfg.storage.run_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        repository = SQLiteRunRepository(db_path)
    reg = registry or build_runtime_registry()
    skills = build_default_skill_registry()
    skills.validate_tools(reg.specs)

    evolution_store = None
    if evolution is not None or cfg.evolution_enabled:
        try:
            from ..evolution.repository import EvolutionRepository
            evolution_store = evolution or EvolutionRepository(cfg.storage.evolution_db)
        except Exception:
            evolution_store = evolution

    deps: GraphDeps = default_deps(cfg, registry=reg, skills=skills,
                                   evolution=evolution_store, repository=repository)

    class _LoopRunner:
        """专属后台事件循环：图（含 AsyncSqliteSaver）全程活在同一循环。"""

        def __init__(self) -> None:
            self.loop: asyncio.AbstractEventLoop | None = None
            self._ready = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True, name="yunpai-graph-loop")
            self._thread.start()
            self._ready.wait()

        def _run(self) -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._ready.set()
            self.loop.run_forever()

        def run(self, coro_fn, *args, **kwargs):
            fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), self.loop)
            return fut.result()

        def stream(self, coro_fn, *args, **kwargs):
            q: queue.Queue = queue.Queue()

            async def _pump():
                try:
                    async for chunk in coro_fn(*args, **kwargs):
                        q.put(("item", chunk))
                    q.put(("end", None))
                except Exception as exc:  # noqa: BLE001
                    q.put(("err", exc))

            asyncio.run_coroutine_threadsafe(_pump(), self.loop)
            return iter(q.get, None) and (q.get for _ in iter(int, 1)) and _QueueIter(q)

    class _QueueIter:
        def __init__(self, q: queue.Queue):
            self._q = q

        def __iter__(self):
            return self

        def __next__(self):
            kind, value = self._q.get()
            if kind == "end":
                raise StopIteration
            if kind == "err":
                raise value
            return value

    runner = _LoopRunner()
    graph_holder: dict[str, Any] = {}

    async def _build_graph_async():
        # AsyncSqliteSaver 必须在运行中的事件循环内构造（绑定 runner 循环）
        saver = checkpointer if checkpointer is not None else default_checkpointer(cfg.storage.run_db)
        return build_graph(deps, checkpointer=saver)

    def _graph():
        if "graph" not in graph_holder:
            graph_holder["graph"] = runner.run(_build_graph_async)
        return graph_holder["graph"]

    async def _invoke(payload: Any, config: dict[str, Any]) -> dict[str, Any]:
        return await run_in_threadpool(runner.run, _graph().ainvoke, payload, config)

    async def _astream_iter(payload: Any, config: dict[str, Any]):
        return await run_in_threadpool(
            runner.stream, _graph().astream, payload, config, stream_mode=["updates"])

    app = FastAPI(title="Yunpai Orchestrator v2", version="0.2.0")
    try:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    except Exception:  # pragma: no cover
        pass

    # ── 进化路由（候选决策/知识操作/红线/快捷操作/demo） ──────────
    try:
        from ..evolution.api import create_evolution_router
        from ..evolution.llm_tasks import EvolutionLLM
        if evolution_store is not None:
            app.include_router(create_evolution_router(evolution_store, EvolutionLLM()))
    except Exception:
        pass

    # ── 工具函数 ─────────────────────────────────────────────────
    def _principal(request: Request) -> dict[str, Any]:
        return {
            "user": request.headers.get("X-Actor-User", ""),
            "role": request.headers.get("X-Actor-Role", ""),
            "roles": [r.strip() for r in (request.headers.get("X-Actor-Roles", "")).split(",") if r.strip()],
            "tenant_id": request.headers.get("X-Tenant-ID", ""),
        }

    def _thread_config(state: dict[str, Any], plan_len: int = 9) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": str(state.get("thread_id") or state.get("run_id"))},
            "recursion_limit": compute_recursion_limit(max(plan_len, 1)),
        }

    def _merged_response(result: dict[str, Any]) -> dict[str, Any]:
        """优先用 RunRepository 里的 Gate 挂起镜像（含 pending_gate）。"""
        run_id = str(result.get("run_id") or "")
        saved = repository.get(run_id) if run_id else None
        if isinstance(saved, dict) and saved.get("pending_gate"):
            return public_state(saved)  # type: ignore[arg-type]
        return public_state(result)  # type: ignore[arg-type]

    async def _aiter_sync(iterator: Any):
        """同步事件流 → 异步生成器（每步 next 在线程池执行）。"""
        def _next() -> tuple[str, Any]:
            try:
                return ("item", next(iterator))
            except StopIteration:
                return ("end", None)
            except Exception as exc:  # noqa: BLE001
                return ("err", exc)

        while True:
            kind, value = await asyncio.to_thread(_next)
            if kind == "end":
                return
            if kind == "err":
                raise value
            yield value

    # ── 基础端点 ─────────────────────────────────────────────────
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok", "version": "0.2.0",
            "graph_nodes": ["orchestrator_plan", "orchestrator_dispatch", "worker_execute",
                            "reviewer_check", "chat_answer", "finalize", "evolution_observe"],
            "registry": {"specs": len(reg.specs), "handlers": len(reg.handlers)},
            "dbs": {"runs": Path(cfg.storage.run_db).exists(),
                    "evolution": Path(cfg.storage.evolution_db).exists()},
        }

    @app.get("/tools")
    async def tools() -> list[dict[str, Any]]:
        return reg.catalog()

    @app.get("/skills")
    async def skills_catalog() -> list[dict[str, Any]]:
        return skills.catalog()

    # ── run 创建/查询 ────────────────────────────────────────────
    @app.post("/runs")
    async def create_run(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        principal = _principal(request)
        if principal.get("tenant_id"):
            body.setdefault("tenant_id", principal["tenant_id"])
        body["principal"] = principal
        state = new_state_v2(body, tenant_id=str(body.get("tenant_id") or "default"))
        result = await _invoke(state, _thread_config(state))
        return _merged_response(dict(result))

    @app.get("/runs")
    async def list_runs(tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [public_state(s) for s in repository.list(tenant_id=tenant_id, limit=limit)]  # type: ignore[arg-type]

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        state = repository.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return public_state(state)  # type: ignore[arg-type]

    # ── 流式（NDJSON 事件名沿旧契约） ────────────────────────────
    async def _stream_events(source: Any, initial_state: dict[str, Any]):
        def nd(event_type: str, **payload: Any) -> str:
            return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + chr(10)

        yield nd("run_start", state=public_state(initial_state))  # type: ignore[arg-type]
        merged: dict[str, Any] = dict(initial_state)
        interrupted = False
        failed = False
        try:
            async for payload in source:
                if isinstance(payload, tuple):
                    payload = payload[-1]
                if not isinstance(payload, dict):
                    continue
                if "__interrupt__" in payload:
                    for item in payload["__interrupt__"]:
                        info = getattr(item, "value", item) or {}
                        gate = info.get("gate") if isinstance(info, dict) else None
                        yield nd("gate_opened", gate=gate or info)
                        yield nd("state_snapshot", state=public_state(merged))
                        interrupted = True
                    continue
                # updates 模式载荷为 {节点名: 更新}——解包为节点更新字典
                node_keys = [k for k in payload if not str(k).startswith("__")]
                if len(node_keys) == 1 and isinstance(payload[node_keys[0]], dict):
                    payload = payload[node_keys[0]]
                merged = {**merged, **payload}
                if "route_decision" in payload:
                    reason = (payload.get("route_decision") or {}).get("reason", "")
                    yield nd("assistant_delta", content=reason or "已生成执行计划")
                step_rec = payload.get("current_step") if isinstance(payload.get("current_step"), dict) else None
                if step_rec and step_rec.get("status") == "running":
                    yield nd("step_start", step=step_rec)
                if step_rec and step_rec.get("status") in ("completed", "failed", "blocked", "skipped"):
                    tool = str(step_rec.get("tool") or "")
                    result = (merged.get("outputs") or {}).get(tool) or {}
                    yield nd("step_result", step=step_rec, result=summarize(result))
                if payload.get("errors"):
                    last = payload["errors"][-1]
                    yield nd("run_error", **last)
                    failed = True
                yield nd("state_snapshot", state=public_state(merged))
        except Exception as exc:  # noqa: BLE001
            yield nd("run_error", code="STREAM_ERROR", message=str(exc)[:500])
            failed = True
        final_status = merged.get("status")
        if interrupted and final_status != "completed":
            merged = {**merged, "status": "waiting_human"}
        if failed and final_status not in ("failed", "waiting_human"):
            merged = {**merged, "status": "failed"}
        yield nd("run_done", state=public_state(merged))  # type: ignore[arg-type]

    @app.post("/runs/stream")
    async def create_run_stream(request: Request, body: dict[str, Any]):
        principal = _principal(request)
        if principal.get("tenant_id"):
            body.setdefault("tenant_id", principal["tenant_id"])
        body["principal"] = principal
        state = new_state_v2(body, tenant_id=str(body.get("tenant_id") or "default"))
        config = _thread_config(state)
        iterator = await _astream_iter(state, config)
        return StreamingResponse(_stream_events(_aiter_sync(iterator), dict(state)),
                                 media_type="application/x-ndjson")

    # ── resume（Gate 决策） ──────────────────────────────────────
    def _resume_body(request: Request, run_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if cfg.require_trusted_principal:
            actor = request.headers.get("X-Actor-User", "")
            roles = [r.strip() for r in request.headers.get("X-Actor-Roles", "").split(",") if r.strip()]
            if not actor and not roles:
                raise HTTPException(status_code=403, detail="缺少受信主体头（X-Actor-User/X-Actor-Roles）")
        state = repository.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        if not isinstance(state, dict) or not state.get("pending_gate"):
            raise HTTPException(status_code=409, detail=f"run {run_id} 无待决 Gate")
        decision = dict(body or {})
        decision.setdefault("actor", request.headers.get("X-Actor-User", "human"))
        if not decision.get("roles"):
            roles = [r.strip() for r in request.headers.get("X-Actor-Roles", "").split(",") if r.strip()]
            if roles:
                decision["roles"] = roles
        return state, decision  # type: ignore[return-value]

    @app.post("/runs/{run_id}/resume")
    async def resume_run(request: Request, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        state, decision = _resume_body(request, run_id, body)
        config = _thread_config(state, len(state.get("plan") or []) + 2)
        result = await _invoke(Command(resume=decision), config)
        return _merged_response(dict(result))

    @app.post("/runs/{run_id}/resume/stream")
    async def resume_run_stream(request: Request, run_id: str, body: dict[str, Any]):
        state, decision = _resume_body(request, run_id, body)
        config = _thread_config(state, len(state.get("plan") or []) + 2)
        iterator = await _astream_iter(Command(resume=decision), config)
        return StreamingResponse(_stream_events(_aiter_sync(iterator), dict(state)),
                                 media_type="application/x-ndjson")

    # ── 上传（契约：workflow 必须为 m1_m5_document_to_plan；附件须带 kind） ──
    def _attachment(filename: str, content_type: str, raw: bytes, kind: str) -> dict[str, Any]:
        import base64
        return {
            "filename": filename,
            "content_type": content_type or "application/octet-stream",
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "kind": kind or "order",
        }

    @app.post("/runs/upload")
    async def upload_run(
        request: Request,
        files: list[UploadFile] = File(...),
        workflow: str = Form("m1_m5_document_to_plan"),
        kind: str = Form("order"),
        message: str = Form(""),
    ) -> dict[str, Any]:
        if workflow and workflow != "m1_m5_document_to_plan":
            raise HTTPException(
                status_code=422,
                detail='原始文件上传必须发 workflow:"m1_m5_document_to_plan"（发错工作流会导致 M1 415）')
        principal = _principal(request)
        attachments = []
        for f in files[:2000]:
            raw = await f.read()
            if len(raw) > 40 * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"文件超限 40MiB：{f.filename}")
            attachments.append(_attachment(str(f.filename), f.content_type or "", raw, kind))
        body = {
            "message": message or "上传原始业务文件，执行订单到计划全链",
            "workflow": workflow,
            "attachments": attachments,
            "principal": principal,
            "tenant_id": principal.get("tenant_id") or "default",
        }
        state = new_state_v2(body, tenant_id=str(body["tenant_id"]))
        result = await _invoke(state, _thread_config(state))
        return _merged_response(dict(result))

    @app.post("/runs/upload/batch")
    async def upload_batch(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise HTTPException(status_code=422, detail="files 不能为空")
        if len(files) > 2000:
            raise HTTPException(status_code=422, detail="单批最多 2000 文件")
        attachments = []
        for item in files:
            if not isinstance(item, dict) or not item.get("content_b64"):
                raise HTTPException(status_code=422, detail="每个文件必须带 filename/content_type/content_b64")
            attachments.append({
                "filename": str(item.get("filename") or "upload.bin"),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "content_b64": str(item["content_b64"]),
                "kind": str(item.get("kind") or "master_data"),
            })
        principal = _principal(request)
        run_body = {
            "message": str(body.get("message") or "批量上传业务资料"),
            "workflow": "m1_m5_document_to_plan",
            "attachments": attachments,
            "principal": principal,
            "tenant_id": principal.get("tenant_id") or body.get("tenant_id") or "default",
        }
        state = new_state_v2(run_body, tenant_id=str(run_body["tenant_id"]))
        result = await _invoke(state, _thread_config(state))
        return _merged_response(dict(result))

    # ── 红线周期任务（进化缺口③：启动即检 + 周期复查） ───────────
    @app.on_event("startup")
    async def _redline_loop() -> None:
        async def _loop() -> None:
            while True:
                try:
                    from ..evolution import redline
                    check = getattr(redline, "check", None) or getattr(redline, "verify_redline", None)
                    if callable(check):
                        report = check()
                        if isinstance(report, dict) and report.get("violations"):
                            print("[redline] 违规：", json.dumps(report["violations"], ensure_ascii=False)[:500])
                except Exception:
                    pass
                await asyncio.sleep(max(1, cfg.redline_interval_hours) * 3600)

        app.state.redline_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _cancel_redline() -> None:
        task = getattr(app.state, "redline_task", None)
        if task is not None:
            task.cancel()

    return app

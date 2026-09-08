from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, TypedDict
from uuid import uuid4

Status = Literal["queued", "running", "waiting_human", "completed", "failed"]
StepStatus = Literal["pending", "running", "completed", "blocked", "failed", "skipped", "superseded"]


class StepRecord(TypedDict, total=False):
    id: str
    module: str
    tool: str
    status: StepStatus
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    evidence: list[dict[str, Any]]
    error: dict[str, Any]
    started_at: str
    finished_at: str


class RunState(TypedDict, total=False):
    run_id: str
    task_id: str
    tenant_id: str
    request: dict[str, Any]
    messages: list[dict[str, str]]
    route: Literal["workflow", "free", "chat"]
    workflow_id: str
    workflow_version: str
    plan: list[dict[str, Any]]
    next_step_index: int
    current_step: str
    current_result: dict[str, Any]
    status: Status
    outputs: dict[str, Any]
    evidence: list[dict[str, Any]]
    steps: list[StepRecord]
    pending_gate: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    authorized_steps: list[str]
    errors: list[dict[str, Any]]
    response: str
    trace: list[dict[str, Any]]
    intent: dict[str, Any]
    route_decision: dict[str, Any]
    model: dict[str, Any]


def new_state(request: dict[str, Any], *, tenant_id: str = "default") -> RunState:
    now = datetime.now(timezone.utc).isoformat()
    run_id, task_id = f"run-{uuid4().hex}", f"task-{uuid4().hex}"
    return RunState(
        run_id=run_id, task_id=task_id, tenant_id=tenant_id,
        request=request, messages=[], route="chat", workflow_id="", workflow_version="",
        plan=[], next_step_index=0,
        current_step="", current_result={},
        status="queued", outputs={}, evidence=[], steps=[], pending_gate=None,
        approvals=[], authorized_steps=[], errors=[], response="", trace=[{"event": "run.created", "at": now}],
        intent={}, route_decision={}, model={},
    )


def summarize(value: Any, *, limit: int = 12) -> Any:
    """日志摘要，避免把原文件/大数组写进 trace。"""
    if isinstance(value, dict):
        preferred = {"id", "code", "status", "count", "quantity", "shortage", "plan_version", "confidence"}
        return {k: summarize(v) for k, v in value.items() if k in preferred or not isinstance(v, (dict, list))}
    if isinstance(value, list):
        return {"count": len(value), "sample": [summarize(v) for v in value[:3]]}
    if isinstance(value, str) and len(value) > 256:
        return value[:253] + "..."
    return value

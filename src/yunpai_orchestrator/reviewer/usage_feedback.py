"""进化使用反馈（书二 §6.3）——补进化缺口①：record_usage 接上调用方。

confirmed：Gate approve / 执行成功且注入知识被消费；
contradicted：人工改判（reject/approve_modified 后重做）——反例 ≥2 次由
evolution.repository.record_usage 自动 suspend（已内建，此前无调用方）。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4


def _record(repo: Any, state: dict[str, Any], knowledge_id: str, outcome: str) -> None:
    try:
        repo.record_usage(
            usage_id=f"usage-{uuid4().hex[:12]}",
            tenant_id=str(state.get("tenant_id") or "default"),
            knowledge_id=str(knowledge_id),
            run_id=str(state.get("run_id") or ""),
            task_id=str(state.get("task_id") or ""),
            outcome=outcome,
        )
    except Exception:
        pass  # 反馈链路永不破坏主流程


def on_gate_decision(repo: Any, state: dict[str, Any], gate: dict[str, Any],
                     decision: dict[str, Any]) -> None:
    normalized = str(decision.get("decision") or "").lower()
    outcome = None
    if normalized in {"approve", "allow", "continue"}:
        outcome = "confirmed"
    elif normalized in {"reject", "supplier_by_material"} or decision.get("modified"):
        outcome = "contradicted"
    if outcome is None:
        return
    for item in state.get("knowledge_context") or []:
        kid = str(item.get("knowledge_id") or item.get("id") or "")
        if kid:
            _record(repo, state, kid, outcome)


def on_finalize(repo: Any, state: dict[str, Any], success: bool) -> None:
    outcome = "confirmed" if success else "contradicted"
    for item in state.get("knowledge_context") or []:
        kid = str(item.get("knowledge_id") or item.get("id") or "")
        if kid:
            _record(repo, state, kid, outcome)

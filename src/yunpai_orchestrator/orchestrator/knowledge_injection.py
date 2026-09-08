"""知识注入消费（书二 §4.4）——补进化缺口②：注入的活跃知识被统筹真实消费。

retrieve_active_knowledge 为确定性结构化匹配（〔迁〕evolution/injection.py:40），
本模块只做两件事：取 top-k 挂到 state.knowledge_context；供 Router 组装 prompt 提示。
"""
from __future__ import annotations

from typing import Any


def attach_knowledge(state: dict[str, Any], repo: Any, top_k: int = 5) -> list[dict[str, Any]]:
    """读取活跃知识挂到 state（repo 为 None 或未启用时返回空，不破坏主流程）。"""
    if repo is None:
        return []
    try:
        from ..evolution.injection import retrieve_active_knowledge
        items = retrieve_active_knowledge(
            repo,
            tenant_id=str(state.get("tenant_id") or "default"),
            context={"message": str(state.get("message") or "")},
            limit=top_k,
        )
        return [dict(item) for item in items]
    except Exception:
        return []


def hints_for_prompt(knowledge: list[dict[str, Any]]) -> list[str]:
    hints = []
    for item in knowledge:
        title = str(item.get("title") or item.get("kind") or "knowledge")
        body = str(item.get("content") or item.get("payload") or "")[:160]
        hints.append(f"[{title}] {body}")
    return hints

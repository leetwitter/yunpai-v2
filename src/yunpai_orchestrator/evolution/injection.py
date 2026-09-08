from __future__ import annotations

from typing import Any

from .repository import EvolutionRepository


def _match_score(applicability: dict[str, Any], context: dict[str, Any]) -> int:
    """确定性结构化匹配：applicability 中每个键与上下文值相等即 +1。"""
    if not applicability:
        return 0
    score = 0
    for key, value in applicability.items():
        ctx_value = context.get(key)
        if ctx_value is None:
            continue
        if isinstance(value, list):
            if ctx_value in value:
                score += 1
        elif str(value) == str(ctx_value):
            score += 1
    return score


def build_context(request: dict[str, Any], *, intent: str = "", tenant_id: str = "default") -> dict[str, Any]:
    """从请求提取可匹配的知识适用条件。"""
    attachments = list(request.get("attachments") or []) + list(request.get("documents") or [])
    kinds = [str(item.get("kind") or "") for item in attachments if isinstance(item, dict)]
    entity_types = [str(item.get("entity_type") or "") for item in attachments if isinstance(item, dict)]
    return {
        "tenant_id": tenant_id,
        "intent": intent,
        "tool": str(request.get("tool") or ""),
        "kind": kinds[0] if kinds else "",
        "entity_type": entity_types[0] if entity_types else str(request.get("entity_type") or ""),
        "message": str(request.get("message") or request.get("task") or ""),
    }


def retrieve_active_knowledge(repo: EvolutionRepository, *, tenant_id: str,
                              context: dict[str, Any] | None = None,
                              kind: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """检索 active 知识并按适用条件匹配度排序（确定性，不用向量）。"""
    context = context or {}
    items = repo.list_knowledge(tenant_id=tenant_id, kind=kind, status="active", limit=200)
    scored = [(item, _match_score(item.get("applicability") or {}, context)) for item in items]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top = [item for item, score in scored if score > 0][: max(1, min(limit, 20))]
    if not top:
        # 无命中时退回最新 1 条（仅作参考，不影响业务执行）。
        top = [item for item, _ in scored[:1]]
    return top


def retrieve_shortcuts(repo: EvolutionRepository, *, tenant_id: str,
                       context: dict[str, Any] | None = None, limit: int = 5) -> list[dict[str, Any]]:
    context = context or {}
    shortcuts = repo.list_shortcuts(tenant_id=tenant_id, status="active", limit=200)
    scored = [(item, _match_score(item.get("trigger") or {}, context)) for item in shortcuts]
    scored.sort(key=lambda pair: (pair[1], pair[0].get("use_count") or 0), reverse=True)
    return [item for item, score in scored if score > 0][: max(1, min(limit, 20))]

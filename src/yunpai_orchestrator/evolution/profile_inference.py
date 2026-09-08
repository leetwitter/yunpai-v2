"""新产品同类型推理（U4 延伸）：新品不全推倒重来，借用同类型既有产品的理解。

确定性核心：规格签名（接口/版本/壳材/特征）→ 匹配相似既有产品 → 产出「草稿档案」
（物料/工序作模板、几何作参考），标 inferred、低置信、须人工确认。
可选 LLM 增强（P7）用于新品类型判断，失败回退确定性。
"""
from __future__ import annotations

from typing import Any

from .identity_resolver import spec_signature
from .repository import EvolutionRepository


def _type_key(name: str, category: str | None, sig: dict[str, list[str]]) -> tuple[str, str] | None:
    if category:
        return ("category", category)
    if sig.get("interface"):
        return ("interface", sig["interface"][0])
    if sig.get("version"):
        return ("version", sig["version"][0])
    return None


def _family_prefix(code: str) -> str:
    """型号家族前缀启发式：保留「字母段 + 首个系列数字」。

    W-H410/W-H415/W-H420 → W-H4（HDTV2.0 光纤线系列）；W-H909 → W-H9（HDMI 8K）。
    仅作弱信号，规格签名仍是主信号。
    """
    import re
    if not code:
        return ""
    m = re.match(r"^(.*?[A-Za-z])(\d)", code)
    return (m.group(1) + m.group(2)) if m else code


def _similarity(sig: dict[str, list[str]], category: str | None, profile: dict[str, Any]) -> int:
    score = 0
    if category and str(profile.get("category") or "") == category:
        score += 3
    pname = str(profile.get("product_name") or "").upper()
    psig = spec_signature(profile.get("product_name") or "")
    for kw in sig.get("interface", []):
        if kw.upper() in pname:
            score += 2
    for kw in sig.get("shell", []):
        if kw.upper() in pname:
            score += 1
    for kw in sig.get("feature", []):
        if kw.upper() in pname:
            score += 1
    # 物料类别重叠也计入同类型
    p_cats = {str(m.get("category") or "") for m in profile.get("materials") or [] if m.get("category")}
    if category and category in p_cats:
        score += 2
    return score


def _template(items: list[dict[str, Any]], *, drop_code: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        copy = {k: v for k, v in item.items() if not (drop_code and k == "material_code")}
        copy["_inferred"] = True
        out.append(copy)
    return out


def infer_profile(repo: EvolutionRepository, *, tenant_id: str, product_code: str,
                  name: str = "", category: str | None = None,
                  sig: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """给未知 product_code 推断同类型草稿。返回 {inferred, confidence, draft, ...}。"""
    sig = sig or spec_signature(name or product_code)
    profiles = repo.list_profiles(tenant_id=tenant_id, limit=500)
    family = _family_prefix(product_code)
    scored: list[tuple[int, dict[str, Any]]] = []
    for profile in profiles:
        score = _similarity(sig, category, profile)
        # 同族前缀弱信号（仅当规格签名未命中时补充）
        if family and str(profile.get("product_code") or "").startswith(family):
            score += 5
        if score > 0:
            scored.append((score, profile))
    scored.sort(key=lambda pair: -pair[0])
    if not scored:
        return {"inferred": False, "reason": "无同类型既有产品可供推理",
                "signature": sig, "searched": len(profiles)}
    best_score, best = scored[0]
    return {
        "inferred": True,
        "confidence": 0.1,
        "similarity_score": best_score,
        "source_product_code": best.get("product_code") or "",
        "source_product_name": best.get("product_name") or "",
        "signature": sig,
        "draft": {
            "materials": _template(best.get("materials") or [], drop_code=True),
            "process": _template(best.get("process") or [], drop_code=False),
            "geometry": dict(best.get("geometry") or {}),
        },
        "note": "同类型推理草稿（低置信，须人工确认后落库）",
    }

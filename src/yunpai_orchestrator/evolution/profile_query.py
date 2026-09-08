from __future__ import annotations

from typing import Any

from .repository import EvolutionRepository


def build_product_chain(repo: EvolutionRepository, *, tenant_id: str, product_code: str,
                        name: str = "", category: str | None = None) -> dict[str, Any]:
    """产品全链视图（U3）：档案 + 字段级证据 + 缺口清单。档案页与注入层共用此实现。

    未知产品走同类型推理（U4 延伸）：返回 inferred 草稿，不全推倒重来。
    """
    profile = repo.get_profile(tenant_id=tenant_id, product_code=product_code)
    if profile is None:
        from .profile_inference import infer_profile
        inference = infer_profile(repo, tenant_id=tenant_id, product_code=product_code,
                                  name=name, category=category)
        return {
            "product_code": product_code, "exists": False,
            "inferred": inference.get("inferred", False),
            "inference": inference,
            "missing": ["bom", "sop", "drawing"],
        }
    evidence = repo.list_field_evidence(profile_id=profile["profile_id"], limit=1000)
    missing: list[str] = []
    if not profile.get("materials"):
        missing.append("bom")
    if not profile.get("process"):
        missing.append("sop")
    if not profile.get("geometry"):
        missing.append("drawing")
    return {
        "product_code": product_code,
        "exists": True,
        "profile_id": profile["profile_id"],
        "product_name": profile.get("product_name") or "",
        "category": profile.get("category") or "",
        "materials": profile.get("materials") or [],
        "process": profile.get("process") or [],
        "geometry": profile.get("geometry") or {},
        "facts": profile.get("facts") or {},
        "crosscheck": profile.get("crosscheck") or {},
        "confidence": profile.get("confidence") or 0.0,
        "version": profile.get("version") or 0,
        "evidence_count": len(evidence),
        "conflicts": [e for e in evidence if e.get("status") == "conflict"],
        "missing": missing,
    }

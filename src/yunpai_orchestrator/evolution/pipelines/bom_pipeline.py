from __future__ import annotations

from typing import Any

from ..merge_engine import merge_field
from ..repository import EvolutionRepository


def parse_bom_records(raw: bytes, filename: str) -> dict[str, Any]:
    """确定性 BOM 理解（复用 tabular_extract.extract_bom_full 的两段式抽取）。

    返回 {products: [{product_code, product_name, lines: [...]}], issues: []}。
    同一 sheet 多行同型号会被归并为同一个产品（extract_bom_full 会按型号出现次数
    产生重复记录，这里去重）。
    """
    from ...tabular_extract import extract_bom_full

    result = extract_bom_full(raw, filename)
    products: dict[str, dict[str, Any]] = {}
    for record in result.get("records") or []:
        code = str(record.get("product_code") or "")
        if not code or code in products:
            continue
        lines = [dict(line) for line in record.get("lines") or [] if isinstance(line, dict)]
        products[code] = {"product_code": code,
                          "product_name": str(record.get("product_name") or ""),
                          "lines": lines}
    return {"products": list(products.values()), "issues": []}


def ingest_bom_profiles(repo: EvolutionRepository, *, tenant_id: str, raw: bytes,
                        filename: str, document_ref: str = "") -> dict[str, Any]:
    """把 BOM 解析结果并入产品档案（确定性，无 LLM）。"""
    parsed = parse_bom_records(raw, filename)
    return _ingest_products(repo, parsed["products"], tenant_id=tenant_id, document_ref=document_ref or filename)


async def ingest_bom_profiles_llm(repo: EvolutionRepository, llm: Any, *, tenant_id: str,
                                  raw: bytes, filename: str, document_ref: str = "") -> dict[str, Any]:
    """BOM 理解 + 27B 分类/规格规范化（P1），再并入档案。LLM 失败回退确定性。"""
    parsed = parse_bom_records(raw, filename)
    for product in parsed["products"]:
        lines = product.get("lines") or []
        if lines and getattr(llm, "enabled", False):
            categorized = await llm.categorize_bom_lines(lines)
            for line, cat in zip(lines, categorized):
                line["category"] = str(cat.get("category") or "")
                if isinstance(cat.get("spec_normalized"), dict):
                    line["spec_normalized"] = cat["spec_normalized"]
        product["lines"] = lines
    return _ingest_products(repo, parsed["products"], tenant_id=tenant_id, document_ref=document_ref or filename)


def _ingest_products(repo: EvolutionRepository, products: list[dict[str, Any]], *,
                     tenant_id: str, document_ref: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"products": [], "actions": {}}
    for product in products:
        code = product["product_code"]
        if not code:
            continue
        profile_id = f"PU-{code}"
        existing = repo.get_profile(tenant_id=tenant_id, product_code=code)
        profile_id = str(existing["profile_id"]) if existing else profile_id
        actions = {"candidate_created": 0, "reinforced": 0, "conflict": 0}
        for index, line in enumerate(product["lines"]):
            key = str(line.get("material_code") or line.get("material_name") or f"line-{index}")
            outcome = merge_field(
                repo, tenant_id=tenant_id, profile_id=profile_id, field_path="materials",
                key=key, value=line, source_kind="bom_document",
                document_ref=document_ref,
                locator={"sheet": line.get("sheet_name") or product["product_name"], "index": index},
            )
            actions[outcome["action"]] += 1
        materials = _confirmed_values(repo, profile_id, "materials")
        repo.upsert_profile(
            profile_id=profile_id, tenant_id=tenant_id, product_code=code,
            product_name=product["product_name"], materials=materials,
            confidence=_avg_confidence(repo, profile_id, "materials"),
            version=int(existing["version"]) + 1 if existing else 1,
        )
        summary["products"].append({"product_code": code, "product_name": product["product_name"],
                                    "materials": len(materials), "actions": actions})
        summary["actions"] = _sum_actions(summary["actions"], actions)
    return summary


def _confirmed_values(repo: EvolutionRepository, profile_id: str, field_path: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for evidence in repo.list_field_evidence(profile_id=profile_id, field_path=field_path, limit=500):
        if evidence["status"] == "conflict":
            continue
        values.append(evidence["value"])
    return values


def _avg_confidence(repo: EvolutionRepository, profile_id: str, field_path: str) -> float:
    from ..merge_engine import confidence as _conf

    evidence = repo.list_field_evidence(profile_id=profile_id, field_path=field_path, limit=500)
    if not evidence:
        return 0.0
    ranks = {"low": 0.33, "medium": 0.66, "high": 1.0}
    total = sum(ranks.get(_conf(int(e.get("independent_count") or 1)), 0.33) for e in evidence)
    return round(total / len(evidence), 2)


def _sum_actions(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    for key, value in b.items():
        a[key] = a.get(key, 0) + value
    return a

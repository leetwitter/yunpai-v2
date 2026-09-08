from __future__ import annotations

from typing import Any
from uuid import uuid4

from .repository import EvolutionRepository


def normalize(value: Any) -> Any:
    """类型感知归一（用于同字段比较）：数值归一到 2 位小数，字符串去空白/全半角/大小写。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, str):
        text = value.strip().lower()
        text = text.replace("\u3000", " ").replace("\uff0c", ",").replace("\uff1a", ":")
        return " ".join(text.split())
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in sorted(value.items())}
    return value


def confidence(independent_count: int, *, has_human: bool = False) -> str:
    if has_human or independent_count >= 3:
        return "high"
    if independent_count >= 2:
        return "medium"
    return "low"


def merge_field(repo: EvolutionRepository, *, tenant_id: str, profile_id: str,
                field_path: str, key: str, value: Any, source_kind: str,
                document_ref: str, locator: dict[str, Any] | None = None) -> dict[str, Any]:
    """字段级合并（U4 核心）：同 key 同值→强化，同 key 异值→冲突，新 key→候选。

    返回 {action, evidence_id, confidence, independent_count}。
    action ∈ {candidate_created, reinforced, conflict}。
    """
    locator = dict(locator or {})
    locator["key"] = key
    existing = repo.list_field_evidence(profile_id=profile_id, field_path=field_path, limit=200)
    match = next(
        (e for e in existing if str(e["locator"].get("key") or "") == str(key)),
        None,
    )
    if match is None:
        evidence_id = f"FE-{uuid4().hex[:8]}"
        repo.add_field_evidence(
            evidence_id=evidence_id, tenant_id=tenant_id, profile_id=profile_id,
            field_path=field_path, value=value, source_kind=source_kind,
            document_ref=document_ref, locator=locator, status="candidate",
        )
        return {"action": "candidate_created", "evidence_id": evidence_id,
                "confidence": "low", "independent_count": 1}
    if normalize(match["value"]) == normalize(value):
        repo.bump_evidence_independence(str(match["id"]))
        fresh = repo.get_field_evidence(str(match["id"])) or match
        return {"action": "reinforced", "evidence_id": str(match["id"]),
                "confidence": confidence(int(fresh["independent_count"])),
                "independent_count": int(fresh["independent_count"])}
    evidence_id = f"FE-{uuid4().hex[:8]}"
    repo.add_field_evidence(
        evidence_id=evidence_id, tenant_id=tenant_id, profile_id=profile_id,
        field_path=field_path, value=value, source_kind=source_kind,
        document_ref=document_ref, locator=locator, status="conflict",
    )
    return {"action": "conflict", "evidence_id": evidence_id,
            "confidence": "low", "independent_count": 1,
            "existing_evidence_id": str(match["id"])}

from __future__ import annotations

from typing import Any


# 晋升门槛：普通候选 3 个独立任务；强证据（人工纠正）1 次即 propose。
INDEPENDENT_THRESHOLD = 3


def should_promote(candidate: dict[str, Any], *, strong: bool = False) -> tuple[bool, str]:
    """晋升检查（确定性）。返回 (是否提议, 原因)。

    已 active / proposed / rejected 的候选不再重复提议（rejected 需治理台人工
    重置；本阶段不自动复活）。strong 证据（human_correction 类）首现即提议。
    """
    status = str(candidate.get("status") or "observed")
    if status in {"active", "proposed", "suspended", "deprecated", "rejected"}:
        return False, f"status={status}"
    if strong:
        return True, "strong_evidence(human_correction)"
    independent = int(candidate.get("independent_count") or 0)
    if independent >= INDEPENDENT_THRESHOLD:
        return True, f"independent_count={independent}>={INDEPENDENT_THRESHOLD}"
    return False, f"independent_count={independent}<{INDEPENDENT_THRESHOLD}"


def promotion_checks(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """晋升前的确定性检查清单（冲突/范围/版本/敏感），返回问题列表（空=通过）。"""
    problems: list[dict[str, Any]] = []
    content = candidate.get("content") or {}
    applicability = candidate.get("applicability") or {}
    kind = str(candidate.get("kind") or "")

    # 敏感内容检查：身份证/手机号/密钥形态一律不放行。
    for value in _walk_strings(content):
        if _looks_sensitive(value):
            problems.append({"code": "SENSITIVE_CONTENT", "message": "候选正文疑似含敏感信息"})
            break

    # 版本检查：映射类候选引用的 entity_type 必须仍在 canonical schema 中。
    entity_type = str(applicability.get("entity_type") or "")
    if entity_type and kind in {"success_mapping", "human_correction", "field_meaning"}:
        try:
            from ..canonical_schema import known_entity_types
            if entity_type not in known_entity_types():
                problems.append({"code": "UNKNOWN_ENTITY_TYPE", "message": f"entity_type={entity_type} 不在 canonical schema"})
        except Exception:
            pass

    if not content:
        problems.append({"code": "EMPTY_CONTENT", "message": "候选正文为空"})
    return problems


def _looks_sensitive(value: str) -> bool:
    import re
    # 身份证（15/18 位，末位可 X）、手机号、API key 形态。
    if re.search(r"\d{15}|\d{17}[\dXx]", value):
        return True
    if re.search(r"\b1[3-9]\d{9}\b", value):
        return True
    if re.search(r"sk-[A-Za-z0-9]{16,}", value):
        return True
    return False


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)

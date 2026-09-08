"""合同驱动审查规则（书二 §6.1）——旧 agents.py:388-449 if-elif 的规则化迁移。

迁移对照（旧分支 → 规则条目）：
- ingest_document: needs_review / overall_confidence<0.8 → gate review（M1 低置信）
- solve_scheduling: lifecycle_status=="draft" → gate apply（M5 发布门）
- data_import_commit: success==False → fail
- run_bom_sop_workflow: 产出工程草稿 → gate engineering（禁当 retry 用）
- ingest_canonical: 候选落库 → gate candidate
- BLOCKED_INPUT 结果 → gate blocked_input（data 补数门）
默认规则：manifest spec.review_gate ∈ {candidate,review,engineering,procurement,schedule}
且未授权时 → 对应 Gate（schedule 归一化为 apply）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GATE_REVIEW = "review"
GATE_APPLY = "apply"


@dataclass(frozen=True)
class Check:
    field: str                    # 结果内点路径，如 "data.needs_review"
    op: str                       # eq | ne | lt | gt | truthy
    value: Any = None
    action: str = "pass"          # pass | fail | gate:<type>
    reason: str = ""


#: 显式规则表（旧 reviewer 行为全集主干；R2-R5 注册批次逐工具对照补全）。
RULES: dict[str, list[Check]] = {
    "ingest_document": [
        Check("data.needs_review", "truthy", action=f"gate:{GATE_REVIEW}",
              reason="M1 解析低置信/需人工复核（needs_review）"),
        Check("data.overall_confidence", "lt", 0.8, action=f"gate:{GATE_REVIEW}",
              reason="M1 解析整体置信度低于 0.8"),
    ],
    "solve_scheduling": [
        Check("data.lifecycle_status", "eq", "draft", action=f"gate:{GATE_APPLY}",
              reason="M5 排程为 draft，发布须人工 apply Gate（pressure_only 亦禁自动发布）"),
    ],
    "data_import_commit": [
        Check("success", "eq", False, action="fail", reason="M0 发布失败（fail-closed：回读计数为 0）"),
    ],
    "run_bom_sop_workflow": [
        Check("success", "eq", True, action="gate:engineering",
              reason="M2 产出工程草稿需工程确认（engineering Gate 不得当 retry 使用）"),
    ],
    "ingest_canonical": [
        Check("success", "eq", True, action="gate:candidate",
              reason="canonical 候选落库须 M0 candidate Gate 审批后发布"),
    ],
}

#: manifest review_gate 值 → Gate 类型归一化（registry._contract_defaults 产生）。
_REVIEW_GATE_MAP = {
    "candidate": "candidate",
    "review": GATE_REVIEW,
    "engineering": "engineering",
    "procurement": "procurement",
    "schedule": GATE_APPLY,       # 求解类合同语义归一到发布门
    "apply": GATE_APPLY,
    "sensitive_data": "sensitive_data",
    "authorization": "authorization",
    "data": "blocked_input",
    "blocked_input": "blocked_input",
    "none": "",
}


def gate_type_for(tool: str, spec: Any | None) -> str:
    if tool in RULES:
        for check in RULES[tool]:
            if check.action.startswith("gate:"):
                return check.action.split(":", 1)[1]
    if spec is None:
        return ""
    return _REVIEW_GATE_MAP.get(str(getattr(spec, "review_gate", "") or ""), "")


def _dig(result: dict[str, Any], path: str) -> Any:
    node: Any = result
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _hit(check: Check, result: dict[str, Any]) -> bool:
    actual = _dig(result, check.field)
    if check.op == "eq":
        return actual == check.value
    if check.op == "ne":
        return actual != check.value
    if check.op == "lt":
        try:
            return float(actual) < float(check.value)
        except (TypeError, ValueError):
            return False
    if check.op == "gt":
        try:
            return float(actual) > float(check.value)
        except (TypeError, ValueError):
            return False
    if check.op == "truthy":
        return bool(actual)
    return False


def evaluate(tool: str, result: dict[str, Any], spec: Any | None = None) -> list[dict[str, Any]]:
    """返回按序命中的 findings：[{check, action, gate, reason}]；空列表=通过。"""
    findings: list[dict[str, Any]] = []
    if str(result.get("code") or "").upper() == "BLOCKED_INPUT":
        findings.append({"check": "blocked_input", "action": "gate:blocked_input",
                         "gate": "blocked_input", "reason": "装配缺口需补数据（data Gate：retry+supplement）"})
        return findings
    for check in RULES.get(tool, []):
        if _hit(check, result):
            action = check.action
            findings.append({
                "check": f"{check.field} {check.op} {check.value}",
                "action": action,
                "gate": action.split(":", 1)[1] if action.startswith("gate:") else "",
                "reason": check.reason,
            })
    return findings


def default_gate_for_authorized(tool: str, spec: Any | None, authorized_steps: list[str]) -> bool:
    """合同默认门：有 review_gate 且该工具步骤已人工授权 → 放行。"""
    gate = gate_type_for(tool, spec)
    return (not gate) or (tool in authorized_steps)


#: 审查永不自动放行人工门（旧审计 P0 教训；test_no_auto_approve 锁定）。
AUTO_APPROVE_ALLOWED = False

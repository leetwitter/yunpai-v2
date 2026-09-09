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
    # M0 分片裁决（rows-S1 data_import_commit「审查需决策」二选一）：
    # **保留 fail 终态**（不可恢复的发布失败不得靠重试或补数蒙过），
    # **可恢复的输入缺口**走 evaluate 顶部的 BLOCKED_INPUT 短路 → blocked_input（data Gate：
    # retry+supplement）。两条路径互斥，既不降级也不删除任何门。
    "data_import_run": [
        Check("status", "eq", "failed", action="fail",
              reason="M0 未生成可审核候选（fail-closed：候选为空不得继续下游）"),
        Check("candidates", "truthy", action="gate:candidate",
              reason="M0 候选已登记，必须人工裁决（resolve）后才能 commit 发布 canonical"),
    ],
    "data_import_resolve": [
        Check("status", "eq", "approved", action="gate:candidate",
              reason="候选批准裁决须 M0 candidate Gate 复核（防 LLM 伪造裁决）"),
    ],
    "data_import_rollback": [
        Check("status", "eq", "rolled_back", action="gate:candidate",
              reason="canonical 回滚是破坏性写，须 M0 candidate Gate 复核"),
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

#: M0 canonical 写工具（rows-S1「审查需补」）：成功即开 candidate 门。
#: 为什么必须写进 RULES 而不是只改 manifest：V2 的 `reviewer_check_node` 只消费本表，
#: manifest 的 `review_gate` 目前仅被 `scripts/check_contracts.py`（W1/W2）读取
#: ——只声明不登记会变成「有门不生效」。manifest 侧同步声明 side_effect/review_gate
#: 保持契约自检干净（`check_contracts.py --strict`）。
M0_CANDIDATE_GATE_TOOLS: tuple[str, ...] = (
    "data_catalog_ingest_publish",
    "data_catalog_document_candidate_publish",
    "data_catalog_file_publish",
    "m0_products_import", "m0_orders_import", "m0_boms_import", "m0_materials_import",
    "m0_suppliers_import", "m0_equipment_import", "m0_routes_import",
    "m0_operations_import", "m0_tooling_import",
)
for _m0_write_tool in M0_CANDIDATE_GATE_TOOLS:
    RULES.setdefault(_m0_write_tool, [
        Check("success", "eq", True, action="gate:candidate",
              reason="M0 canonical 写入须 candidate Gate 人工审批（data-steward/m0-reviewer/admin）"),
    ])

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


def _diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """从工具结果抽取可诊断信息，随 finding 一起进 Gate（「缺什么、怎么补」）。

    口径（按优先级取第一个非空）：
    - ``code``：errors[0].code → result.code；
    - ``message``：errors[0].message → result.message → ``data.recovery`` →
      ``data.open_customer_questions[].question``（M2 skill 的补数问句）；
    - ``missing_fields``：``data.missing`` → ``data.missing_fields`` →
      ``result.missing_fields`` → ``data.open_customer_questions[].field``。
    """
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    questions = data.get("open_customer_questions")
    questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
    missing = data.get("missing") or data.get("missing_fields") or result.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []
    missing = [item for item in missing if isinstance(item, (str, dict))]
    if not missing:
        missing = [str(q.get("field")) for q in questions if str(q.get("field") or "")]
    message = str(first.get("message") or result.get("message") or data.get("recovery") or "")
    if not message:
        message = "；".join(str(q.get("question") or "") for q in questions if str(q.get("question") or ""))
    return {
        "code": str(first.get("code") or result.get("code") or ""),
        "message": message,
        "missing_fields": missing,
    }


def evaluate(tool: str, result: dict[str, Any], spec: Any | None = None) -> list[dict[str, Any]]:
    """返回按序命中的 findings：[{check, action, gate, reason}]；空列表=通过。

    每条 finding 额外携带 ``code`` / ``message`` / ``missing_fields``（工具结果里的
    可诊断信息），供 graph 在建门时写入 Gate——否则 blocked_input 门只有静态
    ``reason``，前端/驱动无法回答「缺什么、怎么补」（W913 基线实测）。
    """
    findings: list[dict[str, Any]] = []
    diagnostics = _diagnostics(result)
    if str(result.get("code") or "").upper() == "BLOCKED_INPUT":
        findings.append({"check": "blocked_input", "action": "gate:blocked_input",
                         "gate": "blocked_input", "reason": "装配缺口需补数据（data Gate：retry+supplement）",
                         **diagnostics})
        return findings
    for check in RULES.get(tool, []):
        if _hit(check, result):
            action = check.action
            findings.append({
                "check": f"{check.field} {check.op} {check.value}",
                "action": action,
                "gate": action.split(":", 1)[1] if action.startswith("gate:") else "",
                "reason": check.reason,
                **diagnostics,
            })
    return findings


def default_gate_for_authorized(tool: str, spec: Any | None, authorized_steps: list[str]) -> bool:
    """合同默认门：有 review_gate 且该工具步骤已人工授权 → 放行。"""
    gate = gate_type_for(tool, spec)
    return (not gate) or (tool in authorized_steps)


#: 审查永不自动放行人工门（旧审计 P0 教训；test_no_auto_approve 锁定）。
AUTO_APPROVE_ALLOWED = False

"""计划器（书二 §4.2）：RouteDecision → StepRecordV2 列表。"""
from __future__ import annotations

from typing import Any

from ..state import StepRecordV2
from .router import RouteDecision, SKILL_USAGE_ORDER, _ordered_skills
from .workflow_engine import WorkflowEngine


class Planner:
    def __init__(self, engine: WorkflowEngine | None = None):
        self.engine = engine or WorkflowEngine()

    def build(self, decision: RouteDecision, state: dict[str, Any]) -> list[StepRecordV2]:
        if decision.route == "workflow":
            return self.engine.expand(decision.workflow_id or "")
        if decision.route == "free":
            steps: list[StepRecordV2] = []
            skills = _ordered_skills(decision.skills)
            for name in skills:
                steps.append(StepRecordV2(
                    step_id=f"skill-{name}", kind="skill", name=name, tool=name,
                    status="pending", depends_on=[], attempt=0,
                ))
            seen: set[str] = set()
            for tool in decision.tools:
                if tool in seen or tool in skills:
                    continue  # 同名工具去重；技能已覆盖的同名跳过
                seen.add(tool)
                steps.append(StepRecordV2(
                    step_id=f"tool-{tool}", kind="tool", tool=tool, status="pending",
                    depends_on=[], attempt=0,
                ))
            return steps
        return []  # chat


def skill_usage_order_hint() -> tuple[str, ...]:
    return SKILL_USAGE_ORDER

"""DAG 工作流引擎（书二 §4.3）——depends_on 真实参与调度（治痛点 10）。

旧实现：加载时校验无前向依赖，运行时却按数组顺序 + next_step_index 线性推进。
v2：ready = status==pending ∧ depends_on ⊆ completed；为并行执行预留全量就绪集。
"""
from __future__ import annotations

from typing import Any

from ..state import StepRecordV2, now_iso
from ..workflow_registry import KNOWN_WORKFLOWS, load_workflow

__all__ = ["WorkflowEngine", "KNOWN_WORKFLOWS"]


class WorkflowEngine:
    def expand(self, workflow_id: str) -> list[StepRecordV2]:
        """加载 workflow JSON 并展开为拓扑序步骤集（同层按声明序，稳定）。"""
        spec = load_workflow(workflow_id)  # KeyError=未知；ValueError=前向依赖
        steps: list[StepRecordV2] = []
        for item in spec.get("steps", []):
            steps.append(StepRecordV2(
                step_id=str(item["id"]),
                kind="tool",
                module=str(item.get("module") or ""),
                tool=str(item["tool"]),
                status="pending",
                depends_on=[str(d) for d in item.get("depends_on", [])],
                attempt=0,
            ))
        self._assert_acyclic(steps)
        return steps

    @staticmethod
    def ready(plan: list[StepRecordV2]) -> list[str]:
        by_id = {s["step_id"]: s for s in plan}
        completed = {sid for sid, s in by_id.items() if s.get("status") == "completed"}
        return [
            s["step_id"] for s in plan
            if s.get("status") == "pending" and all(d in completed for d in s.get("depends_on", []))
        ]

    @staticmethod
    def mark(plan: list[StepRecordV2], step_id: str, status: str) -> list[StepRecordV2]:
        """返回更新后的 plan（不可变风格：新列表）。"""
        out: list[StepRecordV2] = []
        for s in plan:
            if s["step_id"] == step_id:
                s = {**s, "status": status}  # type: ignore[typeddict-item]
                if status in ("running", "completed", "failed", "blocked", "skipped"):
                    s["finished_at" if status != "running" else "started_at"] = now_iso()
            out.append(s)
        return out

    @staticmethod
    def stats(plan: list[StepRecordV2]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in plan:
            counts[s.get("status", "pending")] = counts.get(s.get("status", "pending"), 0) + 1
        return counts

    @staticmethod
    def _assert_acyclic(steps: list[StepRecordV2]) -> None:
        ids = {s["step_id"] for s in steps}
        for s in steps:
            unknown = [d for d in s.get("depends_on", []) if d not in ids]
            if unknown:
                raise ValueError(f"workflow step {s['step_id']} 依赖未知步骤: {unknown}")
        # Kahn 拓扑校验（load_workflow 已挡前向依赖，这里挡环）。
        indegree = {s["step_id"]: len(s.get("depends_on", [])) for s in steps}
        dependents: dict[str, list[str]] = {}
        for s in steps:
            for d in s.get("depends_on", []):
                dependents.setdefault(d, []).append(s["step_id"])
        queue = [sid for sid, deg in indegree.items() if deg == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for nxt in dependents.get(node, []):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if seen != len(steps):
            raise ValueError("workflow 存在依赖环")


def workflow_tools(workflow_id: str) -> list[str]:
    return [str(item["tool"]) for item in load_workflow(workflow_id).get("steps", [])]


def step_by_tool(plan: list[StepRecordV2], tool: str) -> StepRecordV2 | None:
    return next((s for s in plan if s.get("tool") == tool), None)

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

#: 版本化受控工作流（文件名即稳定 id）。
#: 已删除旧版 m0_m5（M0 在前、M1 读 request.document 单数 bug、M5 只有 solve），
#: 统一由 m1_m5_document_to_plan 承担原始文件入口。
KNOWN_WORKFLOWS = (
    "m1_m5_document_to_plan",
    "canonical_to_m5",
)


@lru_cache(maxsize=8)
def load_workflow(workflow_id: str = "m1_m5_document_to_plan") -> dict[str, Any]:
    path = Path(__file__).with_name("workflows") / f"{workflow_id}.json"
    if not path.exists():
        raise KeyError(f"unknown workflow: {workflow_id}")
    workflow = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for step in workflow.get("steps", []):
        if step["id"] in seen:
            raise ValueError(f"duplicate workflow step: {step['id']}")
        missing = set(step.get("depends_on", [])) - seen
        if missing:
            raise ValueError(f"step {step['id']} has unresolved dependencies: {sorted(missing)}")
        seen.add(step["id"])
    return workflow


def required_capabilities(workflow_id: str) -> list[dict[str, str]]:
    """返回 workflow 全链必需的 (module, tool) 能力清单，用于绑定 Gate。"""
    workflow = load_workflow(workflow_id)
    return [
        {"module": str(step["module"]), "tool": str(step["tool"])}
        for step in workflow.get("steps", [])
    ]

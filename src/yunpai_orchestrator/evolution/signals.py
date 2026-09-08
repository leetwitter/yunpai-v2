from __future__ import annotations

import logging
from hashlib import sha256
from typing import Any
from uuid import uuid4

from ..models import RunState
from .gate import should_promote
from .repository import EvolutionRepository

logger = logging.getLogger("yunpai.evolution")


def _digest(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return sha256(raw.encode("utf-8")).hexdigest()[:16]


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 信号提取（纯函数，只读 RunState；绝不改动主流程）
# ---------------------------------------------------------------------------

def _tool_sequence(state: RunState) -> list[str]:
    """工具调用序列（保留顺序；completed/blocked/failed 视为实际发生的调用）。"""
    tools: list[str] = []
    for step in state.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("status") in {"completed", "blocked", "failed"} and step.get("tool"):
            tools.append(str(step["tool"]))
    return tools


def extract_scheduling_cases(state: RunState) -> list[dict[str, Any]]:
    """M5 案例库收编：run 里 record_m5_knowledge 的产出 → scheduling_case 候选。"""
    result = state.get("outputs", {}).get("record_m5_knowledge")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or not data.get("plan_version"):
        return []
    plan_version = str(data.get("plan_version"))
    return [{
        "kind": "scheduling_case",
        "fingerprint": _digest("m5", plan_version),
        "strong": False,
        "content": {
            "plan_version": plan_version,
            "scenario_id": str(data.get("scenario_id") or ""),
            "on_time_rate": data.get("on_time_rate"),
            "total_tardiness_minutes": data.get("total_tardiness_minutes"),
            "solver_status": str(data.get("solver_status") or ""),
            "validation_passed": data.get("validation_passed"),
        },
        "applicability": {"tool": "record_m5_knowledge", "plan_version": plan_version},
        "source_kind": "m5_release",
        "locator": {"run_id": state.get("run_id", ""), "plan_version": plan_version},
        "pattern": None,
    }]


def extract_repeated_operation(state: RunState) -> list[dict[str, Any]]:
    tools = _tool_sequence(state)
    if len(tools) < 2:
        return []
    digest = _digest(*tools)
    intent = (state.get("intent") or {}).get("name") or state.get("route") or ""
    return [{
        "kind": "repeated_operation",
        "fingerprint": digest,
        "strong": False,
        "content": {"tools": tools, "step_count": len(tools)},
        "applicability": {"intent": str(intent), "tool_sequence": tools},
        "source_kind": "run_step",
        "locator": {"run_id": state.get("run_id", ""), "steps": len(state.get("steps") or [])},
        "pattern": {
            "steps_digest": digest,
            "tools": tools,
            "params_template": _params_template(state),
            "trigger": {"intent": str(intent)},
        },
    }]


def extract_error_patterns(state: RunState) -> list[dict[str, Any]]:
    """经常出错：持久化 errors 里带 tool 的业务错误 + 被拒绝的 Gate。"""
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for error in state.get("errors") or []:
        if not isinstance(error, dict):
            continue
        tool = str(error.get("tool") or "")
        code = str(error.get("code") or "UNKNOWN")
        if not tool:
            continue
        key = _digest(tool, code)
        if key in seen:
            continue
        seen.add(key)
        signals.append({
            "kind": "error_pattern",
            "fingerprint": key,
            "strong": False,
            "content": {"code": code, "tool": tool, "message": str(error.get("message") or "")},
            "applicability": {"tool": tool, "error_code": code},
            "source_kind": "run_step",
            "locator": {"run_id": state.get("run_id", ""), "code": code},
            "pattern": None,
        })
    # 被人工拒绝/终止的 Gate 也是错误模式（同类 Gate 反复被拒）。
    for approval in state.get("approvals") or []:
        if not isinstance(approval, dict):
            continue
        if approval.get("decision") not in {"reject", "stop"}:
            continue
        gate = approval.get("gate") or {}
        if not isinstance(gate, dict):
            continue
        tool = str(gate.get("tool") or "")
        gate_type = str(gate.get("type") or "gate")
        if not tool:
            continue
        key = _digest(tool, "rejected_gate", gate_type)
        if key in seen:
            continue
        seen.add(key)
        signals.append({
            "kind": "error_pattern",
            "fingerprint": key,
            "strong": False,
            "content": {"code": "GATE_REJECTED", "tool": tool, "gate_type": gate_type,
                        "message": str(gate.get("message") or "")},
            "applicability": {"tool": tool, "gate_type": gate_type},
            "source_kind": "gate_decision",
            "locator": {"run_id": state.get("run_id", ""), "gate_type": gate_type},
            "pattern": None,
        })
    return signals


def extract_human_corrections(state: RunState) -> list[dict[str, Any]]:
    """人工纠正：Gate 以 supplement 方式补入业务数据（人纠正了 AI 的缺口）。强证据。"""
    signals: list[dict[str, Any]] = []
    for approval in state.get("approvals") or []:
        if not isinstance(approval, dict):
            continue
        if not approval.get("supplemented"):
            continue
        gate = approval.get("gate") or {}
        tool = str((gate if isinstance(gate, dict) else {}).get("tool") or "")
        if not tool:
            continue
        supplement = approval.get("supplement")
        keys = sorted(supplement.keys()) if isinstance(supplement, dict) else list(approval.get("supplement_keys") or [])
        signals.append({
            "kind": "human_correction",
            "fingerprint": _digest(tool, *keys),
            "strong": True,
            "content": {"tool": tool, "corrected_fields": keys,
                        "note": "人工补入业务数据（强证据）"},
            "applicability": {"tool": tool, "fields": keys},
            "source_kind": "human_correction",
            "locator": {"run_id": state.get("run_id", ""), "actor": approval.get("actor", "")},
            "pattern": None,
        })
    return signals


def extract_success_patterns(state: RunState) -> list[dict[str, Any]]:
    """经常做对：Gate 被直接批准（未 supplement，未 override）。"""
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for approval in state.get("approvals") or []:
        if not isinstance(approval, dict):
            continue
        if approval.get("decision") not in {"allow", "approve", "continue"}:
            continue
        if approval.get("supplemented") or approval.get("human_override"):
            continue
        gate = approval.get("gate") or {}
        if not isinstance(gate, dict):
            continue
        tool = str(gate.get("tool") or "")
        gate_type = str(gate.get("type") or "gate")
        if not tool:
            continue
        key = _digest(tool, gate_type)
        if key in seen:
            continue
        seen.add(key)
        signals.append({
            "kind": "success_mapping",
            "fingerprint": key,
            "strong": False,
            "content": {"tool": tool, "gate_type": gate_type,
                        "note": "同类 Gate 被直接批准（成功偏好）"},
            "applicability": {"tool": tool, "gate_type": gate_type},
            "source_kind": "gate_decision",
            "locator": {"run_id": state.get("run_id", ""), "gate_type": gate_type},
            "pattern": None,
        })
    return signals


def _params_template(state: RunState) -> dict[str, Any]:
    """从各步骤 input_summary 提取参数键名模板（值为类型占位，不含真实业务值）。"""
    template: dict[str, Any] = {}
    for step in state.get("steps") or []:
        if not isinstance(step, dict) or not step.get("tool"):
            continue
        summary = step.get("input_summary") or {}
        if isinstance(summary, dict):
            template[str(step["tool"])] = {str(k): _type_placeholder(v) for k, v in summary.items()}
    return template


def _type_placeholder(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _in_cooldown(candidate: dict[str, Any], *, days: int = 7) -> bool:
    """「稍后」冷却：last_error 形如 later:<iso>，7 天内不再自动提议。"""
    from datetime import datetime, timedelta, timezone

    marker = str(candidate.get("last_error") or "")
    if not marker.startswith("later:"):
        return False
    try:
        iso = marker[len("later:"):]
        when = datetime.fromisoformat(iso)
        return datetime.now(timezone.utc) - when < timedelta(days=days)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# observe_run：单一观察钩子（graph.run/stream 完成时调用一次）
# ---------------------------------------------------------------------------

def observe_run(repo: EvolutionRepository, state: RunState) -> dict[str, Any]:
    """从最终 RunState 提取四类信号并写入演进仓库。幂等（按 task_id 判独立）。"""
    if repo is None:
        return {"observed": False, "reason": "no repository"}
    tenant_id = str(state.get("tenant_id") or "default")
    run_id = str(state.get("run_id") or "")
    task_id = str(state.get("task_id") or "")

    signals: list[dict[str, Any]] = []
    signals.extend(extract_repeated_operation(state))
    signals.extend(extract_error_patterns(state))
    signals.extend(extract_human_corrections(state))
    signals.extend(extract_success_patterns(state))
    signals.extend(extract_scheduling_cases(state))

    counts: dict[str, int] = {}
    proposed: list[str] = []
    for signal in signals:
        try:
            _observe_one(repo, signal, tenant_id=tenant_id, run_id=run_id, task_id=task_id)
            counts[signal["kind"]] = counts.get(signal["kind"], 0) + 1
        except Exception as exc:  # 演进观察绝不破坏主流程
            logger.warning("evolution.observe_one failed kind=%s error=%s", signal.get("kind"), exc)
    return {"observed": True, "signals": counts, "run_id": run_id, "task_id": task_id}


def _observe_one(repo: EvolutionRepository, signal: dict[str, Any], *, tenant_id: str,
                 run_id: str, task_id: str) -> None:
    fingerprint = str(signal["fingerprint"])
    source_id = _id("SRC")
    content_hash = _digest(signal["kind"], fingerprint, run_id)
    repo.record_source(
        source_id=source_id, tenant_id=tenant_id, source_kind=str(signal["source_kind"]),
        run_id=run_id, task_id=task_id, trace_id=f"{task_id}:{signal['kind']}",
        document_id="", entity_ref="", locator=signal.get("locator") or {},
        content_hash=content_hash,
    )
    candidate = repo.upsert_candidate(
        candidate_id=_id("CAND"), tenant_id=tenant_id, kind=str(signal["kind"]),
        fingerprint=fingerprint, content=signal.get("content") or {},
        applicability=signal.get("applicability") or {}, scope="tenant",
    )
    candidate_id = str(candidate["candidate_id"])
    repo.add_candidate_source(
        candidate_id=candidate_id, source_id=source_id, tenant_id=tenant_id, task_id=task_id,
    )
    # 重复操作额外维护 pattern 表（供快捷操作固化）。
    pattern = signal.get("pattern")
    if isinstance(pattern, dict):
        pat = repo.upsert_pattern(
            pattern_id=_id("PAT"), tenant_id=tenant_id,
            steps_digest=str(pattern["steps_digest"]), tools=list(pattern.get("tools") or []),
            params_template=pattern.get("params_template") or {}, trigger=pattern.get("trigger") or {},
        )
        repo.increment_pattern(str(pat["pattern_id"]), independent_delta=1)
    # 晋升检查（候选更新后重新读取最新计数）。
    fresh = repo.get_candidate(candidate_id) or candidate
    promote, reason = should_promote(fresh, strong=bool(signal.get("strong")))
    if promote and _in_cooldown(fresh):
        promote = False
    if promote:
        repo.set_candidate_status(candidate_id, "proposed")

from __future__ import annotations

import base64
from copy import deepcopy
import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from .agents import PlannerAgent, ReviewerAgent, WorkerAgent, result_data
from .models import RunState, new_state, summarize
from .registry import ToolRegistry, build_default_registry
from .repository import InMemoryRunRepository, RunRepository
from .skills import SkillRegistry, build_default_skill_registry
from .orchestration_bridge import BRIDGED_WORKFLOWS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_object(filename: str, value: Any) -> dict[str, str]:
    if isinstance(value, dict) and isinstance(value.get("content_b64"), str):
        return value
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {
        "filename": filename,
        "content_type": "application/json",
        "content_b64": base64.b64encode(raw.encode()).decode(),
    }


def _batch_id_from_result(value: Any) -> str | None:
    """Extract an M0 batch id from local or HTTP-wrapped tool output.

    The standalone M0 service wraps upload responses as
    ``{"success": true, "data": {"id": "..."}}`` while the local handler
    returns ``batch_id`` at the top level.  Workflow continuation must use the
    service's batch id, not the wrapper object or an unrelated import id.
    """
    if not isinstance(value, dict):
        return None
    data = result_data(value)
    candidates: list[Any] = [data.get("batch_id"), data.get("id")]
    batch = data.get("batch")
    if isinstance(batch, dict):
        candidates.extend((batch.get("batch_id"), batch.get("id")))
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def _due_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if "T" in text else f"{text}T23:59:00+08:00"


class CompatGraph:
    def __init__(self, runner: Callable[[RunState], Any]):
        self._runner = runner

    async def ainvoke(self, state: RunState, config: dict[str, Any] | None = None) -> RunState:
        return await self._runner(state)

    def invoke(self, state: RunState, config: dict[str, Any] | None = None) -> RunState:
        import asyncio
        return asyncio.run(self.ainvoke(state, config))


class YunpaiGraph:
    """Planner/Worker/Reviewer 三 Agent 执行器和可持久化状态机。"""

    def __init__(self, registry: ToolRegistry | None = None, repository: RunRepository | None = None, skills: SkillRegistry | None = None, evolution: Any | None = None) -> None:
        self.registry = registry or build_default_registry()
        self.repository = repository or InMemoryRunRepository()
        self.skills = skills or build_default_skill_registry()
        self.planner = PlannerAgent(skills=self.skills)
        self.worker = WorkerAgent(self.registry, self.skills)
        self.reviewer = ReviewerAgent()
        self.evolution = evolution  # EvolutionRepository | None（观察钩子；None=关闭）

    def _required_capability_gap(self, state: RunState) -> list[dict[str, str]]:
        """主链必需工具绑定 Gate：workflow 每一步的 tool 都必须有可执行 handler。

        只对受控 workflow 生效；free/chat 路径沿用原有"调用即失败"语义，
        避免把单工具自由路径的显式错误改成静默通过。
        """
        if state.get("route") != "workflow":
            return []
        missing: list[dict[str, str]] = []
        for step in state.get("plan", []):
            tool = step.get("tool", "")
            module = step.get("module", "")
            if step.get("kind") == "skill" or tool in self.skills.specs:
                continue
            if tool not in self.registry.handlers:
                missing.append({"module": module, "tool": tool})
        return missing

    def _save(self, state: RunState) -> RunState:
        self.repository.save(state)
        return state

    def _observe(self, state: RunState) -> None:
        """run 到达终态后观察一次（知识自进化钩子）。绝不破坏主流程。"""
        if self.evolution is None:
            return
        try:
            from .evolution.signals import observe_run
            observe_run(self.evolution, state)
        except Exception:  # pragma: no cover - 演进观察失败不阻断业务
            pass

    def _inject_knowledge(self, state: RunState) -> None:
        """把 active 知识检索结果挂到 state（Planner 注入点；仅记录，不改计划）。"""
        if self.evolution is None:
            return
        try:
            from .evolution.injection import build_context, retrieve_active_knowledge
            intent = (state.get("intent") or {}).get("name") or state.get("route") or ""
            context = build_context(state.get("request", {}), intent=str(intent),
                                    tenant_id=str(state.get("tenant_id") or "default"))
            items = retrieve_active_knowledge(self.evolution, tenant_id=str(state.get("tenant_id") or "default"),
                                              context=context, limit=5)
            state["knowledge_context"] = [
                {"knowledge_id": item.get("knowledge_id"), "kind": item.get("kind"),
                 "applicability": item.get("applicability") or {}, "content": item.get("content") or {}}
                for item in items
            ]
            if items:
                state.setdefault("trace", []).append(
                    {"event": "knowledge.injected", "count": len(items), "at": _now()})
        except Exception:  # pragma: no cover
            pass

    @staticmethod
    def _public_state(state: RunState) -> RunState:
        """Remove file bodies from snapshots while retaining metadata."""
        public = deepcopy(state)

        def redact(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "content_b64" and isinstance(child, str):
                        value[key] = "[omitted]"
                    else:
                        redact(child)
            elif isinstance(value, list):
                for child in value:
                    redact(child)

        redact(public)
        return public

    @classmethod
    def _event(cls, run_state: RunState, event_type: str, **payload: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "run_id": run_state["run_id"],
            "task_id": run_state["task_id"],
            "at": _now(),
            **payload,
        }

    async def planner_node(self, state: RunState) -> RunState:
        if state.get("status") == "waiting_human":
            return state
        if not state.get("plan"):
            decision = await self.planner.aplan(state["request"], self.registry)
            state["route"], state["plan"] = decision["route"], decision["steps"]
            state["workflow_id"] = decision.get("workflow_id", "")
            state["workflow_version"] = decision.get("workflow_version", "")
            state["intent"] = decision.get("intent", {})
            state["route_decision"] = decision.get("route_decision", {})
            state["model"] = decision.get("model", {})
            if decision.get("error"):
                state["status"] = "failed"
                state["errors"] = [decision["error"]]
                state["response"] = str(decision.get("response") or decision["error"].get("code"))
                state["trace"].append({"event": "planner.rejected", "code": decision["error"].get("code"), "at": _now()})
                return self._save(state)
            if state.get("route") == "chat":
                state["response"] = str(decision.get("response") or "")
            state["trace"].append({"event": "react.thought", "agent": "planner", "reason": decision["reason"], "at": _now()})
            state["trace"].append({"event": "agent.model", "agent": "planner", "provider": state["model"].get("provider"), "model": state["model"].get("model"), "status": state["model"].get("status"), "latency_ms": state["model"].get("latency_ms"), "at": _now()})
            state["trace"].append({"event": "agent.intent", "agent": "planner", **state["intent"], "at": _now()})
            state["trace"].append({"event": "agent.route", "agent": "planner", "route": state["route"], "source": state["route_decision"].get("source"), "selected_tools": [step["tool"] for step in state["plan"]], "reason": decision["reason"], "model": state["model"].get("model"), "at": _now()})
        capability_gap = self._required_capability_gap(state)
        if capability_gap:
            # 主链必需工具未绑定时，在执行任何步骤前返回结构化能力缺口，
            # 不得执行到中途才报 generic 500。
            state["status"] = "failed"
            state["errors"] = [{
                "code": "CAPABILITY_UNAVAILABLE",
                "message": "工作流所需工具未全部绑定，无法开始执行",
                "missing": capability_gap,
                "workflow_id": state.get("workflow_id", ""),
            }]
            state["response"] = "主链必需工具未绑定：\n" + "\n".join(
                f"- {item['module']} / {item['tool']}" for item in capability_gap
            )
            state["trace"].append({"event": "run.capability_unavailable", "missing": capability_gap, "at": _now()})
            return self._save(state)
        if not state["plan"] or int(state.get("next_step_index", 0)) >= len(state["plan"]):
            state["status"] = "completed"
            state["response"] = (
                (state.get("response") or "已理解请求；当前消息不需要调用业务工具。").strip()
                if not state["plan"]
                else "计划内工具已执行并通过审查；所有正式副作用均受 Gate 和模块合同约束。"
            )
            state["trace"].append({"event": "run.completed", "at": _now()})
        else:
            state["status"] = "running"
        self._inject_knowledge(state)
        return self._save(state)

    async def worker_node(self, state: RunState) -> RunState:
        index = int(state.get("next_step_index", 0))
        if state.get("status") != "running" or index >= len(state.get("plan", [])):
            return state
        step = state["plan"][index]
        state["current_step"] = step["tool"]
        preflight = self.reviewer.preflight(step, state)
        if preflight:
            state["pending_gate"] = {**preflight, "step_index": index, "opened_at": _now()}
            state["status"] = "waiting_human"
            state["trace"].append({"event": "gate.opened", "tool": step["tool"], "step_index": index, "phase": "pre_execution", "at": _now()})
            return self._save(state)
        payload = self._payload_for(state, step["tool"])
        if state.get("route") == "workflow" and state.get("workflow_id") in BRIDGED_WORKFLOWS:
            from .orchestration_bridge import bridge_payload
            bridged = bridge_payload(state, step["tool"])
            if bridged.get("success") is False and bridged.get("code") == "BLOCKED_INPUT":
                # 桥接发现缺少权威输入：作为可恢复的业务 Gate（数据 Gate）交给
                # Reviewer，不调用 tool，也不把 fixture/request 默认冒充事实。
                record = {
                    "id": step["id"], "module": step["module"], "tool": step["tool"],
                    "status": "blocked", "input_summary": summarize(payload),
                    "started_at": _now(), "finished_at": _now(),
                }
                state["steps"].append(record)
                state["current_result"] = bridged
                state["outputs"][step["tool"]] = bridged
                state["outputs"][step["module"]] = bridged
                state["evidence"].extend(bridged.get("evidence", []))
                record.update(output_summary=summarize(bridged), evidence=bridged.get("evidence", []))
                state["trace"].append({"event": "bridge.blocked_input", "tool": step["tool"], "step_index": index, "missing_fields": bridged.get("data", {}).get("missing_fields", []), "at": _now()})
                return self._save(state)
            payload = bridged
        record = {
            "id": step["id"], "module": step["module"], "tool": step["tool"],
            "status": "running", "input_summary": summarize(payload), "input": payload, "started_at": _now(),
        }
        state["steps"].append(record)
        state["trace"].append({"event": "react.action", "agent": "worker", "tool": step["tool"], "step_index": index, "at": _now()})
        try:
            outcome = await self.worker.run(state, step, payload)
        except Exception as exc:
            # T2：M2 工程工具(模型端点/服务)不可达或超时属可恢复运行态故障。
            # 转成 BLOCKED_INPUT 让 Reviewer 打开数据 Gate（补充数据/重试），
            # 绝不把“端点不可用”伪装成模型成功或直接硬失败丢失恢复路径。
            from .registry import ToolHTTPError

            if step["tool"] == "run_bom_sop_workflow" and isinstance(exc, ToolHTTPError) and exc.code in {"HTTP_UNAVAILABLE", "HTTP_TIMEOUT", "HTTP_STATUS_ERROR", "HTTP_UNAVAILABLE_BACKEND"}:
                result = {
                    "success": False, "code": "BLOCKED_INPUT",
                    "errors": [{"code": "M2_MODEL_ENDPOINT_UNAVAILABLE", "message": f"M2 模型/服务端点不可用（{exc.code}）：{str(exc)[:400]}。请检查 M2_MODEL_BASE_URL 是否指向可达的 Qwen 端点，修复后补充数据重试", "details": []}],
                    "evidence": [{"module": "m2", "source_ref": step["tool"], "evidence_ref": f"m2:{step['tool']}:endpoint-unavailable", "detail": "M2 模型端点不可用：已停止并转为可恢复数据 Gate，未伪造模型成功"}],
                    "trace_id": f"{state['task_id']}:{step['tool']}",
                }
                state["current_result"] = result
                state["outputs"][step["tool"]] = result
                state["outputs"][step["module"]] = result
                state["evidence"].extend(result["evidence"])
                record.update(output_summary=summarize(result), evidence=result["evidence"], finished_at=_now())
                state["trace"].append({"event": "react.observation", "agent": "worker", "tool": step["tool"], "status": "blocked_input", "at": _now()})
                return self._save(state)
            if step["tool"] in {"run_bom_sop_workflow", "run_m3_procurement_requirements", "solve_scheduling"} and isinstance(exc, ValueError) and str(exc).startswith("invalid input"):
                # Contract validation failures for required BOM/SOP/route
                # facts are recoverable business-data gaps, not agent crashes.
                raw_message = str(exc)
                if step["tool"] in {"run_bom_sop_workflow", "run_m3_procurement_requirements"} and any(token in raw_message for token in ("bom", "BOM", "m2_package")):
                    business_code, business_message = "MISSING_BOM", "缺少或不匹配的 BOM 业务数据"
                elif step["tool"] == "solve_scheduling" and any(token in raw_message for token in ("routing_steps", "route", "SOP")):
                    business_code, business_message = "MISSING_SOP", "缺少或不匹配的 SOP/工艺路线业务数据"
                else:
                    business_code, business_message = "BUSINESS_INPUT_INVALID", "业务输入不完整或与工具合同不匹配"
                result = {
                    "success": False, "code": "BLOCKED_INPUT",
                    "errors": [{"code": business_code, "message": business_message, "details": [raw_message]}],
                    "evidence": [{"module": step["module"], "source_ref": step["tool"], "evidence_ref": f"{step['module']}:{step['tool']}", "detail": "工具合同校验发现业务输入不完整"}],
                    "trace_id": f"{state['task_id']}:{step['tool']}",
                }
                state["current_result"] = result
                state["outputs"][step["tool"]] = result
                state["outputs"][step["module"]] = result
                state["evidence"].extend(result["evidence"])
                record.update(output_summary=summarize(result), evidence=result["evidence"], finished_at=_now())
                state["trace"].append({"event": "react.observation", "agent": "worker", "tool": step["tool"], "status": "blocked_input", "at": _now()})
                return self._save(state)
            error = {"code": "TOOL_ERROR", "tool": step["tool"], "message": str(exc)}
            record.update(status="failed", error=error, finished_at=_now())
            state["errors"].append(error)
            state["status"] = "failed"
            state["trace"].append({"event": "react.observation", "tool": step["tool"], "status": "failed", "at": _now()})
            return self._save(state)
        result = outcome["result"]
        if state.get("route") == "workflow" and state.get("workflow_id") in BRIDGED_WORKFLOWS:
            if step["tool"] == "run_bom_sop_workflow":
                from .orchestration_bridge import merge_m2_canonical_bom
                result = merge_m2_canonical_bom(result, payload)
            elif step["tool"] == "run_m3_procurement_requirements":
                from .orchestration_bridge import merge_m3_deferred_bom
                result = merge_m3_deferred_bom(result, payload)
        state["current_result"] = result
        state["outputs"][step["tool"]] = result
        state["outputs"][step["module"]] = result
        state["evidence"].extend(result.get("evidence", []))
        record.update(output_summary=outcome["output_summary"], evidence=result.get("evidence", []), finished_at=_now())
        state["trace"].append({"event": "react.observation", "agent": "worker", "tool": step["tool"], "status": "received", "at": _now()})
        return self._save(state)

    async def reviewer_node(self, state: RunState) -> RunState:
        if state.get("status") != "running" or not state.get("current_result"):
            return state
        index = int(state.get("next_step_index", 0))
        step = state["plan"][index]
        verdict = self.reviewer.review(step["tool"], step["module"], state["current_result"])
        record = state["steps"][-1]
        state["trace"].append({"event": "react.review", "agent": "reviewer", "tool": step["tool"], "approved": verdict["approved"], "at": _now()})
        if verdict.get("terminal"):
            record["status"] = "failed"
            state["errors"].append(verdict["error"])
            state["status"] = "failed"
        elif not verdict["approved"]:
            record["status"] = "blocked"
            gate = dict(verdict["gate"])
            result = state["current_result"]
            if result.get("code") == "BLOCKED_INPUT" or result.get("success") is False:
                result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
                gate.setdefault("code", result.get("code") or "BLOCKED_INPUT")
                gate.setdefault("missing_fields", list(result.get("missing_fields") or result_data.get("missing_fields") or []))
                gate.setdefault("recovery", result_data.get("recovery") or "")
                gate.setdefault("ui_action", "request_data")
            state["pending_gate"] = {**gate, "step_index": index, "opened_at": _now()}
            state["status"] = "waiting_human"
            state["trace"].append({"event": "gate.opened", "tool": step["tool"], "step_index": index, "at": _now()})
        else:
            record["status"] = "completed"
            state["next_step_index"] = index + 1
            state["current_result"] = {}
            if state["next_step_index"] >= len(state["plan"]):
                state["status"] = "completed"
                state["response"] = "计划内工具已执行并通过审查；所有正式副作用均受 Gate 和模块合同约束。"
                state["trace"].append({"event": "run.completed", "at": _now()})
        return self._save(state)

    def route_after_planner(self, state: RunState) -> str:
        return "worker" if state.get("status") == "running" else "end"

    def route_after_reviewer(self, state: RunState) -> str:
        return "worker" if state.get("status") == "running" else "end"

    async def run(self, state: RunState) -> RunState:
        if state.get("status") == "waiting_human":
            return state
        state = await self.planner_node(state)
        while state.get("status") == "running":
            state = await self.worker_node(state)
            state = await self.reviewer_node(state)
        self._observe(state)
        return state

    async def stream(self, state: RunState):
        """Execute the same graph as ``run`` and yield persisted progress events."""
        yield self._event(state, "run_start", state=self._public_state(state))
        try:
            state = await self.planner_node(state)
            thought = next((item for item in reversed(state.get("trace", [])) if item.get("event") == "react.thought"), None)
            if thought:
                yield self._event(state, "assistant_delta", content=state.get("response") or thought.get("reason", "已生成执行计划"))
            yield self._event(state, "state_snapshot", state=self._public_state(state))

            if state.get("status") != "running":
                yield self._event(state, "run_done", state=self._public_state(state))
                return

            while state.get("status") == "running":
                index = int(state.get("next_step_index", 0))
                step = state["plan"][index]
                yield self._event(state, "assistant_delta", content=f"正在执行 {step['module'].upper()} · {step['tool']}")
                yield self._event(state, "step_start", step=deepcopy(step))
                step_count = len(state.get("steps", []))
                state = await self.worker_node(state)

                if state.get("status") == "waiting_human":
                    yield self._event(state, "gate_opened", gate=deepcopy(state["pending_gate"]))
                    yield self._event(state, "state_snapshot", state=self._public_state(state))
                    return

                if len(state.get("steps", [])) > step_count:
                    record = state["steps"][-1]
                    yield self._event(
                        state,
                        "step_result",
                        step=deepcopy(record),
                        output_summary=deepcopy(record.get("output_summary", {})),
                    )

                state = await self.reviewer_node(state)
                if state.get("pending_gate"):
                    yield self._event(state, "gate_opened", gate=deepcopy(state["pending_gate"]))
                yield self._event(state, "state_snapshot", state=self._public_state(state))

            self._observe(state)
            yield self._event(state, "run_done", state=self._public_state(state))
        except (KeyError, ValueError) as exc:
            state["status"] = "failed"
            error = {"code": "RUN_ERROR", "message": str(exc)}
            state.setdefault("errors", []).append(error)
            self._save(state)
            self._observe(state)
            yield self._event(state, "run_error", **error)
            yield self._event(state, "run_done", state=self._public_state(state))

    @staticmethod
    def validate_resume_decision(
        state: RunState,
        decision: str,
        supplement: dict[str, Any] | None = None,
        *,
        human_override: bool = False,
    ) -> None:
        if state.get("status") != "waiting_human" or not state.get("pending_gate"):
            raise ValueError("run is not waiting_human")
        if decision not in {"allow", "approve", "continue", "retry", "reject", "stop"}:
            raise ValueError("unsupported gate decision")
        if supplement is not None and (not isinstance(supplement, dict) or isinstance(supplement, list)):
            raise ValueError("supplement must be a JSON object")
        gate = state["pending_gate"]
        if gate.get("type") == "data":
            if decision == "approve" and not human_override:
                raise ValueError("data gate cannot be approved; provide business-data supplement or reject")
            if decision not in {"allow", "approve", "continue", "retry", "reject", "stop"} or (
                decision not in {"reject", "stop"} and not supplement and not human_override
            ):
                raise ValueError("data gate requires business-data supplement or rejection")

    #: Gate 类型 -> 允许的角色（T5.3）。HTTP /resume 只接受带这些角色的受信
    #: principal；Graph 内部 actor 参数仅供嵌入/测试。
    GATE_ALLOWED_ROLES: dict[str, tuple[str, ...]] = {
        "candidate": ("data-steward", "m0-reviewer", "admin"),
        "sensitive_data": ("data-steward", "hr-officer", "admin"),
        "review": ("document-reviewer", "data-steward", "admin"),
        "engineering": ("engineering-manager", "admin"),
        "procurement": ("procurement-manager", "purchase-reviewer", "admin"),
        "apply": ("production-manager", "admin"),
        "authorization": ("operator", "admin"),
        "blocked_input": ("data-steward", "engineering-manager", "production-manager", "admin"),
    }

    @staticmethod
    def authorize_gate(state: RunState, *, actor: str, roles: list[str],
                       tenant_id: str | None = None) -> None:
        """受信 principal 对当前 pending_gate 的授权校验（T5.2/T5.4）。

        - 无 gate（未等待人工）拒绝。
        - body/principal actor 为空的匿名审批拒绝。
        - 跨租户审批拒绝（principal tenant 与 run tenant 不一致）。
        - 角色不在 gate 允许清单内的审批拒绝。
        任一项失败抛 ValueError（HTTP 调用方映射 403/409），不修改 RunState。
        """
        gate = state.get("pending_gate") or {}
        if not gate:
            raise ValueError("no pending gate to authorize")
        if not actor or not actor.strip():
            raise ValueError("anonymous principal cannot approve a gate")
        run_tenant = str(state.get("tenant_id") or "default")
        if tenant_id and str(tenant_id) not in {"", run_tenant}:
            raise ValueError(f"cross-tenant approval rejected: run tenant={run_tenant}, principal tenant={tenant_id}")
        gate_type = str(gate.get("type") or "authorization")
        allowed = YunpaiGraph.GATE_ALLOWED_ROLES.get(gate_type) or ("admin",)
        principal_roles = [str(role) for role in roles] if isinstance(roles, list) else []
        if not principal_roles or not (set(principal_roles) & set(allowed)):
            raise ValueError(
                f"gate {gate_type} 需要角色 {'/'.join(allowed)}；当前 principal roles={principal_roles or ['(none)']}"
            )

    @staticmethod
    def validate_principal_request(body: dict[str, Any]) -> None:
        """T5.1：/resume 不再信任请求体自报 actor；携带 body actor 的请求必须
        与受信 principal 一致，否则拒绝（冒充检测在 HTTP 层做）。"""
        if isinstance(body.get("actor"), str) and body["actor"] != "untrusted":
            # HTTP 层解析受信 principal 后覆盖；若 body 存在自报 actor 且
            # 调用方没有提供受信 principal，会由调用方直接拒绝，这里只留钩子。
            return

    def _prepare_resume(
        self,
        state: RunState,
        decision: str,
        supplement: dict[str, Any] | None = None,
        *,
        actor: str = "operator",
        human_override: bool = False,
        override_reason: str = "",
    ) -> RunState:
        self.validate_resume_decision(state, decision, supplement, human_override=human_override)
        gate = dict(state["pending_gate"])
        audit = {
            "actor": actor,
            "decision": decision,
            "gate": gate,
            "at": _now(),
            "supplemented": bool(supplement),
            "human_override": bool(human_override),
        }
        if human_override:
            audit["override_reason"] = str(override_reason or "人工批准覆盖数据 Gate")
        state["approvals"].append(audit)
        state["trace"].append({"event": "gate.decided", "decision": decision, "actor": actor, "step_index": gate["step_index"], "at": audit["at"]})
        state["pending_gate"] = None
        if decision in {"reject", "stop"}:
            state["status"] = "failed"
            state["response"] = "人工拒绝，流程已终止并保留审计记录。"
            return self._save(state)
        if gate.get("pre_execution"):
            state["authorized_steps"].append(state["plan"][int(gate["step_index"])]["id"])
            state["status"] = "running"
            return self._save(state)
        record = state["steps"][-1]
        if supplement or decision == "retry":
            if supplement:
                state["request"].update(supplement)
            record["status"] = "superseded"
            state["current_result"] = {}
            state["status"] = "running"
            return self._save(state)
        record["status"] = "completed"
        try:
            self._apply_approval(state, gate, actor=actor)
        except ValueError:
            # 冲突（如 HEAD_CONFLICT/STALE_REVISION）：先把 waiting_human +
            # pending_gate 恢复结果持久化，再向上传播，调用方返回可恢复错误。
            self._save(state)
            raise
        state["next_step_index"] = int(gate["step_index"]) + 1
        state["current_result"] = {}
        state["status"] = "running"
        return self._save(state)

    async def resume(
        self,
        state: RunState,
        decision: str,
        supplement: dict[str, Any] | None = None,
        *,
        actor: str = "operator",
        human_override: bool = False,
        override_reason: str = "",
    ) -> RunState:
        state = self._prepare_resume(
            state,
            decision,
            supplement,
            actor=actor,
            human_override=human_override,
            override_reason=override_reason,
        )
        return state if state.get("status") != "running" else await self.run(state)

    async def stream_resume(
        self,
        state: RunState,
        decision: str,
        supplement: dict[str, Any] | None = None,
        *,
        actor: str = "operator",
        human_override: bool = False,
        override_reason: str = "",
    ):
        state = self._prepare_resume(
            state,
            decision,
            supplement,
            actor=actor,
            human_override=human_override,
            override_reason=override_reason,
        )
        if state.get("status") == "running":
            async for event in self.stream(state):
                yield event
            return
        yield self._event(state, "state_snapshot", state=self._public_state(state))
        yield self._event(state, "run_done", state=self._public_state(state))

    def _apply_approval(self, state: RunState, gate: dict[str, Any], *, actor: str = "operator") -> dict[str, Any]:
        """应用人工批准的结果。

        - engineering Gate：把 M2 BOM/SOP 草稿标为 approved（供桥接消费）。
        - review Gate：接受 M1 复核结果。
        - apply Gate：调用真实 M5 repository 执行 approved/released + head
          CAS（T4.4 同事务），成功并回读 lifecycle/head 后才把步骤置为
          completed；repository 未配置时保持 preview 兼容路径并显式标记
          fixture，绝不冒充生产已发布。

        返回 ``{"applied": bool, "message": str, "readback": dict | None}``。
        失败（如 HEAD_CONFLICT）抛 ValueError，resume 层保持 waiting_human。
        """
        result = state["outputs"].get(gate["tool"], {})
        data = result_data(result)
        if gate["type"] == "engineering":
            # HTTP tools are wrapped in the contract envelope, so the M2
            # draft may live under ``result.data`` while local handlers return
            # the payload at the top level.  Persist the approval marker in
            # the same object consumed by orchestration_bridge.read_approved_bom
            # instead of only annotating the outer wrapper.
            generation = data.setdefault("bom_generation", {})
            generation["approval_status"] = "approved"
            sop_generation = data.get("sop_generation")
            if isinstance(sop_generation, dict):
                sop_generation["approval_status"] = "approved"
            result["data"] = data
            return {
                "applied": True,
                "message": "M2 BOM/SOP 已批准",
                "readback": {
                    "bom_rows": len(generation.get("bom_lines") or []),
                    "route_operations": len(sop_generation.get("route_steps") or []) if isinstance(sop_generation, dict) else 0,
                    "approval_status": generation.get("approval_status"),
                },
            }
        if gate["type"] == "review":
            result["review_status"] = "accepted"
            return {"applied": True, "message": "M1 复核已接受", "readback": None}
        if gate["type"] == "candidate":
            # Candidate approval is the durable hand-off from the upload
            # catalog to M0 canonical data.  The HTTP client is a no-op in
            # local/unit-test configurations without M0_URL.
            records = result.get("m0_candidate_records") if isinstance(result, dict) else None
            if isinstance(records, list) and records:
                from .m0_catalog import publish_records

                approval = next(
                    (
                        item for item in reversed(state.get("approvals", []))
                        if item.get("gate", {}).get("type") == "candidate"
                    ),
                    {},
                )
                publication = publish_records(
                    records,
                    tenant_id=str(state.get("tenant_id") or "default"),
                    task_id=str(state.get("task_id") or "task"),
                    actor=actor,
                    human_override=bool(approval.get("human_override")),
                    override_reason=str(approval.get("override_reason") or ""),
                )
                result["m0_catalog_publish"] = publication
                if publication.get("status") == "failed":
                    raise ValueError(f"M0 canonical publish failed: {publication.get('error')}")
            else:
                result["m0_catalog_publish"] = {"status": "needs_mapping", "published": 0, "reason": "缺少显式 product_code 或可发布候选"}
            # 本地 sandbox：candidate Gate 批准即视为该批次候选已裁决，把未裁决候选
            # 标记为 approved，对齐 HTTP M0 行为，避免 data_import_commit 因
            # "候选未裁决"被拒（本地 sandbox 需要显式 resolve，生产 M0 在候选批准时已裁决）。
            batch_id = str(data.get("batch_id") or data.get("id") or "")
            if batch_id:
                try:
                    import os as _os

                    from .m0_sandbox import M0SandboxStore

                    db_path = _os.getenv("YUNPAI_M0_SANDBOX_DB") or "runtime/yunpai-m0-sandbox.sqlite"
                    store = M0SandboxStore(db_path)
                    for doc in (store.preview(batch_id).get("documents") or []):
                        if doc.get("review_status") not in ("approved", "rejected"):
                            store.resolve(batch_id=batch_id, candidate_id=doc.get("candidate_id"), action="approve", actor=actor)
                except Exception:
                    # 本地 store 无该批次/候选时忽略，保持现有 publish 结果。
                    pass
            return {"applied": True, "message": "业务资料候选已审核并提交 M0 canonical", "readback": result.get("m0_catalog_publish")}
        if gate["type"] == "apply":
            return self._apply_m5_release(state, gate, result, data, actor=actor)
        return {"applied": True, "message": "批准已记录", "readback": None}

    def _apply_m5_release(self, state: RunState, gate: dict[str, Any], result: dict[str, Any],
                          data: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """把 M5 draft 计划真实发布到 repository（approved -> released + head CAS）。

        幂等/重放语义：同 plan_version 已 released 且同 head 时直接回读成功
        视作已应用；已 released 但场景 head 已被其他计划占用时抛 HEAD_CONFLICT。
        """
        import os

        plan_version = data.get("plan_version") or ""
        scenario_id = data.get("scenario_id") or (data.get("schedule") or {}).get("scenario_id") or ""
        db_path = state.get("request", {}).get("m5_db_path") or os.getenv("YUNPAI_M5_DB")
        preview = bool(state.get("request", {}).get("legacy_preview")) or not db_path
        if not plan_version:
            # 未持久化计划（legacy/preview 求解未启用 repository）：无法真实发布。
            if preview:
                data["lifecycle_status"] = "released"
                data["_release_scope"] = "preview_runstate_only"
                return {"applied": True, "message": "preview 路径：RunState 标记 released（未触碰 M5 repository）", "readback": None}
            raise ValueError(f"apply gate: 缺少已持久化 plan_version（tool={gate['tool']}），拒绝只改 RunState JSON")
        if preview and not db_path:
            data["lifecycle_status"] = "released"
            data["_release_scope"] = "preview_runstate_only"
            return {"applied": True, "message": "preview 路径：RunState 标记 released（未触碰 M5 repository）", "readback": None}
        from .m5_repository import M5Repository, M5RepositoryError
        from .orchestration_bridge import BRIDGED_WORKFLOWS

        repo = M5Repository(db_path)
        request = state.get("request", {})
        expected_head = request.get("expected_head_revision")
        expected_head = int(expected_head) if expected_head is not None and str(expected_head).isdigit() else None
        head = repo.get_head(scenario_id) if scenario_id else None
        if head is None and expected_head is None:
            expected_head = 0  # 首次发布场景 head 期望为 0（CAS 防并发覆盖）
        try:
            apply_result = repo.apply_release(
                plan_version, scenario_id,
                tenant_id=state.get("tenant_id", "default"),
                gate="apply",
                actor=str(actor or ""),
                task_id=state.get("task_id", ""),
                trace_id=f"{state.get('task_id', 'task')}:apply",
                expected_head_revision=expected_head,
            )
            readback = repo.readback_after_release(plan_version, scenario_id)
        except M5RepositoryError as exc:
            # 幂等重放：同计划已是 released（previous apply）且 head 指向它 => 已应用。
            if exc.code == "PLAN_PROTECTED":
                existing = repo.get_plan(plan_version)
                if existing and existing.get("lifecycle_status") == "released":
                    current_head = repo.get_head(scenario_id) if scenario_id else None
                    if current_head and current_head.get("head_plan_version") == plan_version:
                        readback = repo.readback_after_release(plan_version, scenario_id)
                        data.update(lifecycle_status="released", head_revision=readback.get("head_revision"))
                        return {"applied": True, "message": "apply 幂等：计划已 released 且 head 一致", "readback": readback}
            # 其它冲突保持 waiting_human，可恢复（Taskbook T4.8）。
            state["pending_gate"] = {**gate, "step_index": gate.get("step_index", 0), "conflict": {"code": exc.code, "message": exc.message}, "reopened_at": _now()}
            state["status"] = "waiting_human"
            raise ValueError(f"apply gate conflict ({exc.code}): {exc.message}") from exc
        data.update({
            "lifecycle_status": "released",
            "plan_version": apply_result["plan_version"],
            "head_revision": apply_result.get("head_revision"),
            "released_at": readback.get("released_at") if isinstance(readback.get("released_at"), str) else None,
        })
        result["evidence"] = [*result.get("evidence", []), {
            "module": "m5", "source_ref": apply_result["plan_version"],
            "evidence_ref": f"m5:{apply_result['plan_version']}:release",
            "detail": f"Apply Gate 真实发布：lifecycle=released, head revision={apply_result.get('head_revision')}",
        }]
        state["trace"].append({"event": "m5.released", "plan_version": plan_version, "head_revision": apply_result.get("head_revision"), "scenario_id": scenario_id, "at": _now()})
        return {"applied": True, "message": f"M5 计划已真实发布（head revision={apply_result.get('head_revision')}）", "readback": readback}

    def _payload_for(self, state: RunState, tool: str) -> dict[str, Any]:
        request, outputs = state["request"], state.get("outputs", {})
        explicit = request.get("payloads", {}).get(tool)
        if isinstance(explicit, dict):
            return explicit
        if tool == "data_import_run":
            files = []
            documents = request.get("documents") or request.get("files", [])
            if not documents:
                documents = [
                    item for item in request.get("attachments", [])
                    if isinstance(item, dict) and item.get("kind") in {"order", "master_data"}
                ]
            for index, document in enumerate(documents):
                if isinstance(document, dict) and "content_b64" in document:
                    files.append(document)
                else:
                    filename = str(document.get("filename", f"document-{index}.json")) if isinstance(document, dict) else f"document-{index}.json"
                    files.append(_file_object(filename, document))
            return {"files": files}
        if tool == "business-data-identification":
            upload_mode = str(request.get("upload_mode") or request.get("mode") or "master_data")
            return {
                "root_path": request.get("business_data_root") or request.get("root_path"),
                "files": request.get("documents") or request.get("attachments") or [],
                "db_path": request.get("business_catalog_db") or "runtime/yunpai-business-catalog.sqlite",
                "mode": upload_mode,
                "product_code": request.get("product_code") or "",
                "product_name": request.get("product_name") or "",
                "message": request.get("message") or request.get("task") or "",
            }
        if tool in self.skills.specs:
            skill_payload = request.get("skill_payload")
            if isinstance(skill_payload, dict):
                return skill_payload
            return {
                key: value
                for key, value in request.items()
                if key not in {"message", "task", "skill", "payloads", "workflow"}
            }
        if tool == "data_import_commit":
            imported = outputs.get("data_import_run", {})
            payload = {"batch_id": _batch_id_from_result(imported) or ""}
            approval = next(
                (
                    item for item in reversed(state.get("approvals", []))
                    if item.get("human_override")
                ),
                {},
            )
            if approval:
                payload.update({
                    "human_override": True,
                    "approved_by": str(approval.get("actor") or ""),
                    "override_reason": str(approval.get("override_reason") or ""),
                })
            return payload
        if tool == "data_import_preview":
            imported = outputs.get("data_import_run", {})
            return {"batch_id": request.get("batch_id") or _batch_id_from_result(imported) or ""}
        if tool == "data_import_resolve":
            imported = outputs.get("data_import_run", {})
            kind = request.get("kind")
            action = request.get("action")
            if kind not in {"entity", "mapping", "field"}:
                raise ValueError("data_import_resolve 需要显式 kind(entity|mapping|field)，禁止伪造裁决")
            if action not in {"approve", "reject"}:
                raise ValueError("data_import_resolve 需要显式 action(approve|reject)，禁止伪造裁决")
            return {
                "batch_id": request.get("batch_id") or _batch_id_from_result(imported) or "",
                "kind": kind, "id": request.get("id", 0), "action": action,
            }
        if tool == "ingest_document":
            document = request.get("document") or request.get("order") or {}
            order_attachment = next(
                (
                    item for item in request.get("attachments", [])
                    if isinstance(item, dict) and item.get("kind") == "order"
                ),
                None,
            )
            if order_attachment and isinstance(order_attachment.get("content_b64"), str) and order_attachment.get("content_b64"):
                # The browser stream path submits the XLSX as an attachment and
                # does not call /runs/upload, so derive the M1 fixture here.
                if not document and isinstance(order_attachment.get("content_b64"), str):
                    try:
                        raw = base64.b64decode(order_attachment["content_b64"])
                        from .order_workbook import parse_order_workbook
                        document = parse_order_workbook(str(order_attachment.get("filename") or "order.xlsx"), raw)
                    except (ValueError, RuntimeError):
                        document = {}
                return {"file": order_attachment, "_fixture_document": document}
            if order_attachment and not order_attachment.get("content_b64"):
                # Metadata-only document references cannot be sent as a
                # multipart upload. Keep the request explicit instead of
                # emitting a malformed file field that the M1 service rejects.
                return {"file": _file_object("order.json", document), "_fixture_document": document}
            return {"file": _file_object("order.json", document), "_fixture_document": document}
        if tool == "run_bom_sop_workflow":
            product = dict(request.get("product") or {})
            m1_result = result_data(outputs.get("ingest_document", {}))
            m1_document = m1_result.get("document") if isinstance(m1_result.get("document"), dict) else {}
            m1_header = m1_document.get("header") if isinstance(m1_document.get("header"), dict) else {}
            m1_lines = m1_document.get("lines") if isinstance(m1_document.get("lines"), list) else []
            first_line = next((line for line in m1_lines if isinstance(line, dict)), {})
            # M1 may not be able to assign a stable product code for a
            # customer order.  Preserve that fact, but provide M2 with the
            # extracted product name so its own contract can evaluate the
            # candidate instead of failing on an empty required field.
            product.setdefault(
                "product_name",
                str(
                    product.get("product_code")
                    or first_line.get("full_product_name")
                    or first_line.get("name_normalized")
                    or first_line.get("name_raw")
                    or m1_header.get("title")
                    or ""
                ),
            )
            bom_lines = request.get("bom_lines", []) or []
            bom_items = [
                {
                    "item_no": str(line.get("line_id") or index),
                    "material_code": str(line.get("material_code") or ""),
                    "name": str(line.get("material_name") or line.get("material_code") or "未命名物料"),
                    "specification": str(line.get("specification") or ""),
                    "quantity": str(line.get("quantity_per", line.get("quantity", ""))),
                    "note": str(line.get("note") or ""),
                }
                for index, line in enumerate(bom_lines, start=1)
                if isinstance(line, dict)
            ]
            routing_steps = []
            for item in (request.get("routing_steps", []) or []):
                if not isinstance(item, dict):
                    continue
                if "standard_time" in item:
                    standard_time_s = float(item.get("standard_time") or 0)
                elif "standard_time_s" in item:
                    standard_time_s = float(item.get("standard_time_s") or 0)
                else:
                    standard_time_s = float(item.get("processing_minutes") or 0) * 60
                routing_steps.append({
                    "name": str(item.get("name") or item.get("operation_name") or item.get("operation_id") or "未命名工序"),
                    "description": str(item.get("description") or ""),
                    "station": str(item.get("station") or item.get("station_code") or ""),
                    # M2's HTTP contract names this value `standard_time` and
                    # interprets it as seconds; retain the internal alias too.
                    "standard_time": standard_time_s,
                    "standard_time_s": standard_time_s,
                })
            attachments = [
                item for item in request.get("attachments", [])
                if isinstance(item, dict) and item.get("kind") == "master_data"
            ]
            return {
                "product_profile": product,
                "bom_lines": bom_lines,
                "bom_items": bom_items,
                "routing_steps": routing_steps,
                "requirement_text": str(request.get("requirement_text") or request.get("message") or ""),
                "rule_package_path": str(request.get("rule_package_path") or os.getenv("YUNPAI_RULE_PACKAGE_PATH", "runtime/rule_packages/material_numbering")),
                "document_no": str(request.get("document_no") or m1_header.get("order_number") or product.get("product_code") or "M2-DRAFT"),
                "history_bom_paths": request.get("history_bom_paths") or [],
                "history_sop_paths": request.get("history_sop_paths") or [],
                "template_confirmation": request.get("template_confirmation") or {"confirmed": False},
                "customer_answers": request.get("customer_answers") or {},
                "machine_hints": request.get("machine_hints") or [],
                "station": str(request.get("station") or ""),
                "enable_bom_model": bool(request.get("enable_bom_model", False)),
                "enable_sop_model": bool(request.get("enable_sop_model", False)),
                "bom_files": request.get("bom_files") or attachments,
                "sop_files": request.get("sop_files") or attachments,
                "use_demo_sources": False,
            }
        if tool == "run_m3_procurement_requirements":
            m1 = outputs.get("ingest_document", {})
            order = dict(m1.get("order") or m1.get("extraction", {}).get("order") or request.get("order") or {})
            m2 = outputs.get("run_bom_sop_workflow", {})
            bom_lines = m2.get("bom_generation", {}).get("bom_lines") or request.get("bom_lines", [])
            product_name = (request.get("product") or {}).get("product_name") or order.get("product_code") or ""
            bom_id = str(request.get("bom_id") or f"BOM-{order.get('product_code', '')}")
            payload = {
                "tenant_id": state.get("tenant_id", "default"),
                "order": {"project_id": str(request.get("project_id") or order.get("order_id") or ""), "order_id": str(order.get("order_id") or ""), "bom_id": bom_id, "product_name": product_name, "order_qty": order.get("quantity", 0), "due_date": str(order.get("due_date") or "")},
                "bom": {"bom_id": bom_id, "product_name": product_name, "lines": [{"line_id": str(line.get("line_id") or f"line-{index}"), "material_code": str(line.get("material_code") or ""), "material_name": str(line.get("material_name") or line.get("material_code") or ""), "qty_per": line.get("qty_per", line.get("quantity_per", line.get("quantity", 0))), "uom": str(line.get("uom") or line.get("unit") or "pcs"), "loss_rate": line.get("loss_rate", 0), "requires_procurement": line.get("requires_procurement", True)} for index, line in enumerate(bom_lines, start=1)]},
                "inventory_snapshot": [{"material_code": str(item.get("material_code") or ""), "material_name": str(item.get("material_name") or item.get("material_code") or ""), "warehouse": str(item.get("warehouse") or "local-fixture"), "lot_no": str(item.get("lot_no") or f"lot-{index}"), "available_qty": item.get("available_qty", item.get("quantity", 0)), "locked_qty": item.get("locked_qty", 0), "qc_status": str(item.get("qc_status") or "released"), "received_at": str(item.get("received_at") or _now())} for index, item in enumerate(request.get("inventory", []), start=1)],
            }
            if not bom_lines:
                # The M3 contract requires one BOM line for calculation. Use
                # its compatibility package so the worker can return a
                # contract-valid BLOCKED_INPUT business gate instead of a
                # schema-validation/tool error.
                payload.pop("bom")
                payload["m2_package"] = {}
            return payload
        if tool == "import_m4_purchase_suggestions_json":
            data = result_data(outputs.get("run_m3_procurement_requirements", {}))
            suppliers = request.get("supplier_by_material") or {}
            suggestions = [{"item_code": line.get("material_code"), "item_name": line.get("material_name"), "quantity": line.get("suggest_purchase_qty", line.get("shortage_qty", 0)), "unit": line.get("uom", "pcs"), "supplier_name": suppliers.get(line.get("material_code"), ""), "required_date": data.get("due_date", ""), "project_code": data.get("project_id", "")} for line in data.get("shortage_lines", [])]
            return {"suggestions": suggestions, "tenant_id": state.get("tenant_id", "default"), "site_id": str(request.get("site_id") or "default"), "tracking_task_id": state["task_id"], "idempotency_key": f"{state['task_id']}:m4", "source_module": "m3", "procurement_plan_id": data.get("procurement_plan_id"), "order_id": data.get("order_id")}
        if tool == "solve_scheduling":
            m1 = outputs.get("ingest_document", {})
            order = dict(m1.get("order") or request.get("order") or request.get("document") or {})
            product_id = str(order.get("product_code") or (request.get("product") or {}).get("product_code") or "")
            resources = [{**item, "name": item.get("name") or item.get("resource_id")} for item in request.get("resources", [])]
            resource_ids = [item["resource_id"] for item in resources]
            routes = []
            for item in request.get("routing_steps", []):
                eligible = item.get("eligible_resources") or [{"resource_id": resource_id, "processing_minutes": max(1, int(item.get("processing_minutes", 1)))} for resource_id in resource_ids[:1]]
                routes.append({**item, "product_id": item.get("product_id") or product_id, "operation_name": item.get("operation_name") or item.get("operation_id"), "eligible_resources": eligible})
            advanced = {key: request[key] for key in ("pmc_v2", "pmc_v2_bundle", "wip_pmc", "wip_pmc_mode", "production_use_allowed", "calendar_windows", "calendar", "resource_snapshot", "resource_unavailability", "supply_entries", "wip_status", "material_availability", "changeover_rules", "setup_matrix", "route_approval_ref", "route_version", "route_code", "legacy_preview") if key in request}
            # M1 order workbooks may contain multiple product lines. Preserve
            # those lines as separate M5 orders so routing and capacity are not
            # silently reduced to the first header product.
            lines = order.get("lines") or m1.get("lines") or []
            if isinstance(lines, list) and lines:
                orders = [
                    {
                        "order_id": str(order.get("order_id") or ""),
                        "order_line_id": str(line.get("line_id") or f"{order.get('order_id')}::L{index}"),
                        "product_id": str(line.get("product_code") or line.get("model") or product_id),
                        "quantity": line.get("quantity", 0),
                        "due_time": _due_time(line.get("due_date") or order.get("due_date")),
                        "priority": request.get("priority", "normal"),
                        "status": "firm",
                    }
                    for index, line in enumerate(lines, start=1)
                    if isinstance(line, dict) and (line.get("product_code") or line.get("model"))
                ]
            else:
                orders = [{"order_id": str(order.get("order_id") or ""), "product_id": product_id, "quantity": order.get("quantity", 0), "due_time": _due_time(order.get("due_date")), "priority": request.get("priority", "normal"), "status": "firm"}]
            return {"idempotency_key": f"{state['task_id']}:m5", "scenario_id": str(request.get("scenario_id") or f"scenario-{order.get('order_id', '')}"), "scenario_purpose": request.get("scenario_purpose", "production"), "planning_start": str(request.get("planning_start") or _now()), "orders": orders, "routing_steps": routes, "resources": resources, "source_systems": ["manual"], **advanced}
        return request.get(tool, {}) if isinstance(request.get(tool), dict) else {}


def build_graph(registry: ToolRegistry | None = None, repository: RunRepository | None = None):
    app = YunpaiGraph(registry, repository)
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        return CompatGraph(app.run)
    builder = StateGraph(RunState)
    builder.add_node("planner", app.planner_node)
    builder.add_node("worker", app.worker_node)
    builder.add_node("reviewer", app.reviewer_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", app.route_after_planner, {"worker": "worker", "end": END})
    builder.add_edge("worker", "reviewer")
    builder.add_conditional_edges("reviewer", app.route_after_reviewer, {"worker": "worker", "end": END})
    return builder.compile()


async def invoke(request: dict[str, Any], *, tenant_id: str = "default", registry: ToolRegistry | None = None, repository: RunRepository | None = None) -> RunState:
    state = new_state(request, tenant_id=tenant_id)
    return await YunpaiGraph(registry, repository).run(state)

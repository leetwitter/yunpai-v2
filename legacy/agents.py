from __future__ import annotations

import base64
import os
import re
from typing import Any

from .models import RunState, summarize
from .registry import ToolRegistry
from .workflow_registry import KNOWN_WORKFLOWS, load_workflow
from .llm import QwenRouter
from .skills import SkillRegistry, build_default_skill_registry
from .m3_m4_tooling import M3_READ_ONLY_SKILL_OPERATIONS, M4_READ_ONLY_SKILL_OPERATIONS
from .m1_tooling import M1_READ_ONLY_SKILL_OPERATIONS


INTENT_TO_TOOL = (
    (("导入", "入库", "基础数据"), "data_import_run"),
    (("识别", "解析", "ocr", "图纸"), "ingest_document"),
    (("bom", "sop", "工艺"), "run_bom_sop_workflow"),
    (("mrp", "缺料", "物料需求", "齐套"), "run_m3_procurement_requirements"),
    (("采购单", "采购建议", "供应商"), "import_m4_purchase_suggestions_json"),
    (("排程", "pmc", "计划"), "solve_scheduling"),
)

BUSINESS_DATA_SKILL = "business-data-identification"

# 任务书 5.1 的使用顺序：高阶 Skill 多步提案必须满足此偏序。
SKILL_USAGE_ORDER = (
    "business-data-identification",
    "yunpai-m1-document-parser",
    "yunpai-m0-data-foundation",
    "yunpai-m2-bom-sop",
    "yunpai-m3-material-planning",
    "yunpai-m4-procurement",
    "yunpai-m5-pmc",
    "yunpai-m5-pmc-lifecycle",
)

INTENT_TO_SKILL = (
    (("主数据治理", "canonical", "资料治理"), "yunpai-m0-data-foundation"),
    (("文档智能", "文档审核", "解析报告"), "yunpai-m1-document-parser"),
    (("工程控制", "bom审核", "sop审核"), "yunpai-m2-bom-sop"),
    (("物料齐套", "物料计划", "mrp分析"), "yunpai-m3-material-planning"),
    (("采购跟踪", "供应商跟踪", "采购预警"), "yunpai-m4-procurement"),
    (("排程求解", "智能排程", "排程检查", "排程就绪", "资源负载"), "yunpai-m5-pmc"),
    (("排程生命周期", "排程版本", "生产执行", "派工", "报工", "执行回传", "flow board"), "yunpai-m5-pmc-lifecycle"),
)


class PlannerAgent:
    """总规划 Agent：只做意图、路径和依赖规划，不执行工具。"""

    def __init__(self, router: QwenRouter | None = None, skills: SkillRegistry | None = None) -> None:
        self.router = router or QwenRouter()
        self.skills = skills or build_default_skill_registry()
        # QwenRouter 构建 prompt 时读取同一 Skill catalog（含版本），模型只做提案。
        try:
            self.router.skills = self.skills  # type: ignore[attr-defined]
        except Exception:
            pass

    def plan(self, request: dict[str, Any], registry: ToolRegistry) -> dict[str, Any]:
        return self._deterministic_plan(request, registry, self.skills)

    async def aplan(self, request: dict[str, Any], registry: ToolRegistry) -> dict[str, Any]:
        """Ask Qwen for an intent/route proposal, then validate it against local contracts."""
        fallback = self._deterministic_plan(request, registry, self.skills)
        explicit_workflow = str(request.get("workflow") or "").strip()
        # 无显式 workflow/tool/skill 且带非订单文件附件时，走 agent 识别分支：
        # sample_file 采样 → map_to_canonical（多模态理解映射）→ ingest_canonical 落库。
        # agent 不可用/低置信/映射失败时返回 None，回落到 classify + 确定性兜底
        # （business_catalog 写死规则，仅作兜底）。
        has_explicit_route = bool(
            request.get("workflow") or request.get("tool") or request.get("skill")
            or isinstance(request.get("tools"), list)
        )
        if explicit_workflow not in KNOWN_WORKFLOWS and not has_explicit_route and self._has_unrouted_file(request):
            recognition = await self._agent_recognize_plan(request, registry)
            if recognition is not None:
                return recognition
        model_result = await self.router.classify(request, registry)
        # An explicit workflow selection is authoritative. Attachments with
        # kind=master_data must not divert an end-to-end run to the business
        # data identification Skill.
        if explicit_workflow in KNOWN_WORKFLOWS:
            return {
                **fallback,
                "intent": {"name": "workflow", "confidence": 1.0, "source": "explicit"},
                "route_decision": {
                    "source": "explicit",
                    "model_status": model_result.get("status"),
                    "model_error": model_result.get("error"),
                },
                "model": model_result.get("model", {}),
            }
        explicit = bool(request.get("workflow") or request.get("tool") or isinstance(request.get("tools"), list))
        decision = model_result.get("decision") if model_result.get("ok") else None
        if explicit or not isinstance(decision, dict):
            source = "explicit" if explicit else "deterministic_fallback"
            intent = {"name": fallback.get("route", "chat"), "confidence": 1.0 if explicit else 0.0, "source": source}
            return {**fallback, "intent": intent, "route_decision": {"source": source, "model_status": model_result.get("status"), "model_error": model_result.get("error")}, "model": model_result.get("model", {})}
        has_unparsed_order_attachment = (
            not request.get("document")
            and any(isinstance(item, dict) and item.get("kind") == "order" for item in request.get("attachments", []))
        )
        if decision.get("route") == "free" and has_unparsed_order_attachment and any(
            tool in {"data_import_preview", "data_import_resolve"} for tool in decision.get("tools", [])
        ):
            return {
                **fallback,
                "intent": {"name": decision.get("intent", "订单解析"), "confidence": decision.get("confidence", 0.0), "source": "qwen_rejected"},
                "route_decision": {
                    "source": "deterministic_fallback",
                    "model_status": "invalid_attachment_plan",
                    "model_proposal": decision,
                    "fallback_reason": "订单附件尚未生成 M0 batch，无法调用 preview/resolve",
                },
                "model": model_result.get("model", {}),
            }
        proposed = self._model_plan(decision, registry, self.skills)
        if isinstance(proposed, dict) and proposed.get("_invalid_reason"):
            invalid_reason = str(proposed["_invalid_reason"])
            return {**fallback, "intent": {"name": decision.get("intent", "unknown"), "confidence": decision.get("confidence", 0.0), "source": "qwen_rejected"}, "route_decision": {"source": "deterministic_fallback", "model_status": "invalid_decision", "model_proposal": decision, "reject_reason": invalid_reason}, "model": model_result.get("model", {})}
        if proposed is None:
            return {**fallback, "intent": {"name": decision.get("intent", "unknown"), "confidence": decision.get("confidence", 0.0), "source": "qwen_rejected"}, "route_decision": {"source": "deterministic_fallback", "model_status": "invalid_decision", "model_proposal": decision}, "model": model_result.get("model", {})}
        if proposed.get("route") == "chat" and not proposed.get("response"):
            proposed["response"] = self._chat_fallback(str(request.get("message") or request.get("task") or "").lower())
        return {**proposed, "intent": {"name": decision["intent"], "confidence": decision["confidence"], "source": "qwen"}, "route_decision": {"source": "qwen", "model_status": model_result.get("status"), "model_proposal": decision}, "model": model_result.get("model", {})}

    @staticmethod
    def _model_plan(decision: dict[str, Any], registry: ToolRegistry, skills: SkillRegistry | None = None) -> dict[str, Any] | None:
        route = decision.get("route")
        if route == "chat":
            return {"route": "chat", "steps": [], "reason": decision.get("reason") or "Qwen 判定为解释性对话", "response": decision.get("answer") or ""}
        if route == "workflow":
            workflow_id = str(decision.get("workflow") or decision.get("workflow_id") or "m1_m5_document_to_plan")
            if workflow_id not in KNOWN_WORKFLOWS:
                return {"_invalid_reason": f"模型提案含未知 workflow: {workflow_id}"}
            workflow = load_workflow(workflow_id)
            return {"route": "workflow", "steps": [{**step, "mode": "workflow"} for step in workflow["steps"]], "workflow_id": workflow["workflow_id"], "workflow_version": workflow["version"], "reason": decision.get("reason") or f"Qwen 判定为受控业务目标 ({workflow_id})"}
        if route == "free":
            requested_tools = [str(name) for name in decision.get("tools", [])]
            requested_skills = [str(name) for name in decision.get("skills", [])] if skills else []
            invalid_tools = [name for name in requested_tools if name not in registry.specs]
            invalid_skills = [name for name in requested_skills if skills is None or name not in skills.specs]
            if invalid_tools or invalid_skills:
                return {"_invalid_reason": f"模型提案含未注册工具或 Skill: tools={invalid_tools} skills={invalid_skills}"}
            tools = [name for name in requested_tools if name in registry.handlers]
            if any(name not in registry.handlers for name in requested_tools):
                return {"_invalid_reason": "模型提案包含未绑定 handler 的工具，拒绝执行"}
            skill_names = [name for name in requested_skills if skills and name in skills.specs]
            if not tools and not skill_names:
                return None
            if not PlannerAgent._respects_skill_order(skill_names):
                return {"_invalid_reason": f"Skill 提案违反任务书使用顺序: {skill_names}"}
            steps = [{"id": f"free-{index}", "module": registry.specs[name].module, "tool": name, "mode": "free", "http_method": registry.specs[name].method} for index, name in enumerate(tools)]
            steps.extend({"id": f"skill-{index}", "module": "orchestrator", "tool": name, "kind": "skill", "mode": "free"} for index, name in enumerate(skill_names, start=len(steps)))
            return {"route": "free", "steps": steps, "reason": decision.get("reason") or "Qwen 路由到自由工具/Skill 路径"}
        return None

    @staticmethod
    def _respects_skill_order(skill_names: list[str]) -> bool:
        """多个高阶 Skill 提案必须满足 SKILL_USAGE_ORDER 偏序；单 Skill 恒通过。"""
        if len(skill_names) <= 1:
            return True
        positions = [SKILL_USAGE_ORDER.index(name) for name in skill_names if name in SKILL_USAGE_ORDER]
        return positions == sorted(positions)

    @staticmethod
    def _has_unrouted_file(request: dict[str, Any]) -> bool:
        """是否带「非订单、有 content_b64」的文件附件（订单走确定性主链，不识别）。"""
        files = list(request.get("documents") or []) + list(request.get("attachments") or [])
        contentful = [item for item in files if isinstance(item, dict) and item.get("content_b64")]
        if not contentful:
            return False
        return any(str(item.get("kind") or "") != "order" for item in contentful)

    async def _agent_recognize_plan(self, request: dict[str, Any], registry: ToolRegistry) -> dict[str, Any] | None:
        """采样 + map_to_canonical（多模态理解映射）→ ingest_canonical 落库编排。

        全程 agent 理解自主决策，不碰 business_catalog 写死规则；agent 不可用/
        低置信/映射失败时返回 None 交给确定性兜底。
        """
        from .recognized_store import sample_file

        files = [
            item for item in list(request.get("documents") or []) + list(request.get("attachments") or [])
            if isinstance(item, dict) and item.get("content_b64") and str(item.get("kind") or "") != "order"
        ]
        if not files:
            return None
        target = files[0]
        try:
            raw = base64.b64decode(str(target["content_b64"]), validate=True)
        except (ValueError, TypeError):
            return None
        max_sheets = int(os.getenv("YUNPAI_SAMPLE_MAX_SHEETS", "6"))
        sample = sample_file(raw, str(target.get("filename") or "document.bin"), max_sheets=max_sheets)
        if not sample.get("content_sampled"):
            return None
        result = await self.router.map_to_canonical(sample)
        decision = result.get("decision") if result.get("ok") else None
        if not isinstance(decision, dict):
            return None
        entity_type = str(decision.get("entity_type") or "")
        records = decision.get("records")
        confidence = float(decision.get("confidence") or 0.0)
        threshold = float(os.getenv("YUNPAI_RECOGNITION_CONFIDENCE_THRESHOLD", "0.7"))
        needs_review = bool(decision.get("needs_review")) or confidence < threshold
        if not entity_type or not isinstance(records, list) or not records or needs_review:
            return None
        # 两段式（仅 BOM）：LLM 识别为 bom 后，用确定性抽取器 extract_bom_full
        # 遍历全部产品 sheet，抽出 材料行 + 型号(product_code) + 产品名(sheet 名)。
        # 其它类型保持 LLM 直接吐 records，不受影响。
        if entity_type == "bom":
            from .tabular_extract import extract_bom_full

            bom_result = extract_bom_full(raw, str(target.get("filename") or "document.bin"))
            bom_records = bom_result.get("records") or []
            if bom_records:
                records = bom_records
        request.setdefault("payloads", {})["ingest_canonical"] = {
            "entity_type": entity_type,
            "records": records,
            "filename": str(sample.get("filename") or target.get("filename") or ""),
            "sha256": str(target.get("sha256") or sample.get("sha256") or ""),
            "confidence": confidence,
        }
        steps = [{"id": "free-0", "module": "m0", "tool": "ingest_canonical", "mode": "free", "http_method": "POST"}]
        return {
            "route": "free",
            "steps": steps,
            "reason": f"agent 识别文件为 {entity_type}（confidence={confidence:.2f}）：{decision.get('reason') or ''}",
            "intent": {"name": f"识别:{entity_type}", "confidence": confidence, "source": "agent_recognition"},
            "route_decision": {"source": "agent_recognition", "model_status": result.get("status"), "model_proposal": decision},
            "model": result.get("model", {}),
        }

    def _deterministic_plan(self, request: dict[str, Any], registry: ToolRegistry, skills: SkillRegistry | None = None) -> dict[str, Any]:
        text = str(request.get("message") or request.get("task") or "").lower()
        # 显式受控 workflow 优先于文本语义猜测（例如 "canonical" 文本
        # 不应被误路由到 m0 Skill），保证 m1_m5_document_to_plan /
        # canonical_to_m5 能被可靠选择。
        explicit_workflow = str(request.get("workflow") or "").strip()
        if request.get("workflow") and explicit_workflow not in KNOWN_WORKFLOWS:
            return {
                "route": "chat",
                "steps": [],
                "reason": f"UNKNOWN_WORKFLOW: {explicit_workflow}",
                "error": {"code": "UNKNOWN_WORKFLOW", "workflow": explicit_workflow},
                "response": f"未注册的 workflow：{explicit_workflow}。请提供已注册的流程标识。",
            }
        if explicit_workflow in KNOWN_WORKFLOWS:
            workflow = load_workflow(explicit_workflow)
            steps = [{**step, "mode": "workflow"} for step in workflow["steps"]]
            return {"route": "workflow", "steps": steps, "workflow_id": workflow["workflow_id"], "workflow_version": workflow["version"], "reason": f"显式选择受控 workflow: {explicit_workflow}"}
        requested_skill = request.get("skill")
        if requested_skill:
            if not skills or requested_skill not in skills.specs:
                raise ValueError(f"未注册 Skill: {requested_skill}")
            return {"route": "free", "steps": [{"id": "skill-0", "module": "orchestrator", "tool": str(requested_skill), "kind": "skill", "mode": "free"}], "reason": f"显式选择已注册 Skill: {requested_skill}"}
        if self._business_skill_requested(request) and skills and BUSINESS_DATA_SKILL in skills.specs:
            return {"route": "free", "steps": [{"id": "skill-0", "module": "orchestrator", "tool": BUSINESS_DATA_SKILL, "kind": "skill", "mode": "free"}], "reason": "用户明确要求识别业务资料，选择资料识别 Skill"}
        if skills:
            for keywords, skill_name in INTENT_TO_SKILL:
                if skill_name in skills.specs and any(keyword in text for keyword in keywords):
                    return {"route": "free", "steps": [{"id": "skill-0", "module": "orchestrator", "tool": skill_name, "kind": "skill", "mode": "free"}], "reason": f"语义匹配高阶 Skill: {skill_name}"}
        full = request.get("workflow") == "m1_m5_document_to_plan" or "全链路" in text or all(word in text for word in ("订单", "采购", "排程"))
        if full:
            workflow = load_workflow("m1_m5_document_to_plan")
            steps = [{**step, "mode": "workflow"} for step in workflow["steps"]]
            return {"route": "workflow", "steps": steps, "workflow_id": workflow["workflow_id"], "workflow_version": workflow["version"], "reason": "识别为 M0→M5 受控业务目标"}
        requested_tools: list[str] = []
        if isinstance(request.get("tools"), list):
            requested_tools.extend(str(item) for item in request["tools"])
        if request.get("tool"):
            requested_tools.append(str(request["tool"]))
        if not requested_tools:
            exact = re.search(r"\b(" + "|".join(re.escape(name) for name in registry.specs) + r")\b", text)
            if exact:
                requested_tools.append(exact.group(1))
            else:
                for keywords, tool in INTENT_TO_TOOL:
                    if any(keyword in text for keyword in keywords):
                        requested_tools.append(tool)
                        break
        if requested_tools:
            unknown = [name for name in requested_tools if name not in registry.specs]
            if unknown:
                raise ValueError(f"未注册工具: {', '.join(unknown)}")
            return {
                "route": "free",
                "steps": [
                    {
                        "id": f"free-{index}", "module": registry.specs[name].module,
                        "tool": name, "mode": "free", "http_method": registry.specs[name].method,
                    }
                    for index, name in enumerate(requested_tools)
                ],
                "reason": "ReAct 路由到显式或语义匹配工具",
            }
        return {"route": "chat", "steps": [], "reason": "当前消息不需要业务工具", "response": self._chat_fallback(text)}

    @staticmethod
    def _chat_fallback(text: str) -> str:
        if any(word in text for word in ("能做什么", "功能", "怎么用", "帮助")):
            return "我可以协助处理订单、识别业务资料、解析订单字段、生成 BOM/SOP 草稿、计算物料需求、形成采购建议和安排生产排程。涉及数据发布、采购和排程生效时，会先进入人工确认 Gate。"
        if any(word in text for word in ("你好", "您好", "嗨")):
            return "你好，我是云湃制造业务 Agent。你可以直接描述目标，或上传订单、BOM、SOP 和其他业务资料。"
        return "我已收到你的问题。请补充具体的订单、物料、采购、工程或排程事项，我会给出处理建议或进入对应工作流。"

    @staticmethod
    def _business_skill_requested(request: dict[str, Any]) -> bool:
        text = str(request.get("message") or request.get("task") or "").lower()
        return any(keyword in text for keyword in ("基础资料", "业务资料", "业务数据", "资料识别", "识别并落库", "文件落库"))


class WorkerAgent:
    """Worker Agent：只能调用 ToolRegistry 中已注册并绑定的能力。"""

    def __init__(self, registry: ToolRegistry, skills: SkillRegistry | None = None) -> None:
        self.registry = registry
        self.skills = skills or build_default_skill_registry()
        self.skills.validate_tools(self.registry.specs)

    async def run(self, state: RunState, step: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        tool = step["tool"]
        if step.get("kind") == "skill":
            result = await self.skills.call(tool, payload, {"run_id": state["run_id"], "task_id": state["task_id"], "tenant_id": state.get("tenant_id"), "_tool_registry": self.registry})
            return {"module": "orchestrator", "tool": tool, "result": result, "input_summary": summarize(payload), "output_summary": summarize(result)}
        spec = self.registry.specs[tool]
        result = await self.registry.call(tool, payload, {
            "run_id": state["run_id"], "task_id": state["task_id"],
            "tenant_id": state.get("tenant_id"),
        })
        return {
            "module": spec.module, "tool": tool, "result": result,
            "input_summary": summarize(payload), "output_summary": summarize(result),
        }


def result_data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else result


class ReviewerAgent:
    """审查 Agent：执行确定性验证并决定继续、Gate 或失败关闭。"""

    # These tools only stage a candidate/draft and are reviewed immediately after execution.
    _POST_REVIEWED_DRAFT_TOOLS = {
        "data_import_run", "business-data-identification", "ingest_document", "run_bom_sop_workflow", "solve_scheduling",
    }
    # 本地识别四件套：只写自描述/本地 canonical 表（幂等/可回滚）或只读，无外部副作用，
    # 不做 pre-execution 授权门（低置信已在 planner 识别分支拦下）。
    _SAFE_LOCAL_TOOLS = {"sample_file", "ingest_recognized", "query_recognized_table", "ingest_canonical"}
    _READ_ONLY_SKILL_OPERATIONS = {
        "yunpai-m0-data-foundation": {"preview"},
        "yunpai-m1-document-parser": M1_READ_ONLY_SKILL_OPERATIONS,
        "yunpai-m3-material-planning": M3_READ_ONLY_SKILL_OPERATIONS,
        "yunpai-m4-procurement": M4_READ_ONLY_SKILL_OPERATIONS,
        # M5 PMC read-only queries (never mutate plan/snapshot state)
        "yunpai-m5-pmc": {"schedule", "progress", "versions", "readiness", "advise", "intelligent",
                          "execution", "contracts", "knowledge_search", "message_get", "message_delivery"},
        "yunpai-m5-pmc-lifecycle": {"default", "schedule", "versions", "progress", "execution"},
    }

    def preflight(self, step: dict[str, Any], state: RunState) -> dict[str, Any] | None:
        if step.get("mode") != "free" or step["id"] in state.get("authorized_steps", []):
            return None
        if step.get("kind") == "skill":
            skill_payload = state.get("request", {}).get("skill_payload")
            operation = str(skill_payload.get("operation") or "default") if isinstance(skill_payload, dict) else "default"
            if operation in self._READ_ONLY_SKILL_OPERATIONS.get(str(step["tool"]), set()):
                return None
        if step["tool"] in self._SAFE_LOCAL_TOOLS:
            return None
        method = str(step.get("http_method") or "POST").upper()
        if method not in {"GET", "HEAD", "OPTIONS"} and step["tool"] not in self._POST_REVIEWED_DRAFT_TOOLS:
            return {
                "type": "authorization", "module": step["module"], "tool": step["tool"],
                "message": "该自由工具可能产生持久化或外部副作用，必须在执行前授权",
                "actions": ["批准执行", "终止"], "pre_execution": True,
            }
        return None

    def review(self, tool: str, module: str, result: dict[str, Any]) -> dict[str, Any]:
        # High-level Skills flatten the underlying Tool result and identify it
        # explicitly so the same deterministic Gate rules still apply.
        effective_tool = str(result.get("invoked_tool") or tool) if isinstance(result, dict) else tool
        effective_module = str(result.get("invoked_module") or module) if isinstance(result, dict) else module
        data = result_data(result)
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        if result.get("success") is False or result.get("code") == "BLOCKED_INPUT":
            message = errors[0].get("message") if errors and isinstance(errors[0], dict) else result.get("message", "缺少权威输入")
            return self._gate("data", effective_module, effective_tool, message, ["补充数据", "终止"])
        if effective_tool in {"data_import_run", "business-data-identification"}:
            if result.get("status") == "failed":
                return {"approved": False, "terminal": True, "error": {"code": "IMPORT_FAILED", "message": "M0 未生成可审核候选"}}
            sensitivity = result.get("sensitivity_summary") if isinstance(result.get("sensitivity_summary"), dict) else {}
            sensitive = {kind: count for kind, count in sensitivity.items() if kind in {"hr", "financial"} and int(count) > 0}
            if sensitive and effective_tool == "business-data-identification":
                detail = "、".join(f"{kind}={count}" for kind, count in sensitive.items())
                return self._gate("sensitive_data", effective_module, effective_tool, f"候选包含敏感资料（{detail}）；必须由授权人员复核后才可进入 M0 canonical 发布", ["授权复核", "拒绝", "终止"])
            message = "业务资料候选已写入识别库，必须审核后才能进入 M0 canonical 发布" if effective_tool == "business-data-identification" else "M0 候选必须审核后才能发布 canonical 事实"
            return self._gate("candidate", effective_module, effective_tool, message, ["批准候选", "补充裁决", "终止"])
        if effective_tool == "ingest_document" and (result.get("needs_review") or float(result.get("overall_confidence") or 0) < 0.8):
            if isinstance(result.get("semantic_supplement"), dict):
                supplement_header = ((result.get("semantic_supplement") or {}).get("document") or {}).get("header")
                external_header = ((result.get("document") or {}).get("header") or {})
                if isinstance(supplement_header, dict) and supplement_header.get("product_code") and not external_header.get("product_code"):
                    message = "M1 订单行已识别；顶层产品编码由本地确定性候选回填，请人工确认后再放行（外部结果与候选都保留）"
                else:
                    message = "外部 M1 未形成有效订单行/订单号；已附加本地确定性解析候选，请人工复核后再放行（外部结果与候选都保留）"
            else:
                message = "M1 解析置信度不足或存在字段缺口"
            return self._gate("review", effective_module, effective_tool, message, ["修正并重试", "接受结果", "终止"])
        if effective_tool == "run_bom_sop_workflow":
            if result.get("status") == "human_input_required":
                canonical_match = result.get("canonical_bom_match")
                if isinstance(canonical_match, dict) and canonical_match.get("status") == "matched":
                    count = canonical_match.get("line_count") or 0
                    sop_match = result.get("canonical_sop_match")
                    if isinstance(sop_match, dict) and sop_match.get("status") == "matched":
                        operations = sop_match.get("operation_count") or 0
                        missing_times = sop_match.get("standard_minutes_missing") or 0
                        suffix = f"；已识别 {operations} 道 SOP 工序"
                        if missing_times:
                            suffix += f"，{missing_times} 道缺少标准工时，不能直接生成生产 PMC"
                        return self._gate("engineering", effective_module, effective_tool, f"M0 已匹配并审核 {count} 条 BOM{suffix}；请工程确认路线与资源", ["批准 BOM/SOP", "补充工时/资源", "终止"])
                    return self._gate("engineering", effective_module, effective_tool, f"M0 已匹配并审核 {count} 条 BOM；SOP/工艺约束仍需工程确认", ["批准 BOM/SOP", "补充 SOP", "终止"])
                return self._gate("data", effective_module, effective_tool, "M2 缺少产品/BOM 权威输入", ["补充数据", "终止"])
            if result.get("status") == "draft_created":
                sop_match = result.get("canonical_sop_match")
                if isinstance(sop_match, dict) and sop_match.get("status") == "matched":
                    missing_times = sop_match.get("standard_minutes_missing") or 0
                    return self._gate("engineering", effective_module, effective_tool, f"已识别 {sop_match.get('operation_count') or 0} 道 SOP 工序；{missing_times} 道缺少标准工时，需工程确认后再进入 PMC", ["批准 BOM/SOP", "补充工时/资源", "终止"])
                return self._gate("engineering", effective_module, effective_tool, "BOM/SOP 草稿必须由工程人员批准", ["批准 BOM/SOP", "修改后重试", "终止"])
        if effective_tool == "run_m3_procurement_requirements" and not data.get("lines") and not data.get("shortage_lines"):
            return self._gate("data", effective_module, effective_tool, "M3 缺少可计算的 BOM 行", ["补充 BOM 后重试", "终止"])
        if effective_tool == "import_m4_purchase_suggestions_json":
            suggestions = result.get("suggestions") or result.get("items") or []
            missing_supplier = any(not item.get("supplier_name") for item in suggestions if isinstance(item, dict))
            if suggestions and missing_supplier:
                return self._gate("procurement", effective_module, effective_tool, "采购建议缺少权威供应商或交期", ["补充供应商后重试", "保留草稿继续", "终止"])
        if effective_tool == "solve_scheduling" and data.get("lifecycle_status") == "draft":
            return self._gate("apply", effective_module, effective_tool, "排程候选验证通过，但设置 current/发布仍需人工批准", ["发布", "重排", "终止"])
        return {"approved": True, "terminal": False, "gate": None}

    @staticmethod
    def _gate(gate_type: str, module: str, tool: str, message: str, actions: list[str]) -> dict[str, Any]:
        return {"approved": False, "terminal": False, "gate": {"type": gate_type, "module": module, "tool": tool, "message": message, "actions": actions}}

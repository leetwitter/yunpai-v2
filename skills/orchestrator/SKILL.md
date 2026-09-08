---
name: yunpai-orchestrator
description: Route Yunpai manufacturing conversations through planner, worker, and reviewer agents. Use for M0-to-M5 workflows, free registered-tool execution, human gates, recovery, and auditable TaskID traces.
---

# Orchestrator

## 目的
将用户目标转为可观察、可恢复、可审计的业务任务。规划 Agent 只负责理解和拆解；Worker 只通过注册表调用工具；Reviewer 负责确定性校验和人工 Gate。

## 路由
- `workflow=m0_m5` 或目标同时包含订单、采购、排程：加载 `workflows/m0_m5.json`，执行 `M0 candidate -> M0 publish -> M1 -> M2 -> M3 -> M4 -> M5`。
- 显式 `tool`、`tools` 或文本命中工具名：进入自由 ReAct 路径，可执行一个或多个注册工具。
- 文件上传后要求“业务资料/业务数据识别、文件落库”时，调用 `business-data-identification` Skill；它写入可审核候选库，不直接发布 M0 canonical 事实。
- 其他文本：返回解释，不创建业务副作用。

## 状态合同
输入包含 `message|task|workflow|tool|tools` 之一及相应载荷。上传资料识别可使用 `business_data_root`（业务数据目录）或 `documents/files`（上传文件）。输出为 `RunState`：`run_id/task_id/status/workflow_id/workflow_version/plan/next_step_index/outputs/evidence/steps/pending_gate/approvals/errors/trace`。需要调用具体模块工具时，读取相应 `skills/m0` 至 `skills/m5` 的 `references/tools.md`。

## Gate 语义
`waiting_human` 必须携带 `pending_gate.type/module/tool/step_index/message/actions`。恢复请求使用 `decision=approve|allow|continue|retry|reject|stop`；`retry` 合并 `supplement` 后重跑原步骤，批准从下一步骤继续，拒绝终止但保留完整 trace。

自由路径中只读接口可直接执行。只生成候选或草稿并立即后置审查的主链接口可先执行；其他非只读接口先打开 `authorization` Gate，批准后恰好执行一次，拒绝时不得调用工具。

## 安全边界
不猜库存、供应商、交期、产能或审核结果；模型不能直接写 M0 current、发送采购、发布排程、修改权限/Schema。

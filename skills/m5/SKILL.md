---
name: yunpai-m5-pmc
description: Solve, validate, review, replan, dispatch, and observe Yunpai production schedules and workload reports. Use for M5 PMC planning, readiness, knowledge, messaging, execution, or material-procurement views.
---

# M5 PMC

## 职责
将订单、批准路线、资源、班次、物料可用性和约束编译为不可变排程候选，验证后由人工设置 current，再进入派工/执行反馈。

## 使用方式
正式求解使用 `solve_scheduling`，至少提供 `idempotency_key/scenario_id/planning_start/orders/routing_steps/resources`；查询、重排、消息、派工、采购视图和报工使用其他专用工具。完整接口和嵌套字段见 [references/tools.md](references/tools.md)。

M5 Flow Board 的版本历史、重排、派工和执行回传由已注册的 `yunpai-m5-pmc-lifecycle` Skill 统一路由；它只调用 M5 Tool 白名单，不绕过审批、发布或 MES 发送边界。

## 规则
缺订单、工艺、资源或生产单元映射返回 `BLOCKED_INPUT`；`pressure_only` 不能发布；同场景旧 head 必须 CAS 校验。排程工具只生成 draft，不替代审批、MES 发送或成本事实。

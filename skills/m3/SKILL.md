---
name: yunpai-m3-material-planning
description: Calculate and review Yunpai MRP, material matching, shortages, readiness snapshots, and demand feedback. Use for M3 procurement requirements and material-readiness questions, not purchase-order execution.
---

# M3 物料需求

## 职责
按不可变库存/在途快照计算 MRP、物料匹配和缺料。M3 拥有运行事实，不写 M0 canonical，也不预占库存。

## 使用方式
正式计算使用 `run_m3_procurement_requirements({order:{project_id,order_id,bom_id,product_name,order_qty,due_date},bom:{bom_id,product_name,lines}})`；齐套快照和反馈使用正式专用工具，`LEGACY` 工具只用于兼容。完整接口见 [references/tools.md](references/tools.md)。

Skill operation 覆盖计划计算、订单/计划查询、齐套、PR/PO 草稿、审批、M3→M4 handoff 查询和采购建议导出。写操作包括 `legacy_plan`、`approve`、`reject`、`request_change`、`approve_to_send`；其他查询操作保持只读。`receive_m3_material_demand` 不在当前 Skill 范围。

## 规则
`required = order.quantity * bom.quantity_per`；`shortage=max(0,required-available)`。缺 approved BOM、权威库存或交期时 `data_incomplete/BLOCKED_INPUT`；只有 `approve_to_send` 成功后才能导出受版本和 checksum 约束的 M3→M4 数据。

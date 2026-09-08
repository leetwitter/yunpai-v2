# M3 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M3 工具时读取。

## M3 能力

默认服务地址：`http://m3:8000`。完整 Schema：`registry/tool-manifests/m3.json`。

### `run_m3_procurement_requirements`

正式 M3 工具。接收订单和完整 BOM/子 BOM，由 M3 自行查询库存、在途采购与历史用量，经过可审计物料匹配后计算交给 M4 的采购需求。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/procurement-requirements:run-json`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tenant_id` | `string` | 否 | 租户 ID；正式 Orchestrator 流程必须传入。 |
| `order` | `object` | 否 | - |
| `bom` | `object` | 否 | - |
| `component_bom` | `array | object` | 否 | 已展开或可递归展开的子 BOM 关系。 |
| `inventory_snapshot` | `array` | 否 | 旧调用兼容字段；正式计算忽略该字段并查询 M3 Provider。 |
| `open_purchase_orders` | `array` | 否 | 旧调用兼容字段；正式计算忽略该字段并查询 M3 Provider。 |
| `historical_usage` | `array` | 否 | 旧调用兼容字段；正式计算忽略该字段并查询 M3 Provider。 |
| `m2_package` | `object` | 否 | 兼容当前 M2 输出的数据包。 |
| `m2_package_dir` | `string` | 否 | 兼容已有本地 M2 产物目录。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `run_mrp_procurement_plan`

[LEGACY] 根据订单、BOM、库存、在途采购和采购参数计算旧版聚合采购计划、审批任务和下游草稿。仅供已有调用兼容；新流程必须使用 run_m3_procurement_requirements。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/procurement-plan:run-json`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tenant_id` | `string` | 否 | 租户 ID；正式 Orchestrator 流程必须传入。 |
| `source_module` | `string` | 否 | 上游模块，例如 M1 或 M2。 |
| `source_package_id` | `string` | 否 | 上游数据包、任务或产物包 ID。 |
| `project_id` | `string` | 否 | - |
| `order` | `object` | 否 | - |
| `bom` | `object` | 否 | - |
| `approval_context` | `object` | 否 | 上游业务、工程、财务、法务审核状态。不确定或 blocked 会进入 M3 质量报告和人工审核。 |
| `m2_package` | `object` | 否 | M2 兼容包，包含 bom_header、bom_lines、assumptions、project_file。M3 会先转换为标准 order + bom。 |
| `m2_package_dir` | `string` | 否 | 兼容已有本地 M2 产物目录调用；跨服务调用优先使用 m2_package。 |
| `inventory_snapshot` | `array` | 否 | - |
| `procurement_params` | `array` | 否 | - |
| `open_purchase_orders` | `array` | 否 | - |
| `historical_usage` | `array` | 否 | - |
| `component_bom` | `array | object` | 否 | - |
| `procurement_adjustments` | `array` | 否 | - |
| `substitute_materials` | `array` | 否 | - |
| `inventory_transactions` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | 采购计划主体，包含 procurement_plan_id、availability_status、lines、shortage_lines。 |
| `errors` | `array` | 是 | - |
| `order_id` | `string` | 是 | - |
| `cache` | `object` | 是 | - |
| `quality_report` | `object` | 是 | - |
| `approval_tasks` | `array` | 是 | - |
| `behavior_controls` | `object` | 是 | - |
| `execution_log` | `array` | 是 | - |
| `pr_po_drafts` | `object` | 是 | - |
| `inventory_reservation_drafts` | `object` | 是 | - |
| `data_completion_tasks` | `array` | 是 | - |
| `material_readiness_handoff` | `object` | 是 | - |
| `supplier_message_drafts` | `array` | 是 | - |
| `inventory_transaction_ledger` | `object` | 是 | - |
| `inventory_audit_clues` | `object` | 是 | - |
| `substitute_material_suggestions` | `object` | 是 | - |
| `upstream_context` | `object` | 否 | - |
| `trace_id` | `string` | 是 | - |

### `list_m3_orders`

[LEGACY] 查询旧 M3 数据提供器中的订单列表，仅供已有调用兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/orders`，超时 `60s`

输入：

无固定顶层字段；以 JSON Schema 的组合约束为准。

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `array` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_m3_order`

[LEGACY] 查询旧 M3 数据提供器中的单个订单，仅供已有调用兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/orders/{order_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_m3_procurement_plan`

[LEGACY] 查询并运行旧版聚合采购计划，仅供已有调用兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/procurement-plan`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | 要查计划的目标订单 ID。 |
| `expand_bom` | `boolean` | 否 | 是否展开多层级 BOM 明细行。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_persisted_m3_plan`

[LEGACY] 读取旧版持久化采购计划全量 bundle，仅供已有调用兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/persisted/procurement-plans/{plan_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_material_readiness_snapshot`

正式 M5 物料齐套快照：按订单生成/查询逐料齐备快照（material readiness snapshot），返回订单级齐套状态（ready/partial/shortage/quality_hold/unknown）、最早可齐套时间、逐料 required/allocated/shortage 数量与 trusted ready time、每个库存 lot 或采购行的 allocation 明细（source/quantity/available_at/confidence/checksum）及固定输入版本。回答“某订单物料齐不齐、缺什么料、什么时候能齐”时使用本工具。订单号示例：ORD-DEMO-M3-0001。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/material-readiness-snapshot`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | 目标订单 ID，例如 ORD-DEMO-M3-0001。 |
| `tenant_id` | `string` | 是 | 必填租户 ID；仅查询该租户的持久化计划。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_material_readiness`

[LEGACY] 查询旧版物料齐套和 PMC 交接结果；该职责不再属于正式 M3，请改用 get_material_readiness_snapshot。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/material-readiness`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_pr_po_drafts`

[LEGACY] 查询旧版 PR/PO 草稿；采购执行职责应迁移到 M4。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/pr-po-drafts`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_m3_approval_tasks`

[LEGACY] 查询旧版 M3 审批任务；正式 M3 不再产生审批任务。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/approval-tasks`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `array` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `approve_m3_task`

[LEGACY] 审批旧版 M3 任务，仅供已有流程兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/approval-tasks/{approval_id}:approve`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `approval_id` | `string` | 是 | - |
| `order_id` | `string` | 是 | - |
| `actor_user` | `string` | 是 | - |
| `actor_role` | `string` | 是 | - |
| `comment` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `reject_m3_task`

[LEGACY] 驳回旧版 M3 任务，仅供已有流程兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/approval-tasks/{approval_id}:reject`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `approval_id` | `string` | 是 | - |
| `order_id` | `string` | 是 | - |
| `actor_user` | `string` | 是 | - |
| `actor_role` | `string` | 是 | - |
| `comment` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `request_change_m3_task`

[LEGACY] 请求修改旧版 M3 任务，仅供已有流程兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/approval-tasks/{approval_id}:request-change`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `approval_id` | `string` | 是 | - |
| `order_id` | `string` | 是 | - |
| `actor_user` | `string` | 是 | - |
| `actor_role` | `string` | 是 | - |
| `comment` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `approve_to_send_m3_task`

[LEGACY] 批准并发送旧版供应商消息；该职责应迁移到 M4。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/approval-tasks/{approval_id}:approve_to_send`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `approval_id` | `string` | 是 | - |
| `order_id` | `string` | 是 | - |
| `actor_user` | `string` | 是 | - |
| `actor_role` | `string` | 是 | - |
| `comment` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `get_m3_m4_handoffs`

[LEGACY] 查询旧版 M3 到 M4 handoff 记录，仅供已有流程兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/procurement-plan/{plan_id}/m4-handoffs`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `array` | 否 | - |
| `trace_id` | `string` | 否 | - |

### `export_m3_procurement_suggestions`

[LEGACY] 导出旧版采购建议供 Orchestrator 转发 M4，仅供已有流程兼容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/m3/procurement-plan/{plan_id}/export-suggestions`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_id` | `string` | 是 | 采购计划 ID。 |
| `tenant_id` | `string` | 是 | 由已认证 Orchestrator 传入的租户 ID。 |
| `version_id` | `string` | 是 | 当前 M3 运行返回的不可变采购计划版本 ID。 |
| `source_plan_checksum` | `string` | 是 | 当前 M3 运行返回的原始计划内容 SHA-256。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_id` | `string` | 否 | - |
| `procurement_plan_id` | `string` | 否 | - |
| `procurement_plan_version_id` | `string` | 否 | - |
| `version_id` | `string` | 否 | - |
| `source_plan_checksum` | `string` | 否 | - |
| `observed_at` | `string` | 否 | 不可变采购计划快照的创建时间；同一 version_id 重复导出保持不变。 |
| `tenant_id` | `string` | 否 | - |
| `tracking_task_id` | `string` | 否 | 从 X-Yunpai-Task-ID 原样校验并返回的业务追踪 ID。 |
| `suggestions` | `array` | 否 | 采购建议行数组,每条含 item_code/item_name/quantity/unit/supplier_name/required_date/project_code。 |
| `count` | `integer` | 否 | - |

### `receive_m3_material_demand`

接收 M5 排程发布后的 material-demand 反馈（经 orchestrator bridge 转交 M3）。幂等受理并持久化（同 task_id+event_id 重放返回同一 feedback_ref）；应用（并入计划缺口）由后续阶段人工/规则确认，本工具只受理记录。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/m3/material-demand/receive`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `feedback_type` | `string` | 是 | - |
| `event_id` | `string` | 是 | - |
| `task_id` | `string` | 是 | - |
| `event_kind` | `string` | 否 | - |
| `event_version` | `integer` | 否 | - |
| `schema_version` | `string` | 否 | - |
| `base_plan_version` | `string | null` | 否 | - |
| `plan_version` | `string` | 是 | - |
| `scenario_id` | `string` | 否 | - |
| `lifecycle_status` | `string` | 否 | - |
| `source_input_hash` | `string` | 否 | - |
| `affected_lines` | `array` | 否 | - |
| `occurred_at` | `string` | 否 | - |
| `observed_at` | `string` | 否 | - |
| `checksum` | `string` | 否 | - |
| `feedback_ref` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

# M4 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M4 工具时读取。

## M4 能力

默认服务地址：`http://m4:8000`。完整 Schema：`registry/tool-manifests/m4.json`。

### `import_m4_purchase_suggestions`

[LEGACY/LOCAL] 导入采购建议 CSV。共享及生产环境默认关闭无租户导入；正式 M3→M4 交接必须使用 import_m4_purchase_suggestions_json，并携带租户、站点、TaskID、版本和授权。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/import-batches`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | `string` | 是 | M3采购建议CSV文件。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer | null` | 否 | - |
| `filename` | `string` | 否 | - |
| `total_rows` | `integer` | 否 | - |
| `valid_rows` | `integer` | 否 | - |
| `invalid_rows` | `integer` | 否 | - |
| `duplicate_rows` | `integer` | 否 | - |
| `status` | `string` | 否 | - |
| `items` | `array` | 否 | - |

### `import_m4_purchase_suggestions_json`

以 JSON 方式导入采购建议到 M4（供 orchestrator 自动化调用，无需 multipart 文件上传）。接受采购建议数组，每条含 item_code、item_name、quantity、unit、supplier_name、required_date、project_code。当 orchestrator 需要把 M3 的采购建议自动流转到 M4 时使用——M3 导出的采购建议可直接作为此工具的 suggestions 参数。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/suggestions/import-json`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `suggestions` | `array` | 是 | 采购建议数组，每条含 item_code、item_name、quantity、unit、supplier_name、required_date、project_code。 |
| `tenant_id` | `string` | 否 | 受信 tenant 标识；提供时必须同时提供 site_id。 |
| `site_id` | `string` | 否 | M4 采购归属站点；提供时必须同时提供 tenant_id，且与 data_scope 含义独立。 |
| `tracking_task_id` | `string` | 否 | 根 Tracking TaskID（X-Yunpai-Task-ID 原样传播，幂等作用域一部分） |
| `idempotency_key` | `string` | 否 | 幂等键：同 (tracking_task_id, idempotency_key) 同正文 replay 返回原批次，异正文 409 |
| `source_module` | `string` | 否 | 来源模块（如 m3） |
| `procurement_plan_id` | `string` | 否 | M3 采购计划 ID |
| `procurement_plan_version_id` | `string` | 否 | M3 采购计划版本 ID |
| `source_plan_checksum` | `string` | 否 | M3 计划内容 checksum（若提供） |
| `order_id` | `string` | 否 | 业务订单 ID |
| `order_version` | `string` | 否 | 订单版本（若提供） |
| `project_id` | `string` | 否 | - |
| `bom_id` | `string` | 否 | - |
| `source_event_id` | `string` | 否 | 来源事件 ID（稳定、可追溯） |
| `observed_at` | `string` | 否 | - |
| `actor` | `string` | 否 | 受信主体（展示/审计用，RBAC 收紧在后续 MR） |
| `data_scope` | `string` | 否 | 来源命令的数据可见范围审计值，不作为 site_id。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer | null` | 否 | - |
| `filename` | `string` | 否 | - |
| `total_rows` | `integer` | 否 | - |
| `valid_rows` | `integer` | 否 | - |
| `invalid_rows` | `integer` | 否 | - |
| `duplicate_rows` | `integer` | 否 | - |
| `status` | `string` | 否 | - |
| `tenant_id` | `string | null` | 否 | - |
| `site_id` | `string | null` | 否 | - |
| `tracking_task_id` | `string | null` | 否 | - |
| `idempotency_key` | `string | null` | 否 | - |
| `source_plan_id` | `string | null` | 否 | - |
| `source_plan_version` | `string | null` | 否 | - |
| `source_plan_checksum` | `string | null` | 否 | - |
| `source_order_id` | `string | null` | 否 | - |
| `payload_digest` | `string | null` | 否 | - |
| `items` | `array` | 否 | - |

### `list_m4_purchase_suggestions`

查询 M4 已导入的采购建议清单，可按批次、供应商、物料编码和校验状态过滤。当需要查看哪些采购建议有效、哪些需要修正时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/suggestions`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `batch_id` | `integer` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `item_code` | `string` | 否 | - |
| `validation_status` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `total` | `integer` | 否 | - |

### `generate_m4_purchase_orders`

根据同一导入批次的有效采购建议生成采购单草稿。tenant/site 省略时继承批次归属；显式提供时必须与批次一致。已追踪批次要求 X-Yunpai-Task-ID 与批次完全一致，生成结果持久化来源批次和同一 TaskID。历史无归属/无 TaskID 批次不会补造身份。同一完整建议 ID 集合重放返回原采购单；作用域、来源、TaskID 不一致或部分/混合复用返回冲突。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/generate`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `suggestion_item_ids` | `array` | 是 | - |
| `tenant_id` | `string` | 否 | 可选；提供时必须同时提供 site_id，作为新采购单的 M4 归属。 |
| `site_id` | `string` | 否 | 可选；提供时必须同时提供 tenant_id，作为新采购单的 M4 归属。 |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `list_m4_purchase_orders`

查询 M4 采购单列表，可按状态和供应商过滤。当用户需要查看采购单草稿、审核中、已批准或已发送的采购单时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/purchase-orders`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `status` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `total` | `integer` | 否 | - |

### `get_m4_purchase_order`

查询单个采购单的完整明细（含采购单号、供应商、状态、需求日期和行项）。当用户问‘这张采购单具体情况’‘采购单明细’‘这张单到哪个状态了’时使用。在审批或发送前用此确认采购单内容。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/purchase-orders/{purchase_order_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `required_date` | `string | null` | 否 | - |
| `items` | `array` | 否 | - |

### `submit_m4_purchase_order_review`

将受控采购单草稿提交人工审核（draft/request_changes → pending_review）。必须绑定根 TaskID 以及当前采购单 revision/checksum；过期写入返回 409。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/{purchase_order_id}/submit-review`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |
| `expected_revision` | `integer` | 是 | - |
| `expected_checksum` | `string` | 是 | - |
| `comment` | `string` | 否 | 可选送审备注。 |
| `operated_by` | `string` | 否 | 可选操作人。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `required_date` | `string | null` | 否 | - |
| `items` | `array` | 否 | - |

### `approve_m4_purchase_order`

人工批准受控采购单（pending_review → approved_for_message）。必须绑定根 TaskID 以及待审采购单的准确 revision/checksum；批准仅允许生成供应商邮件草稿，不代表已发送或已入库。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/{purchase_order_id}/approve`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |
| `expected_revision` | `integer` | 是 | - |
| `expected_checksum` | `string` | 是 | - |
| `comment` | `string` | 否 | 可选审批备注。 |
| `operated_by` | `string` | 否 | 可选审批人。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `required_date` | `string | null` | 否 | - |
| `items` | `array` | 否 | - |

### `request_changes_m4_purchase_order`

人工将受控采购单退回修改（pending_review → request_changes）。必须绑定根 TaskID 以及待审采购单的准确 revision/checksum，并保留退回原因。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/{purchase_order_id}/request-changes`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |
| `expected_revision` | `integer` | 是 | - |
| `expected_checksum` | `string` | 是 | - |
| `comment` | `string` | 否 | 可选驳回原因。 |
| `operated_by` | `string` | 否 | 可选审批人。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `required_date` | `string | null` | 否 | - |
| `items` | `array` | 否 | - |

### `generate_m4_purchase_inquiry_message`

基于已人工批准的采购单生成供应商邮件草稿。必须绑定采购单 revision/checksum，并保存人工核对的收件人和附件快照；仅生成草稿，不对外发送，也不产生到货或库存事实。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/{purchase_order_id}/inquiry-message`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |
| `expected_po_revision` | `integer` | 是 | - |
| `expected_po_checksum` | `string` | 是 | - |
| `recipient_snapshot` | `object` | 是 | 人工核对的收件人快照；模拟验收必须明确 simulation_only=true。 |
| `attachment_snapshot` | `array` | 否 | - |
| `channel` | `string` | 否 | - |
| `language` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `message_id` | `integer` | 否 | - |
| `subject` | `string | null` | 否 | - |
| `content` | `string` | 否 | - |
| `channel` | `string` | 否 | - |
| `recipient` | `string | null` | 否 | - |
| `send_status` | `string` | 否 | - |
| `model_name` | `string | null` | 否 | - |
| `prompt_version` | `string | null` | 否 | - |

### `send_m4_purchase_order`

[LEGACY/LOCAL] 旧版采购单发送记录，仅供无 TaskID 的本地兼容流程；受控流程禁止调用。受控流程必须使用采购单审核、独立邮件审核和 record-simulated，且不得据此生成到货或库存事实。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/purchase-orders/{purchase_order_id}/send`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `items` | `array` | 否 | - |

### `create_m4_supplier_reply`

保存供应商回复原文，用于后续 AI 解析交期、价格和异常。适用于采购员收到邮件、微信、钉钉等供应商回复后录入 M4。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/replies`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `purchase_order_id` | `integer` | 是 | - |
| `purchase_order_no` | `string` | 是 | - |
| `supplier_name` | `string` | 是 | - |
| `reply_content` | `string` | 是 | - |
| `received_at` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `purchase_order_id` | `integer` | 否 | - |
| `purchase_order_no` | `string` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `reply_content` | `string` | 否 | - |
| `received_at` | `string | null` | 否 | - |

### `parse_m4_supplier_reply`

解析供应商回复，提取承诺交期、价格、币种、税含标记和异常说明。低置信度结果会标记 need_human_review，需要人工确认。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/replies/{reply_id}/parse`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `reply_id` | `integer` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `delivery_date` | `string | null` | 否 | - |
| `unit_price` | `number | string | null` | 否 | - |
| `currency` | `string` | 否 | - |
| `tax_included` | `boolean | null` | 否 | - |
| `exception_type` | `string | null` | 否 | - |
| `exception_description` | `string | null` | 否 | - |
| `confidence` | `number` | 否 | - |
| `need_human_review` | `boolean` | 否 | - |

### `confirm_m4_supplier_reply`

人工确认或修正供应商回复的 AI 解析结果。parse_m4_supplier_reply 标记 need_human_review=true 时，用此工具提交确认后的交期/价格/异常等字段，闭合 HITL 环节。确认后的结果才作为正式承诺写入追踪。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/replies/{reply_id}/confirm`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `reply_id` | `integer` | 是 | - |
| `delivery_date` | `string | null` | 否 | 确认后的承诺交期（YYYY-MM-DD）。 |
| `unit_price` | `number | string | null` | 否 | 确认后的单价。 |
| `currency` | `string` | 否 | - |
| `tax_included` | `boolean | null` | 否 | - |
| `exception_type` | `string | null` | 否 | - |
| `exception_description` | `string | null` | 否 | - |
| `confidence` | `number` | 否 | 确认后的置信度，通常置为 1.0。 |
| `need_human_review` | `boolean` | 是 | 是否仍需人工复核；人工确认时一般传 false。 |
| `confirmed_by` | `string` | 否 | 可选确认人。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `delivery_date` | `string | null` | 否 | - |
| `unit_price` | `number | string | null` | 否 | - |
| `currency` | `string` | 否 | - |
| `tax_included` | `boolean | null` | 否 | - |
| `exception_type` | `string | null` | 否 | - |
| `exception_description` | `string | null` | 否 | - |
| `confidence` | `number` | 否 | - |
| `need_human_review` | `boolean` | 否 | - |

### `list_m4_suppliers`

查询 M4 供应商主数据列表，可按供应商名称模糊过滤。当用户问‘有哪些供应商’‘某供应商的联系方式’‘供应商主数据’时使用。返回分页供应商信息（名称、联系人、邮箱、电话、默认渠道、状态）。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/suppliers`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `supplier_name` | `string` | 否 | 可选供应商名称过滤。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `total` | `integer` | 否 | - |

### `create_m4_supplier`

新建供应商主数据。当采购需要登记一个新供应商（含联系人、邮箱、电话、默认沟通渠道）时使用。新建后的供应商可用于采购单和询价消息。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/suppliers`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `supplier_name` | `string` | 是 | - |
| `contact_name` | `string` | 否 | - |
| `email` | `string` | 否 | - |
| `phone` | `string` | 否 | - |
| `default_channel` | `string` | 否 | 默认沟通渠道，如 email/wechat/dingtalk。 |
| `remark` | `string` | 否 | - |
| `status` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `contact_name` | `string | null` | 否 | - |
| `email` | `string | null` | 否 | - |
| `phone` | `string | null` | 否 | - |
| `default_channel` | `string` | 否 | - |
| `remark` | `string | null` | 否 | - |
| `status` | `string` | 否 | - |

### `update_m4_supplier`

更新供应商主数据（联系人、邮箱、电话、默认渠道、状态等）。当供应商信息变更（换联系人、改邮箱、停用）时使用。status 常用于启用/停用供应商。

- 类型：`tool`
- 执行：`sync`
- HTTP：`PATCH /api/m4/suppliers/{supplier_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `supplier_id` | `integer` | 是 | - |
| `supplier_name` | `string` | 否 | - |
| `contact_name` | `string` | 否 | - |
| `email` | `string` | 否 | - |
| `phone` | `string` | 否 | - |
| `default_channel` | `string` | 否 | - |
| `remark` | `string` | 否 | - |
| `status` | `string` | 否 | 如 active/inactive。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `supplier_name` | `string` | 否 | - |
| `contact_name` | `string | null` | 否 | - |
| `email` | `string | null` | 否 | - |
| `phone` | `string | null` | 否 | - |
| `default_channel` | `string` | 否 | - |
| `remark` | `string | null` | 否 | - |
| `status` | `string` | 否 | - |

### `list_m4_tracking`

查询采购追踪结果，包括供应商承诺交期、价格、异常、到货状态和是否超期。当 M5 或项目总览需要了解采购执行状态时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/tracking`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `total` | `integer` | 否 | - |

### `scan_m4_purchase_alerts`

触发 M4 采购预警扫描，生成超期、临期和供应商异常等预警。当需要刷新采购风险看板或联动催单时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/alerts/scan`，超时 `60s`

输入：

无固定顶层字段；以 JSON Schema 的组合约束为准。

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scanned` | `integer` | 否 | - |
| `created` | `integer` | 否 | - |

### `list_m4_purchase_alerts`

查询 M4 采购预警列表，可按状态和预警类型过滤。当用户需要查看超期、临期、异常供应商回复或催单对象时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/alerts`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `status` | `string` | 否 | - |
| `alert_type` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |
| `page` | `integer` | 否 | - |
| `page_size` | `integer` | 否 | - |
| `total` | `integer` | 否 | - |

### `generate_m4_urge_message`

为指定采购预警生成催单话术草稿（AI 生成，不直接发送）。只允许 open 状态预警调用；生成成功会覆盖该预警的催单文本，按 alert_type 区分已超期/临期/供应商异常措辞。当需要对超期或临期采购生成催单文本时使用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/alerts/{alert_id}/urge-message`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `alert_id` | `integer` | 是 | - |
| `channel` | `string` | 否 | - |
| `language` | `string` | 否 | 可选目标输出语言，如 zh-CN、en-US、ja-JP。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `alert_id` | `integer` | 否 | - |
| `urge_message` | `string` | 否 | - |
| `channel` | `string` | 否 | - |
| `model_name` | `string | null` | 否 | - |
| `prompt_version` | `string | null` | 否 | - |

### `query_m4_material_supply_snapshot`

按期望租户、站点、物料和观测窗口读取 M4 当前采购供应投影。tenant_id 必须与认证 principal 完全一致，返回 snapshot 也以 checksum 绑定该 scope。只返回具有 M4 tenant/site 归属的采购单行；无收货权威时 received/open 均为 null，未知 ETA 和 arrival confidence 保持 null，不能据此释放排程。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/material-supply-snapshots:query`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `any` | 是 | - |
| `scenario_id` | `string` | 是 | - |
| `tenant_id` | `string` | 是 | - |
| `site_id` | `string` | 是 | - |
| `material_ids` | `array` | 是 | - |
| `as_of` | `string` | 是 | - |
| `horizon_end` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `any` | 是 | - |
| `snapshot_id` | `string` | 是 | - |
| `snapshot_version` | `integer` | 是 | - |
| `scenario_id` | `string` | 是 | - |
| `tenant_id` | `string` | 是 | - |
| `site_id` | `string` | 是 | - |
| `as_of` | `string` | 是 | - |
| `horizon_end` | `string` | 是 | - |
| `generated_at` | `string` | 是 | - |
| `observed_at` | `string` | 是 | - |
| `entity_version` | `string` | 是 | - |
| `lines` | `array` | 是 | - |
| `completeness` | `object` | 是 | - |
| `input` | `object` | 是 | - |
| `input_checksum` | `string` | 是 | - |
| `checksum` | `string` | 是 | - |

### `list_m4_material_supply_events`

按认证 tenant/site scope 和版本游标增量读取 M4 物料供应变化事件。用于排程发现供应商确认、ETA、发运、收货、质量冻结或取消后的确定性变化，并支持在同一 tenant/site 流内断点续取。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/material-supply-events`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `site_id` | `string` | 是 | - |
| `after_version` | `integer` | 否 | - |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `any` | 是 | - |
| `site_id` | `string` | 是 | - |
| `after_version` | `integer` | 是 | - |
| `items` | `array` | 是 | - |
| `has_more` | `boolean` | 是 | - |
| `next_after_version` | `integer` | 是 | - |

### `get_m4_material_supply_snapshot`

按 snapshot_id 和 snapshot_version 精确读取一份已持久化的 M4 供应快照。用于重试、审计或恢复排程输入；绝不以最新版本替代请求的不可变版本。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m4/material-supply-snapshots/{snapshot_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `snapshot_id` | `string` | 是 | - |
| `snapshot_version` | `integer` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `any` | 是 | - |
| `snapshot_id` | `string` | 是 | - |
| `snapshot_version` | `integer` | 是 | - |
| `scenario_id` | `string` | 是 | - |
| `tenant_id` | `string` | 是 | - |
| `site_id` | `string` | 是 | - |
| `as_of` | `string` | 是 | - |
| `horizon_end` | `string` | 是 | - |
| `generated_at` | `string` | 是 | - |
| `observed_at` | `string` | 是 | - |
| `entity_version` | `string` | 是 | - |
| `lines` | `array` | 是 | - |
| `completeness` | `object` | 是 | - |
| `input` | `object` | 是 | - |
| `input_checksum` | `string` | 是 | - |
| `checksum` | `string` | 是 | - |

### `confirm_m4_supplier_fact`

由已授权人员确认供应商回复的当前解析版本到一条 M4 采购单行。只在人工核对承诺日期或供应异常后使用；调用方不能提供确认人或确认时间，重复确认返回原结果，改变同一回复版本的事实返回冲突。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/replies/{reply_id}/supply-facts/confirm`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `reply_id` | `integer` | 是 | - |
| `schema_version` | `any` | 是 | - |
| `purchase_order_item_id` | `integer` | 是 | - |
| `supplier_reply_version` | `integer` | 是 | - |
| `result` | `object` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `any` | 是 | - |
| `id` | `integer` | 是 | - |
| `purchase_order_item_id` | `integer` | 是 | - |
| `supplier_reply_id` | `integer` | 是 | - |
| `supplier_reply_version` | `integer` | 是 | - |
| `confirmed_by` | `string` | 是 | - |
| `confirmed_at` | `string` | 是 | - |
| `parse_confidence` | `string | null` | 是 | - |
| `delivery_date` | `string | null` | 是 | - |
| `exception_type` | `string | null` | 是 | - |
| `exception_description` | `string | null` | 是 | - |
| `result_payload` | `object` | 是 | - |
| `checksum` | `string` | 是 | - |

### `receive_m4_schedule_impact_proposal`

接收 M5 排程需求变更建议（schedule-impact proposal，经 orchestrator bridge 转交 M4）。幂等受理并持久化（同 task_id+proposal_id 或同 task_id+idempotency_key 重放返回同一结果）；应用（调整采购建议/PO）由后续人工/规则确认，本工具只受理记录。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m4/schedule-impact-proposals`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `string` | 是 | - |
| `proposal_id` | `string` | 是 | - |
| `plan_version` | `string` | 是 | - |
| `scenario_id` | `string` | 否 | - |
| `material_id` | `string` | 是 | - |
| `required_quantity` | `string` | 否 | - |
| `uom` | `string` | 否 | - |
| `previous_required_at` | `string` | 否 | - |
| `required_at` | `string` | 否 | - |
| `impact_type` | `string` | 否 | - |
| `affected_orders` | `array` | 否 | - |
| `reason_code` | `string` | 否 | - |
| `m3_readiness_version` | `string` | 否 | - |
| `m4_supply_version` | `string` | 否 | - |
| `evidence` | `array` | 否 | - |
| `feedback_ref` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `received` | `boolean` | 是 | - |
| `proposal_id` | `string` | 是 | - |
| `replayed` | `boolean` | 是 | - |

# M1 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M1 工具时读取。

## M1 能力

默认服务地址：`http://m1:8080`。完整 Schema：`registry/tool-manifests/m1.json`。

### `ingest_document`

上传 PDF、图片、XLSX/XLSM/XLS/CSV、DOCX、DXF/DWG 或 ZIP/TAR/RAR/7Z。普通文档同步返回 m1.document.v2；归档自动分流为批次并返回可轮询的父子任务。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /ingest/sync`，超时 `240s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | `oneOf` | 是 | - |
| `doc_type_hint` | `string` | 否 | 可选文档大类提示；只影响分类，不覆盖源文件事实。 |
| `document_subtype_hint` | `string` | 否 | 可选文档子类型提示；只影响分类，不覆盖源文件事实。 |
| `semantic_enrichment` | `boolean` | 否 | 是否保留订单行的名称归一化、产品分类和名称属性等可选语义字段；关闭时仍执行 MinerU/Instructor 权威基础事实提取，但这些可选字段不会进入结果。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `status` | `string` | 是 | - |
| `processing_stage` | `string` | 否 | - |
| `schema_version` | `string | null` | 否 | Compatibility alias for document_schema_version; null before a v2 document exists. |
| `document_schema_version` | `string | null` | 否 | - |
| `document_subtype` | `string` | 否 | - |
| `needs_review` | `boolean` | 否 | - |
| `overall_confidence` | `number | null` | 否 | - |
| `document` | `object | null` | 否 | - |
| `extraction` | `object | null` | 否 | - |
| `scored_result` | `object | null` | 否 | - |

### `ingest_m1_archive`

异步上传 ZIP/TAR/RAR/7Z 归档，安全递归展开并为父归档与每个叶子文档建立可查询任务。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /ingest/archive`，超时 `240s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | `oneOf` | 是 | - |
| `doc_type_hint` | `string` | 否 | - |
| `document_subtype_hint` | `string` | 否 | - |
| `semantic_enrichment` | `boolean` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `status` | `string` | 是 | - |
| `processing_stage` | `string` | 否 | - |
| `child_count` | `integer` | 是 | - |
| `child_ids` | `array` | 是 | - |
| `async` | `boolean` | 否 | - |

### `get_m1_task`

查询单个 M1 任务的阶段、终态、失败原因、完整 v2 文档和兼容结果；用于同步上传超时后的轮询。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /tasks/{task_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `status` | `string` | 是 | - |
| `processing_stage` | `string` | 否 | - |
| `error` | `string | null` | 否 | - |
| `document_schema_version` | `string | null` | 否 | - |
| `document` | `object | null` | 否 | - |

### `get_m1_batch`

轮询归档或混合批次，返回父任务、全部子任务和完成/失败/待审核计数。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /batch/{parent_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `parent_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `parent` | `object` | 是 | - |
| `children` | `array` | 是 | - |
| `child_count` | `integer` | 是 | - |
| `done_count` | `integer` | 是 | - |
| `failed_count` | `integer` | 是 | - |
| `review_count` | `integer` | 是 | - |
| `pending_count` | `integer` | 是 | - |

### `get_m1_document`

按任务 ID 获取完整的 m1.document.v2 文档。返回确定性源文件事实、标准化订单行、字段证据与校验问题；旧 extraction/scored_result/fields/bom 字段继续保留。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /tasks/{task_id}/document`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | M1 任务 ID。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schema_version` | `string` | 否 | - |
| `source` | `any` | 是 | - |
| `document_type` | `string` | 否 | 文档大类，例如 order、inventory、equipment、mold、technical_document。 |
| `document_subtype` | `string` | 否 | 细分模板类型，例如 stocking_order、customer_purchase_order、inventory_snapshot。 |
| `header` | `any` | 否 | - |
| `lines` | `array` | 否 | - |
| `totals` | `any` | 否 | - |
| `field_meta` | `object` | 否 | JSONPath 到字段证据和置信度的映射。 |
| `validation_issues` | `array` | 否 | - |
| `needs_review` | `boolean` | 否 | - |
| `extraction` | `object` | 否 | 旧版抽取结果，兼容保留。 |
| `scored_result` | `object` | 否 | 旧版置信度结果，兼容保留。 |
| `fields` | `array` | 否 | 旧版字段列表，兼容保留。 |
| `bom` | `array` | 否 | 订单行兼容别名；与 lines 同步。 |

### `search_m1_orders`

按订单号、型号、产品名称、类别或日期范围检索 M1 的订单明细行。直接返回命中的完整行和 task_id，适合 Agent 回答订单字段与产品查询。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /orders/search`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_number` | `string` | 否 | - |
| `model` | `string` | 否 | - |
| `name` | `string` | 否 | - |
| `interface` | `string` | 否 | 接口类型（HDMI/USB/DP），匹配 name_attributes.interface |
| `length` | `string` | 否 | 线材长度（如 20M），匹配 name_attributes.cable_length |
| `color` | `string` | 否 | 颜色，匹配 name_attributes.color |
| `connector` | `string` | 否 | 连接器/插头，匹配 name_attributes.plug |
| `conductor` | `string` | 否 | 线芯/导体，匹配 name_attributes.conductor |
| `od` | `string` | 否 | 线径（如 7.0mm），匹配 name_attributes.od |
| `category` | `string` | 否 | - |
| `date_from` | `string` | 否 | - |
| `date_to` | `string` | 否 | - |
| `limit` | `integer` | 否 | - |
| `offset` | `integer` | 否 | - |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `export_m1_order`

获取标准订单 Excel 的下载链接。链接对应包含订单头、产品明细、校验问题、原始证据四个 Sheet 的 .xlsx 文件。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /tasks/{task_id}/exports/order`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `filename` | `string` | 是 | - |
| `content_type` | `string` | 是 | - |
| `download_url` | `string` | 是 | - |
| `generated` | `boolean` | 是 | - |

### `search_m1_documents`

检索 M1 已持久化的 m1.document.v2 文档。支持全文、文档类型、子类型以及任意 JSON 字段路径和值过滤；返回显式摘要命中，随后可用 get_m1_document 获取完整文档。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /documents/search`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `q` | `string` | 否 | 标题、正文、订单号、文件名等全文关键词。 |
| `document_type` | `string` | 否 | - |
| `document_subtype` | `string` | 否 | - |
| `field_path` | `string` | 否 | 要检索的 JSONPath，例如 $.lines[*].model。 |
| `field_value` | `string` | 否 | 字段值关键词，与 field_path 配合使用。 |
| `limit` | `integer` | 否 | - |
| `offset` | `integer` | 否 | - |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `list_m1_tasks`

查询 M1 已识别的文档任务列表，可按状态过滤（created/parsing/extracting/scoring/needs_review/done/failed）。当用户问‘识别过哪些文档’‘有哪些待审核’‘之前那份图识别完了吗’时使用。返回任务摘要（id/文件名/状态/置信度）。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /tasks`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `status` | `string` | 否 | 可选状态过滤，如 needs_review、done。留空返回全部（不含子任务）。 |
| `limit` | `integer` | 否 | 可选每页任务数；省略时为兼容旧调用返回全部。 |
| `offset` | `integer` | 否 | 分页偏移量；提供时必须同时提供 limit。 |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `list_m1_review_queue`

查询 M1 当前所有待人工审核（needs_review）的任务。当用户问‘有哪些识别结果需要我确认’‘待审核队列’时使用。这是 HITL 闭环的入口——返回的任务都等着人工修正或确认。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /review/queue`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `limit` | `integer` | 否 | 可选每页待审核任务数；省略时为兼容旧调用返回全部。 |
| `offset` | `integer` | 否 | 分页偏移量；提供时必须同时提供 limit。 |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `submit_m1_review`

对 M1 某个待审核任务提交人工审核结果（通过/驳回 + 字段修正）。当人工确认了识别结果、或修正了错误字段后，用此工具闭合 HITL 环节，任务转入 done。approve=true 表示通过，false 表示驳回；corrections 是字段名→新值的映射。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /review/{task_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `approve` | `boolean` | 否 | - |
| `reviewer` | `string` | 否 | - |
| `comment` | `string` | 否 | - |
| `corrections` | `object` | 否 | 字段名→修正值的映射。修正过的字段置信度置为 1.0。 |
| `header_corrections` | `object` | 否 | 订单头 JSON 字段名到审核值的映射。 |
| `line_corrections` | `anyOf` | 否 | 按 line_id 提交的明细行修正。 |
| `issue_resolutions` | `anyOf` | 否 | 校验问题的处理结果。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 否 | - |
| `status` | `string` | 否 | - |
| `scored_result` | `object` | 否 | - |

### `generate_m1_report`

为 M1 某个已识别任务生成 Markdown 综合报告（含字段表、置信度、明细）。当用户问‘给我出一份识别报告’‘导出这份文档的识别结果’时使用。单文件和批次父任务都支持。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /tasks/{task_id}/report`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 是 | - |
| `note` | `string` | 否 | 可选附加说明，会附在报告末尾。 |
| `force` | `boolean` | 否 | 是否强制重新生成（否则用缓存） |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `task_id` | `string` | 否 | - |
| `report_kind` | `string` | 否 | - |
| `report_status` | `string` | 否 | - |
| `message` | `string` | 否 | - |

### `search_m1_knowledge`

在指定租户的 M1 Governed Wiki 中检索已激活知识。返回可追溯的字段索引、相关性分数、来源版本和证据坐标；默认不暴露 candidate/待审核数据。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /knowledge/search`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `q` | `string` | 是 | 必填关键词、订单号、型号、企业或产品名称。 |
| `document_type` | `string` | 否 | - |
| `document_subtype` | `string` | 否 | - |
| `mode` | `string` | 否 | - |
| `min_score` | `number` | 否 | - |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tenant_id` | `string` | 是 | - |
| `query` | `string` | 是 | - |
| `include_candidate` | `boolean` | 是 | - |
| `mode` | `string` | 否 | - |
| `min_score` | `number` | 否 | - |
| `hits` | `array` | 是 | - |

### `list_m1_knowledge_entities`

列出指定租户中已激活的标准实体（文档、企业、订单、产品/物料、设备、模具），用于从 Wiki 主数据定位 canonical entity_id。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /knowledge/entities`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `entity_type` | `string` | 否 | - |
| `lifecycle_status` | `string` | 是 | - |
| `limit` | `integer` | 否 | - |
| `offset` | `integer` | 否 | - |

输出：

无固定顶层字段；以 JSON Schema 的组合约束为准。

### `get_m1_knowledge_entity`

按 canonical entity_id 获取一个租户内标准实体及其属性、生命周期、置信度和 ACL。跨租户或无权限时返回不存在。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /knowledge/entities/{entity_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `entity_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `entity_id` | `string` | 是 | - |
| `tenant_id` | `string` | 是 | - |
| `entity_type` | `string` | 是 | - |
| `canonical_key` | `string` | 是 | - |
| `canonical_label` | `string` | 是 | - |
| `attributes` | `object` | 否 | - |
| `lifecycle_status` | `string` | 是 | - |
| `confidence` | `number` | 否 | - |
| `acl` | `object` | 否 | - |
| `valid_from` | `string | null` | 否 | - |
| `valid_to` | `string | null` | 否 | - |

### `get_m1_knowledge_graph`

按 Wiki source_record_id 获取已激活的文档图投影，包含可追溯节点和关系；默认不返回 candidate 图。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /knowledge/graph/{source_record_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `source_record_id` | `string` | 是 | - |
| `version` | `string` | 否 | 可选精确版本；留空取最新版本。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `projection_id` | `string` | 是 | - |
| `tenant_id` | `string` | 是 | - |
| `source_record_id` | `string` | 是 | - |
| `version` | `string` | 是 | - |
| `nodes` | `array` | 是 | - |
| `edges` | `array` | 是 | - |

### `get_m1_knowledge_stats`

返回指定租户的来源、版本、绑定、Wiki 字段事实、实体关系与搜索/图投影计数，用于判断基础资料入库覆盖率和积压。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /knowledge/stats`，超时 `60s`

输入：

无固定顶层字段；以 JSON Schema 的组合约束为准。

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `tenant_id` | `string` | 是 | - |
| `inventory` | `object` | 是 | - |
| `claims` | `object` | 是 | - |
| `projections` | `object` | 是 | - |

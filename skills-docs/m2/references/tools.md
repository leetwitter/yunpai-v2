# M2 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M2 工具时读取。

## M2 能力

默认服务地址：`http://m2:8765`。完整 Schema：`registry/tool-manifests/m2.json`。

### `run_bom_sop_workflow`

BOM/SOP 生成 Agent。接受结构化产品信息（产品名/编码/规格），检索历史 BOM 记录，生成草稿 BOM（物料清单）和 SOP（标准作业指导书 Word 文档）。核心流程：① 分析历史 BOM 模板 ② 入库历史数据 ③ 多维度相似度检索历史 BOM ④ 受控生成草稿 BOM（规则+可选LLM）⑤ 生成 80806-129 格式 SOP Word 文档（含工序流程图）。当用户需要根据产品需求生成 BOM 物料清单或 SOP 工艺文档时使用。所有输出为草稿状态(draft_created/human_input_required)，需人工审核。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/run`，超时 `300s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_profile` | `object` | 是 | Product profile. |
| `requirement_text` | `string` | 否 | - |
| `rule_package_path` | `string` | 否 | 受控 BOM 和物料编码规则包目录或 ZIP；禁用 demo 时必填。 |
| `history_bom_paths` | `array` | 否 | - |
| `use_demo_sources` | `boolean` | 否 | - |
| `template_confirmation` | `object` | 否 | 模板确认记录；confirmed=false 时流程保持人工 Gate。 |
| `customer_answers` | `object` | 否 | 按 open_customer_questions.field 提交的明确答案；只解除同字段问题。 |
| `routing_steps` | `array` | 否 | - |
| `machine_hints` | `array` | 否 | - |
| `station` | `string` | 否 | - |
| `enable_bom_model` | `boolean` | 否 | - |
| `enable_sop_model` | `boolean` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `status` | `string` | 否 | draft_created \| human_input_required \| failed |
| `run_id` | `string` | 否 | - |
| `workflow_sequence` | `array` | 否 | 执行的步骤序列。 |
| `bom_generation` | `object` | 否 | BOM 生成结果（bom_lines + assumptions + evidence）。 |
| `sop_generation` | `object` | 否 | SOP 生成结果（Word 文档路径 + 流程图）。 |
| `open_customer_questions` | `array` | 否 | 需要人工确认的开放问题。 |
| `artifacts` | `object` | 否 | 产物文件路径。 |

### `search_m2_bom_history`

Search historical BOM library. Multi-dimensional similarity matching, returns reuse candidates + open questions.

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/bom/history/search`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_name` | `string` | 是 | - |
| `history_paths` | `array` | 否 | - |
| `keywords` | `array` | 否 | - |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `results` | `array` | 否 | - |

### `generate_m2_bom_controlled`

Controlled draft BOM generation. Based on historical BOM + rule package + product requirements.

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/bom/generate-controlled`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_profile` | `object` | 是 | - |
| `rule_package_path` | `string` | 是 | - |
| `history_paths` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `standard_bom` | `object` | 否 | - |

### `onboard_m2_bom_template`

Analyze historical BOM files, extract column structure and numbering rules, generate template proposals.

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/bom/templates/onboard`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `rule_package_path` | `string` | 是 | - |
| `history_paths` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `proposals` | `array` | 否 | - |

### `generate_m2_sop`

Generate 80806-129 format SOP Word document with process flowchart.

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/sop/generate`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_name` | `string` | 是 | - |
| `part_no` | `string` | 是 | - |
| `document_no` | `string` | 是 | - |
| `bom_items` | `array` | 否 | - |
| `routing_steps` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `status` | `string` | 否 | - |
| `artifacts` | `object` | 否 | - |

### `list_m2_runs`

只读列出 M2 已生成的 BOM/SOP run（读 workflow_manifest.json 摘要）。可按产品编码/产品名/订单号过滤。当用户问某订单/产品的 BOM 或 SOP 是否已生成、要查看已有制品时使用；返回 run_id、状态、order_id/product_code、制品链接（artifact_paths 值可直接传给前端 /api/m2/artifact?path= 下载或预览）。若按 order_id/product_code 过滤没有结果，必须去掉过滤条件再调用一次本工具列出全部 run，不要直接回答无结果。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/runs`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_code` | `string` | 否 | 产品编码过滤（可选）。 |
| `product_name` | `string` | 否 | 产品名称模糊过滤（可选）。 |
| `order_id` | `string` | 否 | 订单号过滤（可选），生成时已写入 manifest。 |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `get_m2_run`

只读获取单个 M2 BOM/SOP run 详情（workflow_manifest.json + 制品文件清单）。当用户要看某个已生成 run 的具体制品（BOM xlsx/json、SOP Word/流程图 PNG、校验 JSON）时使用；run_id 由 list_m2_runs 返回。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/runs/{run_id}`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `run_id` | `string` | 是 | M2 run_id，来自 list_m2_runs。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

# M0 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M0 工具时读取。

## M0 能力

默认服务地址：`http://m0:8010`。完整 Schema：`registry/tool-manifests/m0.json`。

### `data_import_run`

m0 数据建设：统一接收多文件（多格式×多内容类型：订单/采购/BOM/SOP/工程图/ID图/库存/设备/模具/规格书/混合归档），按 七层管道 摄入→分类→解析→实体→匹配 建批次。压缩包自动解压递归；伪扩展名/坏文件隔离不中止整批；低置信与实体冲突进入待人工确认。返回批次 id 与状态。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/import/upload?wait=false`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `files` | `array` | 是 | Base64 文件对象数组；每个对象转换为同名 files multipart 字段。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `string` | 否 | - |
| `status` | `string` | 否 | ready \| awaiting_review \| failed |

### `data_import_status`

查询导入批次状态与文档级结果（格式×内容类型×置信度×隔离）。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/import/batch/{batch_id}`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch` | `object` | 否 | - |
| `documents` | `array` | 否 | - |
| `stats` | `object` | 否 | - |

### `data_import_preview`

入库前预览：数据行、实体冲突、匹配候选、隔离区、台账。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/import/batch/{batch_id}/preview`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `documents` | `array` | 否 | - |
| `rows` | `array` | 否 | - |
| `entities` | `array` | 否 | - |
| `mappings` | `array` | 否 | - |
| `quarantine` | `array` | 否 | - |

### `data_import_resolve`

人工裁决：批准/拒绝实体冲突（同码不同名/同码不同单位）或匹配候选。裁决后批次状态重算。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/import/batch/{batch_id}/resolve/{kind}`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | - |
| `kind` | `string` | 是 | - |
| `id` | `integer` | 是 | - |
| `action` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | `integer` | 否 | - |
| `status` | `string` | 否 | - |

### `data_import_commit`

提交入库：approved 行写入 v1 主数据（订单/库存/BOM/文档），全程 ledger 可回滚。存在未裁决冲突/低置信文档时拒绝。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/import/batch/{batch_id}/commit`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `status` | `string` | 否 | - |
| `master_counts` | `object` | 否 | - |

### `data_import_rollback`

按 ledger 回滚批次全部主数据写入，证据文件保留。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/import/batch/{batch_id}/rollback`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `status` | `string` | 否 | - |
| `rolled_back` | `integer` | 否 | - |

### `data_import_history`

历史导入批次列表。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/import/batches`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batches` | `array` | 否 | - |

### `data_import_quarantine`

隔离区文件列表（坏文件/伪扩展名/未分类）。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/import/quarantine`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `items` | `array` | 否 | - |

### `data_catalog_ingest_validate`

校验 m0.ingest.v1 标准数据，不写数据库。检查稳定编码、版本、来源哈希、证据、审核状态、产品引用、产品族关系和 TaskID 范围内幂等冲突，返回 dry-run 报告。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/validate`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `data_catalog_ingest_publish`

发布已审核的 m0.ingest.v1 标准数据。在一个事务内写入规范实体版本、来源证据、产品索引关系和 Tracking Outbox；相同 TaskID+幂等键+载荷返回 duplicate，不同载荷返回冲突。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/publish`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_products_import`

M0 canonical 产品导入 facade。只接受已审核的 m0.ingest.v1 product 记录，并在 PostgreSQL canonical transaction 中写入实体、版本、来源、索引和 projection Outbox。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/products/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_orders_import`

M0 canonical 订单导入 facade。订单行和产品引用在同一 PostgreSQL transaction 中校验并写入。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/orders/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_boms_import`

M0 canonical BOM 导入 facade。BOM 头、物料身份、BOM 行、来源证据和 projection Outbox 使用同一事务。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/boms/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_materials_import`

M0 canonical 物料导入 facade。物料编码为稳定身份（不可带版本），name/unit/spec/aliases 进入同一 canonical 事务；发布后可通过 where-used 反查使用该物料的 BOM 与产品。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/materials/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_suppliers_import`

M0 canonical 供应商导入 facade。供应商编码为稳定身份，name/legal_id/status 与声明的供应物料编码（material_codes）进入同一 canonical 事务；物料入规范后自动回填 supplied_by 关系。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/suppliers/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_equipment_import`

M0 canonical 设备导入 facade。设备编码为稳定身份，name/equipment_type/line/status 进入同一 canonical 事务；工艺路线与工序实体接入后用于工序-设备反查。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/equipment/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_routes_import`

M0 canonical 工艺路线导入 facade。route_code+revision 为版本化身份，product_code 必须已是规范实体；operations 携带 sequence_no、工序/物料/设备/工装编码，未入规范的端点关系静默降级。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/routes/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_operations_import`

M0 canonical 工序导入 facade。工序编码为稳定身份，name/standard_time/status 进入同一 canonical 事务；发布后回填 has_operation 及工序-物料/设备/工装边。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/operations/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `m0_tooling_import`

M0 canonical 工装导入 facade。工装编码为稳定身份，name/tooling_type/status 进入同一 canonical 事务；发布后回填 requires_tooling 边。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/v1/tooling/import`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `records` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `data_catalog_file_validate`

把版本化 CSV/Excel 模板转换为 m0.ingest.v1 后执行 dry-run。支持 m0.product.v1、m0.product-family.v1、m0.order.v1、m0.bom.v1、m0.document.v1、m0.material.v1、m0.supplier.v1、m0.equipment.v1、m0.route.v1、m0.operation.v1、m0.tooling.v1；缺稳定编码、修订或未知列时拒绝，不从文件名或 Sheet 名猜业务身份。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/files/validate`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | `object` | 是 | - |
| `template_version` | `string` | 是 | - |
| `source_system` | `string` | 是 | - |
| `source_external_id` | `string` | 是 | - |
| `review_status` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `data_catalog_file_publish`

把已审核的版本化 CSV/Excel 模板先转换为 m0.ingest.v1，再在同一规范发布服务中写实体版本、证据、产品索引关系和 Tracking Outbox。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/files/publish`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | `object` | 是 | - |
| `template_version` | `string` | 是 | - |
| `source_system` | `string` | 是 | - |
| `source_external_id` | `string` | 是 | - |
| `review_status` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `data_catalog_document_candidate_validate`

把 m0.document-candidate.v1 解析候选转换为 m0.ingest.v1 文档记录并 dry-run。文档号、修订、角色、产品编码和证据缺一即拒绝，不按标题、文件名或名称猜产品。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/document-candidates/validate`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `candidates` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `data_catalog_document_candidate_publish`

把已审核的 m0.document-candidate.v1 候选规范化并发布，建立产品到承认书、SOP 或工程图的版本化证据关系。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/m0/catalog/ingest/document-candidates/publish`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `candidates` | `array` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `get_m0_product_overview`

按稳定产品编码查询订单、承认书、BOM 各修订、SOP、工程图、产品族和同系列产品索引，并返回版本、来源证据和同系列产品的索引数量。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/catalog/products/{product_code}/overview`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_code` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `get_m0_product_graph`

按产品编码查询 1-3 跳规范关系图；默认两跳可看到产品族和同系列产品，三跳可继续看到同系列产品的订单/BOM/承认书/SOP/工程图。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/catalog/products/{product_code}/graph`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_code` | `string` | 是 | - |
| `depth` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `list_m0_documents`

按订单/物料/产品查询 M0 已入库的工程文档（工程图 engineering_drawing、工艺方法 process_method、包装方法 packing_method、测试方法 test_method 等），返回 doc_id、doc_type、标题、状态与绑定实体。当用户问某订单/产品的工程图、图纸、工程文档时使用本工具（M0 文档库按 order/material/product 绑定），优先于 M1 文档检索。返回的 doc_id 可让前端用 /api/m0/documents/{doc_id}/file 预览或下载。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/documents`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order` | `string` | 否 | 订单号（如 SO-HIST-20260724-005），返回绑定到该订单的文档。 |
| `material` | `string` | 否 | 物料编码过滤（可选）。 |
| `product` | `string` | 否 | 产品编码/名称过滤（可选）。 |
| `doc_type` | `string` | 否 | 文档类型过滤（可选），如 engineering_drawing。 |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

### `list_m0_inventory`

查询 M0 已入库的原料库存（m0_master_inventory），返回物料编码、物料名称、数量、单位、规格、仓库。当用户问原料库存、物料库存、有什么料、某物料还剩多少、库存数量时使用本工具；可按 material_code 精确过滤单个物料。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/m0/import/master/m0_master_inventory`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `material_code` | `string` | 否 | 物料编码过滤（可选），精确匹配。 |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 否 | - |
| `data` | `object` | 否 | - |

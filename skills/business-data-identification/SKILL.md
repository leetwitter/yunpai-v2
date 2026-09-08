---
name: business-data-identification
description: Identify Yunpai business files after upload or from an authorized business-data directory, extract order/BOM/engineering metadata, preserve hashes and field evidence, and write reviewable candidates to the catalog database.
---

# 业务资料识别与候选入库

## 适用场景

当用户上传文件，或明确要求识别“云湃业务资料/业务数据”并落库时，由总规划 Agent 选择本 Skill。普通订单解析仍使用 M1 `ingest_document`；完整订单到排程主链仍使用 M0-M5 workflow。

## 接口

Skill 名称：`business-data-identification`

输入二选一：

```json
{"root_path":"/data/yunpai-business","db_path":"runtime/yunpai-business-catalog.sqlite"}
```

或：

```json
{"files":[{"filename":"订单.xlsx","content_b64":"...","content_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}]}
```

可选 `business_data_root`、`staging_dir` 和 `db_path`。上传文件会先写入任务隔离的 staging 目录，再进行识别。

## 输出与落库

返回 `schema_version=yunpai.business-catalog.v2`、`batch`、分类计数、异常计数和 `evidence`。数据库包括 `ingest_batches`、`source_files`、`document_candidates`、`field_observations`：保存路径、相对路径、扩展名、MIME、大小、修改时间、SHA-256、文档分类、订单字段、字段路径、原值/归一化值、置信度和证据定位。

外置目录批处理对 Excel 先做轻量登记并标记 `xlsx_deferred_to_m1_parser`，避免大表被误判为已解析；上传到 Skill 的小型 Excel 会在隔离 staging 中尝试 M1 级字段抽取。

识别结果始终是 `candidate`/`needs_review`，不能绕过人工审核直接写入 M0 active canonical 事实。低置信度、未知格式、冲突字段和解析错误必须保留并可定位。

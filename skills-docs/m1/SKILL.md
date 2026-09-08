---
name: yunpai-m1-document-parser
description: Parse and review Yunpai manufacturing documents, orders, drawings, spreadsheets, and archives with field-level evidence. Use for M1 ingestion, task polling, review queues, order search, reports, or legacy knowledge reads.
---

# M1 文档解析

## 职责
解析 PDF、图片、Excel/CSV、DOCX、CAD 和归档，生成 `m1.document.v2` candidate。保留原文件 hash、页码/区域/行号、parser 版本、字段置信度和校验问题；不拥有 M0 active 事实。

## 使用方式
文件使用 `ingest_document({file:{filename,content_type,content_b64}})`；归档、轮询、审核、检索或报告按需选择其余 16 个工具。调用前读取 [references/tools.md](references/tools.md) 中对应工具的 HTTP 和 JSON Schema。

## Gate
置信度 `<0.8`、订单身份/数量/交期缺失或字段冲突时返回 `needs_review`，由人工修正后再交给 M0/M2；解析失败必须返回可定位错误，不用默认值。

---
name: yunpai-m0-data-foundation
description: Govern Yunpai M0 data ingestion, canonical publishing, evidence, versioning, rollback, and master-data queries. Use for factory source data, BOM, inventory, product, supplier, equipment, route, or document ingestion and review.
---

# M0 数据基础

## 职责
把原始文件/表格/API 数据转为带 SHA-256、SourceRef、EvidenceRef、revision 的 canonical candidate，并在人工批准后以 ledger/outbox 事务发布。M0 是跨模块事实权威，Neo4j/搜索只能做可重建投影。

## 使用方式
先判断任务是导入候选、审核/回滚，还是查询 canonical 事实。上传调用 `data_import_run({files:[{filename,content_b64}]})`；批准候选后再调用 `data_import_commit({batch_id})`。选择其余 25 个工具或构造接口时，读取 [references/tools.md](references/tools.md)。

## 约束
候选不驱动业务；同编码冲突、缺来源、低置信度、跨租户引用必须 Gate。正式事实包含稳定实体 ID、版本、来源、证据和 TaskID；不能通过文件名或数组位置猜实体。

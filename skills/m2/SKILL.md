---
name: yunpai-m2-bom-sop
description: Search, generate, review, and inspect Yunpai BOM and SOP drafts and artifacts. Use for M2 historical matching, controlled BOM generation, template onboarding, SOP generation, or run lookup.
---

# M2 BOM/SOP

## 职责
读取 M0 已批准产品、物料、BOM、SOP 和历史模板，生成版本化 draft BOM 与 SOP。规则/LLM 只做候选，发布必须有 approval_ref、来源和完整工序要素。

## 使用方式
完整生成入口为 `run_bom_sop_workflow({product_profile:{product_name,product_code},...})`。单独做历史检索、受控 BOM、模板分析、SOP 或制品查询时选择专用工具；接口见 [references/tools.md](references/tools.md)。

## 失败语义
缺 `product_code` 或 BOM 行返回 `BLOCKED_INPUT`；不得把历史相似结果静默当作当前事实。Orchestrator 将 draft 转成 M3 输入，不能由浏览器拼接。

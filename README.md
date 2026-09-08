# yunpai-agent-v2 —— 云湃工业一体机 · Agent 编排重构

本仓库是云湃工业一体机（39092 交付线）**编排层从零重构**的新独立仓库。旧仓库 `yunpai-39092-repo` 的工具/技能/数据库/知识库/自进化资产以**复制迁移**方式进入本仓库；编排（路由/计划/执行/审查）全部重写。

## 为什么要重构（一句话）

旧编排的 LangGraph 图是空壳（生产走手写循环）、路由三套机制叠加且中文关键词硬编码、free 路径参数装配有实锤缺口（W913 实测死在 `ingest_m5_planning_snapshot`）。详见 [docs/01 §3](.project-to-act/docs/01-编排重构-产品与实现思路.md)。

## 核心设计

- **LangGraph StateGraph 是唯一执行路径**（checkpointer + interrupt 人工 Gate），无第二套循环。
- **三角色**：统筹 Agent（按工作流调度+路由决策）、Worker Agent（唯一装配器+工具执行）、审查 Agent（合同驱动审查规则+Gate 生命周期）。
- **先迁移、后逐个审查注册**：119 工具 + 8 技能按五项 checklist 逐个过审登记台账。
- **自进化迁入并补全闭环**：使用反馈接线、知识注入真实消费、红线周期检查。
- **API 契约兼容**现有前端（/runs 系列零改动）。

## 文档地图

| 文档 | 内容 |
|---|---|
| `.project-to-act/` 五本账本 | 本项目唯一事实源（OVERVIEW/FEATURES/PROGRESS/VERSIONS/ACCEPTANCE） |
| `.project-to-act/docs/01-编排重构-产品与实现思路.md` | **书一**：产品定位、现状诊断、新架构、迁移策略、里程碑 |
| `.project-to-act/docs/02-编排重构-细分功能与代码级方案.md` | **书二**：逐细分功能的代码级设计 + 115 工具注册审查清单 |
| `.project-to-act/docs/03-功能需求对齐手册.md` | **书三**：功能卡式需求对齐手册（是什么/为了什么/验收点，全部挂需求文档出处） |
| `.project-to-act/docs/04-任务表-v2.xlsx` | 治理 §9 规范任务表（V2-M1~M5 分阶段） |

## 资产来源

- 基线：`yunpai-39092-repo@9c7c799`（github/main，2026-09-08）
- evolution 自进化包：旧仓库主工作区未跟踪文件（与 `github/feat/evolution-20260908` 同源）
- 迁移映射表：见书一 §11、书二各章「迁移来源」

## 状态

- 2026-09-09：**仓库迁移至 `F:\Zcode\Yunpai-v2` 独立运作（彻底脱离原工作区）**；三本方案书经用户确认冻结 v1.0；V2-M1/M2 完成（feat/orchestrator-skeleton-20260909：pytest 376 绿 + API 冒烟；实现偏差见 .project-to-act/docs/05）；下一步 V2-M3 工具注册批次。

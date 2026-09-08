# yunpai-agent-v2 仓库工作规范

## 会话启动顺序

1. 先读 `.project-to-act/PROJECT_OVERVIEW.md`（五本账本是唯一事实源）。
2. 按任务读对应账本：功能→`PROJECT_FEATURES.md`；进度→`PROJECT_PROGRESS.md`；验收→`PROJECT_ACCEPTANCE.md`。
3. 写代码前读 `.project-to-act/docs/01`（书一）与 `docs/02`（书二）对应章节——**书二是代码级唯一设计依据**，实现与书二冲突时先改书（走账本记录）再改码。

## 分支纪律

- 单干线 `main`。功能分支 `feat/<功能>-<YYYYMMDD>`，修补 `fix/<说明>-<YYYYMMDD>`，从 main 拉，PR base=main。
- 提交格式 `<scope>: <change>`（scope 如 graph/router/reviewer/registry/evolution/docs）。
- 只暂存本次任务实际修改的文件；大文件（*.sqlite、*.tar.gz、*.zip、*.log、runtime 数据目录）永不进 git。

## 测试门槛

- 后端：`python -m pytest -q` 全绿才允许提交；涉及 API 契约改动必须跑契约快照测试。
- 代码改动必须同步 `.project-to-act` 账本（防漂移）。

## 关键红线

- **语义自由，非事实自由**：Agent 只做意图/分类/映射提案；事实值、计算、护栏永远确定性。Agent 决定映射绝不编造数值；事实字段必须带 sheet/行/列/原值证据；输出必过 schema 校验。
- **进化只改运行库不改代码**：对 src/、workflows/、skills/、migrations/ 只读（sha256 红线监测）。
- **DeepSeek 仅限授权任务**（进化兜底等，D-012 沿用）；生产识别/路由只用本地 Qwen。
- **路由目录只含已绑定工具**：LLM 路由提案的候选集只喂 `BOUND_LOCAL/BOUND_HTTP` 状态的工具。
- 人工 Gate（六类）不得被 LLM 置信度绕过（旧审计 P0 教训）。

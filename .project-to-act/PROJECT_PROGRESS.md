# 项目进度

> 只记录当前执行状态和少量近期节点；详细工作日志保存在项目正常材料中。

## 当前任务

| 任务 ID | 任务 | 状态 | 负责人 | 完成条件 | 证据 ID | 最后更新 |
|---|---|---|---|---|---|---|
| P-001 | 三本方案书撰写与审核入库 | 已完成 | Agent | 用户确认冻结 v1.0 | E-001 | 2026-09-09 |
| P-002 | V2-M1/M2：骨架 + 资产迁移 + 测试跑绿 | 已完成 | Agent | pytest 376 passed/4 skipped + API 冒烟全通 | E-002 | 2026-09-09 |
| P-003 | V2-M3：119 工具+8 技能逐个审查注册（R1-R7，七批制 D-004） | 进行中 | Agent | 台账 100% 登记，五项 checklist 全过 | 待补 | 2026-09-09 |

## 阻塞项

| 阻塞 ID | 阻塞 | 影响 | 解除条件 | 状态 |
|---|---|---|---|---|
| - | 无 | - | - | - |

## 下一步

1. feat/orchestrator-skeleton-20260909 收口合 main → 拉 feat/orchestrator-register-<开工日>。
2. 按 docs/07 §4 工作卡开工 R1（识别 4+M0 沙箱 5；建 test_ledger_consistency + test_bridge_rules_coverage；**E-003 四项发现随 R1 修补**：/tools 暴露与绑定表矛盾、非法参数 500、LLM workflow 提案闭环缺失、Qwen 超时/占位 key 配置）。

## 进度历史

| 日期 | 会话或任务 ID | 工作内容摘要 | 关键确认或纠正 | 证据 ID | 遗留问题 | 下一步 |
|---|---|---|---|---|---|---|
| 2026-09-09 | P-001 | 仓库建立、账本初始化；全量探查旧仓库（文档/编排/资产三路）；开始撰写 docs/01、docs/02 | 用户四项决策：独立仓库/基线 main+资产复制/只交付两本书/API 契约兼容 | 待补 | 书二 115 工具注册清单需按 manifest 实名填写（已提取） | 完成两本书并提交审核 |
| 2026-09-09 | P-001 | 书一/书二完成入库（4 commit）；按用户反馈新增书三《功能需求对齐手册》（docs/03，45+ 功能卡全部挂需求文档出处：01 产品功能/03 数据需求/04 工具接口/11 白板蓝图/自进化设计稿/09-10 原始需求）；任务表 xlsx 改号 04 | 用户反馈：书二勿动；新书要"每个功能具体是什么、为了实现什么"，须多参照需求文档（需求文档大部分没问题） | 待补 | 书三待用户逐卡审核 | 三本齐后冻结 v1.0 基线 → V2-M1 |
| 2026-09-09 | P-002 | 仓库迁至 F:/Zcode/Yunpai-v2（脱离原工作区，运行库连数据复制）；三书冻结 v1.0（D-003）；分支 feat/orchestrator-skeleton-20260909：资产迁移（115 合同+进化包+存储识别链+handler+随迁测试+local.json 识别合同）→ 新骨架（config/state/build_graph 三 Agent/装配器收编 bridge/审查规则表/绑定状态表/API 契约兼容/专属循环执行模型/Python3.12 venv）→ pytest 376 绿 + API 冒烟全通 | 实现偏差 11 条入档 docs/05；旧图强耦合测试 14 个归档 legacy_rewrite；langgraph@3.10 interrupt 坑与 AsyncSqliteSaver 循环绑定坑实测定型 | E-002 | workflow 全链（W913）与逐字段流式快照在 M5；identity 端点与 readback 在 M3 | P-003 注册批次（R1 识别+M0 起） |
| 2026-09-09 | P-002 | 生成交接包 F:/Zcode/Yunpai-v2-交接包-20260909（git bundle 全历史+源码 zip+5 库快照+三书账本+验证证据 pytest376/冒烟+HANDOFF）；HANDOFF 副本入库 docs/06-交接-v2.md | 应用户要求 | E-002 | - | P-003 注册批次 |
| 2026-09-09 | P-003 | 读取交接包续建开发方案：docs/07《V2-M3 注册批次开发方案》（事实基线实测+R1-R6 工作卡+批次修订 D-004 建议+防漂移机制 test_ledger_consistency）+《注册审查台账》初值入库（129 行全实名=119 工具+8 技能+2 反馈面，绑定/装配/审查列程序化导出自代码实况）；实测挖出 5 个审查疑点（m3 审批 blocked_input 门错位、审批 4 件缺默认隐藏机制、import_m4_purchase_suggestions 未绑定、M0/M2 29 件 http-only 口径、replan/dispatch 缺 plan_version 桥） | 书一 §12.3 与书二 §7.3 批次总数账矛盾（106≠129，M0 余 23 无批）由 docs/07 §2 裁定为 D-004 建议，待用户确认后回写两书 | 待补 | 生成脚本已删（防误跑覆盖台账）；identity 端点归 R2a、evolution/shortcuts 归 M4 | 用户确认 D-004 → skeleton 合 main → R1 开工 |
| 2026-09-09 | P-003 | 开工门基线复验（用户指令，含 DeepSeek 真实 LLM 测试）：.venv(Py3.12) pytest 376 绿对齐 E-002；注册目录静态对账全对上（119=115+4、四态 30/53+1/35、红线无泄漏、production 降级、8 技能）；路由三层链验证=专项单测 18/18 + 活体冒烟 9 用例×2 配置（确定性 + DeepSeek 真实 LLM 经 QwenRouter 同码路径），LLM 提案 chat/free 全链走通、护栏与 CatalogView 过滤经真实模型验证有效；真实后端对比 DeepSeek 1-3s vs 本地 Qwen 49-76s | DeepSeek 仅为测试替身注入测试进程 env（红线：生产识别/路由仍只用本地 Qwen，未改任何生产配置/代码）；4 项发现（/tools 暴露与 bound 矛盾、非法参数 500、LLM workflow 提案闭环缺失、本地 Qwen 超时>45s 默认值）详见 E-003 | E-003 | 默认 python=3.10 与 .venv 并存易误用（6 failed 已归因为环境非回归） | 用户确认 D-004 → skeleton 合 main → R1 开工（4 项发现并入 R1 修补清单） |
| 2026-09-09 | P-003 | D-004 批次裁定落地（用户单选确认）：注册批次六批扩为七批——M0 余 23 单独成新 R3，原 R3-R6 顺延为 R4-R7（9+24+23+20+26+17+10=129 对平）；回写书一 §12.3/§13.1、书二 §7.3/§14、docs/05 偏差记录 #12；docs/07 全篇与《注册审查台账》129 行按 R1-R7 重排；E-003 四项发现并入 docs/07 R1 工作卡 | 用户选择"M0 余 23 单独成批"（三选一，AskUserQuestion） | 无 | - | skeleton 合 main → R1 开工（含 E-003 四项修补） |
| 2026-09-09 | P-003 | LLM workflow 提案闭环修复 + WF 全链闭环验证（用户指令）：①按书二 §4.1 补齐路由 prompt 工作流目录（llm.py 三处+7 测试）；②E2E 依次挖出并修复 stale-resume 卡死（非法决策改再挂起 gate_invalid）、工程门 approval_status 落点缺失、checkpoint/runs 同库写锁竞争（checkpoint 分库+镜像重试退避）、多行订单 m5 快照批内 UNIQUE 冲突（行号按序唯一）——共 +4 测试，pytest 387 绿；③终局 E2E：DeepSeek 真实 LLM 提案 workflow=m1_m5_document_to_plan → 上传带码订单（库存/供应商/资源/日历/路线/供应六类权威事实顶层喂入）→ **9/9 步 completed**，5 次 Gate 审批（review×2/候选裁决/工程/apply），排程 plan-scenario-WX20260905001-d80bab1814 回读成功，response=「业务链执行完成：9/9 步，Gate 审批 5 次」 | 六类快照的权威事实要求逐项实测：缺一即确定性阻断并给精确补数指引（「语义自由非事实自由」红线经真实链路验证）；新遗留观察：TOOL_ERROR 步骤会被审查默认放行标 completed → 已修复（见偏差 #15）；偏差 #13/#14 入档 docs/05 | 待补（pytest 387 绿 + E2E run-95a494af，临时脚本在 %TEMP%\yunpai-verify） | - | skeleton 合 main → R1 开工 |
| 2026-09-09 | P-003 | 链路功能/逻辑筛查（用户指令，后续工作地基）：读全 planner/workflow_engine/executor/state/repository/graph/rules/contracts 主干，实测三处「假成功」并修复——①工具异常被审查静默标 completed（复现：无规则工具抛异常 → run=completed）→ failed 步骤按重试余量重试、耗尽即 failed，executor 对非阻塞 success=False 也标 failed；②finalize 对 blocked/pending/running 残留误判 completed → failed>残留>completed；③LLM free 空工具+空技能空转「0 步完成」→ _validate 拒绝。共 +3 测试，pytest **390 绿**；偏差 #15 入档 docs/05 | 另两项记录不修（待定）：① spec.review_gate 合同默认门是死代码（RULES 表外工具的合同门不触发，default_gate_for_authorized 零调用，属 docs/05 #9 注册批次 R2-R5 接线）；② 行标识不一致——M1 line_id={order}::{sheet}!R{row} 与桥接 order_line_id={order}::L{index} 从未打通，真实多源数据会错配，需统一约定后透传 | 待补（pytest 390 绿） | - | skeleton 合 main → R1 开工（行标识约定 + 默认门接线并入 R1） |

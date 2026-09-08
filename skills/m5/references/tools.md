# M5 工具接口

本文由工具 manifest 自动生成；只在需要选择或调用 M5 工具时读取。

## M5 能力

默认服务地址：`http://m5-api:8000`。完整 Schema：`registry/tool-manifests/m5.json`。

### `solve_scheduling`

幂等求解并持久化一版 M5 排程候选。输入真实订单、已审批工艺/工时、资源、班次及幂等键，输出 draft 计划、校验报告和版本号。生产排程、后续人工审批/发布或动态重排必须使用本工具；缺少外部事实时失败关闭。缺权威数据（订单/工艺/资源/物料可用性/生产单元映射）时由编排层返回可恢复 data_incomplete（数据未完善）；本工具不补造默认库存、批次或生产单元。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/schedule-candidates`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `idempotency_key` | `string` | 是 | 调用方生成的稳定幂等键；同键不同输入返回 409。 |
| `expected_head_plan_version` | `string | null` | 否 | 场景已有计划时必须给出预期 head；首次创建为 null。 |
| `scenario_id` | `string` | 是 | - |
| `scenario_purpose` | `string` | 否 | 机器可读的生命周期身份；pressure_only 只允许诊断求解，不得审批、发布或派工。 |
| `production_units` | `array` | 否 | 调用方声明的稳定生产单元映射；生产生命周期要求可信适配器核验且完整覆盖订单、资源、日历和仓库。 |
| `cross_unit_order_dependencies` | `array` | 否 | 不得在分区时静默截断的显式跨单元订单依赖。 |
| `planning_start` | `string` | 是 | - |
| `solver` | `string` | 否 | - |
| `explain_with_llm` | `boolean` | 否 | 遗留兼容字段，核心求解路径忽略该开关且只返回规则解释；需要 LLM 说明时调用 run_m5_intelligent_schedule。 |
| `source_systems` | `array` | 否 | 本次规划事实的来源系统；包含非 manual 来源时必须提供逐来源观测时间。 |
| `observed_at` | `string | null` | 否 | 兼容旧调用的整包观测时间；未提供 source_observed_at 时应用于全部已声明外部来源。 |
| `source_observed_at` | `object` | 否 | 逐来源权威观测时间；每个外部来源分别接受 24 小时 freshness 校验，新来源不能刷新其他来源。 |
| `orders` | `array` | 是 | - |
| `routing_steps` | `array` | 是 | - |
| `resources` | `array` | 是 | - |
| `tooling_resources` | `array` | 否 | 模具、治具、工装主数据：tooling_id/name/quantity_available/status/compatible_product_ids/compatible_setup_families。 |
| `tooling_requirements` | `array` | 否 | 工序所需模治具：operation_id/tooling_id/product_id/quantity。 |
| `changeover_rules` | `array` | 否 | 相邻任务换型矩阵：from_setup_family/to_setup_family/changeover_minutes，可按 resource_id 限定。 |
| `missing_changeover_policy` | `string` | 否 | 缺少换型规则时默认禁止该切换。 |
| `labor_skills` | `array` | 否 | 稳定技能主数据：skill_id/version/effective interval/production_unit_ids/capacity_unit/source classification。 |
| `labor_requirements` | `array` | 否 | 工序技能需求：operation_id/skill_id/skill_version/production_unit_id/quantity，可按 product_id 限定。 |
| `labor_capacity_windows` | `array` | 否 | 人员能力窗口：skill_id/skill_version/production_unit_id/capacity_source_id/aggregation_mode/start_time/end_time/capacity/is_overtime。 |
| `labor_capacity_adjustments` | `array` | 否 | 缺勤或临时能力调整：adjustment_id/skill/version/unit/start/end/capacity_delta/reason_code。 |
| `calendar_windows` | `array` | 否 | - |
| `resource_unavailability` | `array` | 否 | 设备/产线停机窗口：resource_id/start_time/end_time/reason/confirmed。 |
| `order_kitting` | `array` | 否 | 订单齐套状态与最早齐套时间。 |
| `wip_status` | `array` | 否 | 一线在制状态：中间工序必须提供 route_version 和完整 completed_operation_ids 证据。 |
| `manual_locks` | `array` | 否 | 人工锁单：order_id/operation_id/resource_id/start_time/end_time/reason/locked_by；start_time 和 end_time 必须相对 planning_start 为整分钟。planning_start 可带秒或微秒。 |
| `frozen_windows` | `array` | 否 | 计划冻结窗口：start_time/end_time/reason，可按 resource_id 限定。 |
| `material_availability` | `array` | 否 | - |
| `material_substitutions` | `array` | 否 | - |
| `product_bom_items` | `array` | 否 | - |
| `pmc_rules` | `array` | 否 | - |
| `material_policy` | `object` | 否 | - |
| `predicted_operation_parameters` | `array` | 否 | - |
| `optimization_weights` | `object` | 否 | 目标权重：makespan/tardiness/setup_time/resource_preference/changeover_time/changeover_count/labor_overtime。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_schedule`

按 plan_version 查询权威持久化计划详情、lifecycle_status、校验状态、父版本、触发事件和输入哈希。审批、发布、派工或重排前用于确认当前版本状态。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/schedules/{plan_version}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_version` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_pmc_progress`

按不可变 plan_version 查询可在正式 Agent 展示的 M5 生产上下文。仅接受 production、released、validation-passed 且为当前 scenario head 的计划；否则拒绝。返回 PMC 计算计划、M5 资源主数据、已接受的非 simulation 执行证据及人工维护的订单级人员绑定。人员绑定不代表 HR、考勤或实时在岗。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/pmc/progress`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_version` | `string` | 是 | 要查询的权威排程版本号。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `replan_m5_schedule`

从已持久化的权威计划输入应用一个幂等生产事件并动态重排。服务端恢复原始输入，校验场景 head，禁止客户端用陈旧或伪造的完整快照覆盖当前状态。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/schedules/{base_plan_version}/replan-from-version`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `base_plan_version` | `string` | 是 | 当前场景的权威 head 版本。 |
| `idempotency_key` | `string` | 是 | - |
| `expected_head_plan_version` | `string | null` | 否 | 省略时等于 base_plan_version。 |
| `event` | `object` | 是 | - |
| `freeze_policy` | `object | null` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_integration_contracts`

查询 M5 与 M1-M4、M6-M8 按方向和交付项拆分的接口需求、readiness（available/partial/proposed/blocked/current_none）及缺口。返回的 JSON Schema 只是 M5 侧载荷结构，不代表对端接口、生成、持久化、投递或回执已经可用。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/contracts`，超时 `60s`

输入：

无固定顶层字段；以 JSON Schema 的组合约束为准。

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `list_m5_schedules`

查询 M5 历史排程版本列表，可按 scenario_id 过滤、限制返回条数。scenario_id 遵循约定格式 scenario-{订单号}（例如订单 SO-HIST-20260724-005 的场景是 scenario-SO-HIST-20260724-005）；当用户问某订单的排程/排程版本/历史排程时，优先用该格式构造场景过滤，或先用 get_business_order_trace 获取订单排程摘要中的 plan_version。返回排程版本摘要（不含完整 operations，避免响应过大）。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/schedules`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scenario_id` | `string` | 否 | 可选场景过滤。 |
| `limit` | `integer` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_material_readiness`

查询某场景是否具备生产排程所需事实，并返回逐检查项的 pass/warn/fail；其中 material-or-kitting 仅汇总判断是否提供物料可用量或订单齐套证据。它不返回逐物料到位/缺料清单或预计到料时间；需要逐料采购缺口与需求时间时调用 generate_m5_material_procurement_plan。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/integrations/snapshots/{scenario_id}/readiness`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scenario_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `search_m5_knowledge`

检索 M5 内置知识库中与给定特征相似的历史排程案例（RAG）。当用户问‘类似订单以前怎么排的’‘有没有相似场景的经验’‘这种产品排程要注意什么’时使用。输入特征描述当前场景，返回 top_k 个最相似的历史案例及结果。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/knowledge/search`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `features` | `object` | 是 | 当前场景的特征描述（产品类型、订单规模、交期紧迫度等），用于检索相似历史案例。 |
| `top_k` | `integer` | 否 | - |
| `outcome_filter` | `string` | 否 | 可选：只返回某结果类型的案例。 |
| `tag_filter` | `string` | 否 | 可选：按标签过滤。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `record_m5_knowledge`

把一个已持久化的 M5 排程版本沉淀为知识案例。只提交 plan_version、标签和备注；输入、结果及案例哈希由 M5 从数据库权威版本读取。版本或权威载荷缺失时返回“数据未完善”，不会使用调用方副本补造事实。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/knowledge/record`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_version` | `string` | 是 | - |
| `tags` | `array` | 否 | - |
| `note` | `string` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `prepare_m5_department_message`

根据排程、异常或执行反馈的结构化证据起草企业部门消息并持久化为 pending_approval。LLM 只可生成 subject/body；部门、渠道、收件人、证据与幂等键由调用方提供。本工具不会审批或直接发送。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/messages/prepare`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `department` | `string` | 是 | - |
| `channel` | `string` | 是 | - |
| `recipient_targets` | `array` | 是 | - |
| `message_kind` | `string` | 是 | - |
| `event_summary` | `string` | 是 | - |
| `required_action` | `string` | 是 | - |
| `evidence` | `array` | 是 | - |
| `idempotency_key` | `string` | 是 | - |
| `template_key` | `string | null` | 否 | - |
| `template_variables` | `object` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_department_message`

查询部门消息草稿及人工审批状态。消息只有 approved 后才会进入耐久 Outbox；LLM 或 Agent 无权代替审批人。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/messages/{draft_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `draft_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_department_message_delivery`

查询已批准部门消息的耐久 Outbox 投递状态、尝试次数和 provider message id。不会触发重试或绕过审批。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/messages/outbox/{outbox_id}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `outbox_id` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `advise_m5_schedule`

基于已有排程结果给出 PMC 辅助决策建议——催料、加班、协商交期等软约束优化方向。当排程结果准交率不理想或存在延期，用户问‘怎么办’‘怎么改善’‘能不能优化’时使用。输入是排程结果（ScheduleResponse），输出是建议动作清单。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/schedules/advise`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scenario_id` | `string` | 是 | - |
| `scenario_purpose` | `string` | 是 | - |
| `plan_version` | `string` | 是 | - |
| `solver_status` | `string` | 是 | - |
| `operations` | `array` | 是 | - |
| `metrics` | `object` | 是 | - |
| `validation_report` | `object` | 是 | - |
| `risks` | `array` | 否 | - |
| `messages` | `array` | 否 | - |
| `order_kitting` | `array` | 否 | - |
| `prediction_summary` | `object | null` | 否 | - |
| `predicted_operation_parameters` | `array` | 否 | - |
| `solver_diagnostics` | `object | null` | 否 | - |
| `explanation` | `object | null` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `run_m5_intelligent_schedule`

对排程输入执行非持久化分析，并可选生成 LLM 解释、审计文字和决策建议。它不会创建可审批计划；生产生命周期必须先调用 solve_scheduling 持久化候选。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/schedules/intelligent`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `schedule_request` | `object` | 是 | 基础排程请求；外部来源必须带 source_systems 与逐来源观测时间。此入口只做非持久化分析，不接受重排事件或冻结策略。 |
| `enable_audit` | `boolean` | 否 | 是否生成审计报告。 |
| `enable_advise` | `boolean` | 否 | 是否生成决策建议。 |
| `enable_knowledge_record` | `boolean` | 否 | 兼容开关；智能排程不会写入未绑定案例。请先持久化计划，再调用 record_m5_knowledge。 |
| `auto_replan_on_validation_fail` | `boolean` | 否 | 兼容字段；校验失败现在会失败关闭，不再隐式切换到 baseline。 |
| `knowledge_tags` | `array` | 否 | 兼容字段；本工具不直接沉淀，标签应传给 record_m5_knowledge。 |
| `knowledge_note` | `string` | 否 | 兼容字段；本工具不直接沉淀，备注应传给 record_m5_knowledge。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `dispatch_m5_schedule`

为 lifecycle_status=released 的计划创建 M5 本地耐久待派工记录。必须显式给出工序键和幂等键；当前 M5 没有 MES sender/Provider worker，本调用不会把记录发送到 MES，初始状态只能是 pending。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/schedules/{plan_version}/dispatch`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_version` | `string` | 是 | 要派工的排程版本（路径参数）。 |
| `target_system` | `string` | 否 | 目标系统，当前固定 mes。 |
| `requested_by` | `string` | 否 | 可选兼容字段；服务端审计身份始终取认证 principal，不信任调用方自报值。 |
| `idempotency_key` | `string` | 是 | 稳定幂等键；同键异内容返回冲突。 |
| `operation_keys` | `array` | 是 | 显式工序键，格式 order_id:operation_id；未知、空或重复均拒绝。 |
| `payload` | `object` | 否 | 可选附加下发载荷。 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `get_m5_execution_summary`

仅汇总该计划版本中已经通过执行事件接口持久化的数据：事件数、最新事件发生时间、可计算的开始/结束偏差、延期工序数、异常数和报废原始值。它不采集现场数据，也不证明车间完整执行；不返回良品完工、人工/设备/setup/工装/能耗分项，不能替代 m5.actual-operation-evidence.v1 或 m5.cost-consumption.v1 交付。

- 类型：`tool`
- 执行：`sync`
- HTTP：`GET /api/v1/schedules/{plan_version}/execution-summary`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `plan_version` | `string` | 是 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `ingest_m5_planning_snapshot`

把上游（ERP/MES/WMS/PLM 或人工）的规划数据快照写入 M5：订单、工艺路线、资源、工装等。当需要把外部系统的最新主数据/订单数据灌进 M5 再排程时使用，是 M5 接收上游数据的集成入口。replace_existing=true 会覆盖同 scenario 的旧快照。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/integrations/snapshots`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scenario_id` | `string` | 是 | - |
| `scenario_purpose` | `string` | 否 | 机器可读的生命周期身份；pressure_only 快照生成的计划不得审批、发布或派工。 |
| `production_units` | `array` | 否 | 调用方声明的稳定生产单元映射；生产生命周期另行核验。 |
| `cross_unit_order_dependencies` | `array` | 否 | - |
| `planning_start` | `string` | 否 | - |
| `solver` | `string` | 否 | - |
| `explain_with_llm` | `boolean` | 否 | - |
| `source_systems` | `array` | 否 | - |
| `observed_at` | `string | null` | 否 | 兼容旧调用的整包观测时间；未提供 source_observed_at 时应用于全部已声明外部来源。 |
| `source_observed_at` | `object` | 否 | 逐来源权威观测时间；增量合并分别保存并校验，缺失或陈旧来源会失败关闭。 |
| `replace_existing` | `boolean` | 否 | true 整包替换该场景；false 按业务稳定键增量合并，未提供的实体保持不变。 |
| `orders` | `array` | 否 | - |
| `routing_steps` | `array` | 否 | 工艺路线；eligible_resources 支持 processing_minutes/setup_minutes/cycle_minutes/batch_size/cavity_count/yield_rate，setup_family 用于换型。 |
| `resources` | `array` | 否 | 设备/产线资源；支持 status 与计划开始时已安装模具 initial_setup_family。 |
| `tooling_resources` | `array` | 否 | - |
| `tooling_requirements` | `array` | 否 | - |
| `changeover_rules` | `array` | 否 | from_setup_family/to_setup_family/changeover_minutes/resource_id。 |
| `missing_changeover_policy` | `string` | 否 | - |
| `labor_skills` | `array` | 否 | skill_id/version/effective interval/production_unit_ids/capacity_unit/source classification。 |
| `labor_requirements` | `array` | 否 | operation_id/skill_id/skill_version/production_unit_id/quantity/product_id。 |
| `labor_capacity_windows` | `array` | 否 | skill_id/skill_version/production_unit_id/capacity_source_id/aggregation_mode/start_time/end_time/capacity/is_overtime。 |
| `labor_capacity_adjustments` | `array` | 否 | adjustment_id/skill/version/unit/start/end/capacity_delta/reason_code。 |
| `calendar_windows` | `array` | 否 | - |
| `resource_unavailability` | `array` | 否 | - |
| `material_availability` | `array` | 否 | - |
| `order_kitting` | `array` | 否 | - |
| `wip_status` | `array` | 否 | order_id/operation_id/resource_id/actual_start_time/estimated_remaining_minutes/completed_quantity/status。 |
| `manual_locks` | `array` | 否 | 人工锁单时间必须相对 planning_start 为整分钟；planning_start 可带秒或微秒。 |
| `frozen_windows` | `array` | 否 | - |
| `pmc_rules` | `array` | 否 | - |
| `material_substitutions` | `array` | 否 | - |
| `product_bom_items` | `array` | 否 | - |
| `optimization_weights` | `object` | 否 | - |
| `material_policy` | `object` | 否 | - |
| `predicted_operation_parameters` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `generate_m5_material_procurement_plan`

基于排程请求生成物料采购计划（哪些料够、哪些要采、采多少、何时要）。当用户问‘排这单要采购哪些料’‘算一下采购需求’‘物料采购计划’时使用。输入与 solve_scheduling 相同的订单/工艺/资源，输出采购计划视角的结果。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/materials/procurement-plan`，超时 `120s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `scenario_id` | `string` | 是 | - |
| `scenario_purpose` | `string` | 否 | - |
| `production_units` | `array` | 否 | 调用方声明的稳定生产单元映射；生产生命周期另行核验。 |
| `cross_unit_order_dependencies` | `array` | 否 | - |
| `planning_start` | `string` | 否 | - |
| `solver` | `string` | 否 | - |
| `explain_with_llm` | `boolean` | 否 | - |
| `source_systems` | `array` | 否 | - |
| `observed_at` | `string | null` | 否 | - |
| `source_observed_at` | `object` | 否 | - |
| `orders` | `array` | 是 | - |
| `routing_steps` | `array` | 是 | - |
| `resources` | `array` | 是 | - |
| `calendar_windows` | `array` | 否 | - |
| `optimization_weights` | `object` | 否 | - |
| `material_availability` | `array` | 否 | - |
| `material_substitutions` | `array` | 否 | - |
| `product_bom_items` | `array` | 否 | - |
| `material_policy` | `object` | 否 | - |
| `order_kitting` | `array` | 否 | - |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `report_workload`

【报工工具，必须调用】工人或组长用自然语言报工并落库。只要用户表达了开工、完工、报产、报废、工时等信息，就必须调用本工具真正落库，不要只用文字回应或谎称已记录。触发语（含但不限于）：「开工」「开始做」「完工」「做完了」「报数 200」「报废 5 个」「做了 8 小时」「今天 SO-xxx 做了 300 个，花了 6 小时」。参数从用户原话提取：worker_id=工号，order_id=订单号，event_type：开工=actual_start，完工=actual_finish，报产=quantity_report，报废=scrap，异常=exception；reported_quantity 只表示本次新增产量（不是累计快照），reported_unit 必须从原话提取并标准化，scrap_quantity=报废数，actual_min=工时分钟，shift_date=日期。若数量/单位疑似错位、单位不确定或无法与计划匹配，先审核澄清，不得猜测落库。服务端仅在数量单位、权威排程订单产品与单位三者验证一致后计入 PMC 实际量。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/worker/report`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `worker_id` | `string` | 是 | 报工工人工号，如 worker-001；组长替组员报工时代入该组员工号 |
| `plan_version` | `string | null` | 否 | 工人任务接口返回的 released 场景头计划版本；多工序报工时与 operation_id、resource_id 一并原样提交 |
| `order_id` | `string` | 是 | 订单号，如 SO-HIST-20260724-001 |
| `operation_id` | `string | null` | 否 | 用户明确说出的工序键；多工序订单未绑定唯一工位时必须提供。 |
| `resource_id` | `string | null` | 否 | 工人任务接口返回的权威排程资源键；多工序报工时与 plan_version、operation_id 一并原样提交 |
| `station` | `string | null` | 否 | 工位展示名称，用于计件归类；工序和资源身份仍以 operation_id、resource_id 为准 |
| `event_type` | `string` | 是 | 报工事件类型；「开工/开始做」→actual_start，「完工/做完了」→actual_finish，「做了 N 个/报数」→quantity_report，「报废 N 个」→scrap，「异常」→exception |
| `reported_quantity` | `number | null` | 否 | 本次报工新增的产出量（delta，不是累计快照）；quantity_report 时提供，并用 reported_unit 显式声明单位 |
| `reported_unit` | `string | null` | 否 | reported_quantity 的单位；必须从用户原话提取并标准化（如 个/件→pcs、米→m），且与权威排程订单单位一致。不确定或疑似字段错位时先审核，禁止猜测后落库。 |
| `scrap_quantity` | `number` | 否 | 报废数量（scrap 时提供） |
| `actual_min` | `number | null` | 否 | 实际工时（分钟）；用户说「6 小时」应换算为 360 分钟 |
| `shift_date` | `string | null` | 否 | 报工日期 YYYY-MM-DD；缺省为今天 |
| `reason` | `string | null` | 否 | 异常/报废原因 |
| `idempotency_key` | `string | null` | 否 | 幂等键；同一次报工重复提交用同一键避免重复落库 |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

### `bind_worker_to_order`

组长给生产订单绑定组内工人（订单级绑定，用于工人自助报工的订单隔离）。当用户说「把 SO-xxx 订单绑定给王五、李四」「给这个订单分配工人」时使用。需要订单号 order_id、工人列表 worker_ids、班组 team_id。

- 类型：`tool`
- 执行：`sync`
- HTTP：`POST /api/v1/leader/bindings`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 是 | 订单号 |
| `worker_ids` | `array` | 是 | 要绑定的工人工号列表，如 ["worker-001", "worker-002"] |
| `team_id` | `string` | 是 | 班组 id（组长自己的班组） |
| `resource_id` | `string | null` | 否 | 工位（可选，缺省从排程反推） |
| `station` | `string | null` | 否 | 工序/工位名（可选） |

输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `success` | `boolean` | 是 | - |
| `data` | `object` | 是 | - |
| `errors` | `array` | 是 | - |
| `trace_id` | `string` | 是 | - |

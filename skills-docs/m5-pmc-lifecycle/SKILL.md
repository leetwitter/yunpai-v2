---
name: yunpai-m5-pmc-lifecycle
description: Operate the M5 plan lifecycle through registered schedule tools: inspect versions, replan, dispatch, and read execution feedback without bypassing approval or production gates.
---

# M5 PMC 生命周期

## 适用场景

当用户需要查看排程版本、比较当前计划、按生产事件重排、创建派工记录、查看报工/执行偏差，或使用 M5 Flow Board 的运营能力时，由 Planner Agent 选择本 Skill。

## 工具映射

| 操作 | Tool | 说明 |
| --- | --- | --- |
| `versions` | `list_m5_schedules` | 查询 scenario 的不可变版本摘要 |
| `schedule` | `get_m5_schedule` | 查询计划、生命周期和校验结果 |
| `progress` | `get_m5_pmc_progress` | 查询当前已发布计划的 PMC 上下文 |
| `replan` | `replan_m5_schedule` | 从服务端版本恢复输入并应用生产事件 |
| `dispatch` | `dispatch_m5_schedule` | 为 released 计划创建待派工记录 |
| `execution` | `get_m5_execution_summary` | 汇总已持久化的执行事件和偏差 |

调用时可使用：

```json
{
  "skill": "yunpai-m5-pmc-lifecycle",
  "skill_payload": {
    "operation": "versions",
    "tool_payload": {"scenario_id": "scenario-SO-001", "limit": 20}
  }
}
```

## 生命周期约束

- 该 Skill 只能调用 `SkillRegistry` 白名单内的 M5 Tool，所有输入仍经过原始 JSON Schema 校验。
- `replan` 必须提供服务端认可的 `expected_head_plan_version`、事件和幂等键；客户端不能用陈旧完整快照覆盖权威输入。
- `dispatch` 仅允许 `lifecycle_status=released` 的计划，且只创建耐久待派工记录；当前没有 MES sender/provider 时不得宣称已发送。
- `get_m5_pmc_progress`、执行摘要和消息查询是只读检查，不能替代人工批准、发布或现场回传。
- `pressure_only`、`draft`、`production_blocked` 计划不得进入生产派工。

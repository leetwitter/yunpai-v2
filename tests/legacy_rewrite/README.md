# 旧图/旧 App 强耦合测试（书二 §12.2 需重写清单）

以下测试依赖 legacy 的 PlannerAgent/ReviewerAgent/YunpaiGraph/旧 create_app，
随迁移保留作重写参考（改名 legacy_ 前缀，不进 pytest 收集）：

- legacy_test_llm / legacy_test_business_skill / legacy_test_skill_blind_metrics：旧 PlannerAgent 路由行为（v2 对应 tests/test_router.py）
- legacy_test_m1_skill_operations / legacy_test_m5_skill_operations / legacy_test_m1_real_order_replay：技能经旧 ReviewerAgent 的集成（M3 注册批次按模块重写）
- legacy_test_m2_qwen_endpoint / legacy_test_m5_graph_persistence：旧 YunpaiGraph/旧 App 行为（v2 对应 tests/test_graph_smoke.py + M5 联调）
- legacy_test_identity_login / legacy_test_identity_shadow：旧 App 身份端点（M3 身份端点批次重写）
- legacy_test_registry：旧双目录 manifest 一致性/SOURCE_PROVENANCE/m0=32 口径（fin-m6 分支账本漂移现场；v2 单目录 registry-manifests 政策下重写为 tests/test_binding.py）
- legacy_test_skill_upgrade_regression：旧确定性 Planner 复现测试（v2 对应 tests/test_router.py）

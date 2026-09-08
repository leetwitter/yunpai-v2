"""migrations RLS 静态守护（P4.1）。

本机（Windows）无 docker daemon / psql / psycopg2，无法起 PG 实测双租户隔离；
按交接约定交付 SQL + 评审记录。此测试只做**静态**检查：迁移文件对全部表
启用了 RLS 且策略统一走 current_setting('app.tenant_id')——不代表已验证。
上 PG 环境后需补真实双租户用例。
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "m0_backend_v1.sql"

_ALL_TABLES = (
    "import_batches", "source_documents", "import_candidates", "approval_records",
    "canonical_entities", "canonical_entity_versions", "canonical_ledger",
    "canonical_outbox", "entity_relations", "entity_aliases",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_covers_all_tables_with_rls():
    sql = _sql()
    for table in _ALL_TABLES:
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(", sql), f"缺表 {table}"
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql, f"{table} 未启用 RLS"
        # 每张表的策略块都引用租户上下文（子表经父表 EXISTS，直连表按列）。
        block = sql.split(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")[1]
        policy = block.split("ALTER TABLE")[0]
        assert "CREATE POLICY tenant_isolation" in policy, f"{table} 缺策略"
        assert "current_setting('app.tenant_id', true)" in policy, f"{table} 策略未走 app.tenant_id"
        assert "WITH CHECK" in policy, f"{table} 策略未约束写入"


def test_child_table_policies_join_parent_tables():
    sql = _sql()
    # 子表策略必须经父表推导租户，不得出现「无 tenant 依据」的直通策略。
    assert "b.batch_id = source_documents.batch_id" in sql
    assert "e.entity_id = canonical_entity_versions.entity_id" in sql
    assert "l.ledger_id = canonical_outbox.ledger_id" in sql
    assert "b.batch_id = entity_relations.source_batch_id" in sql
    assert "e.entity_id = entity_aliases.entity_id" in sql


def test_migration_documents_unverified_status_and_app_role_requirement():
    header = _sql().split("CREATE SCHEMA")[0]
    assert "未连接真实 PG 验证" in header
    assert "FORCE ROW LEVEL SECURITY" in header  # 评审记录说明为何未启用 FORCE
    assert "SET app.tenant_id" in header  # 应用接入方式

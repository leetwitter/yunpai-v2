-- M0 PostgreSQL schema（canonical 主数据专用）。
--
-- P4.1（2026-09-07）：补齐 RLS 策略（此前无任何 RLS，仅设计稿）。
-- 评审记录（未连接真实 PG 验证，交付 SQL + 评审，不声称已验证）：
--   * 租户上下文取 current_setting('app.tenant_id', true)：连接未设置时返回
--     NULL，比较结果为假 → 所有行不可见（fail-closed，与 API/M1 侧语义一致）。
--     应用连接需在会话级执行 `SET app.tenant_id = '<tenant>'`（连接池按租户
--     绑定连接，或事务级 SET LOCAL）。
--   * 有 tenant_id 列的表（import_batches/canonical_entities）直接按列过滤；
--     无租户列的子表经父表 EXISTS 连接推导（batch→import_batches；
--     version/ledger/alias→canonical_entities；outbox→ledger→entities；
--     relation→import_batches.source_batch_id），不改应用写入路径。
--   * 策略为 FOR ALL（读+写一致约束）。
--   * 未启用 FORCE ROW LEVEL SECURITY：表 owner/超级用户/BYPASSRLS 角色不受
--     约束。部署要求：应用不得用 owner 身份连库——建独立 app 角色（默认无
--     BYPASSRLS）；待应用侧接入逐连接 SET app.tenant_id 后再评估开启 FORCE。
--   * 本机（Windows）无 docker daemon/psql，未做双租户实测；上 PG 环境后需
--     按 tests 计划补「双租户隔离用例」再宣称验证通过。
CREATE SCHEMA IF NOT EXISTS yunpai_m0;
SET search_path TO yunpai_m0;

CREATE TABLE IF NOT EXISTS import_batches (
  batch_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,
  status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS source_documents (
  document_id UUID PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
  filename TEXT NOT NULL, sha256 TEXT NOT NULL, content_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS import_candidates (
  candidate_id UUID PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
  document_id UUID NOT NULL REFERENCES source_documents(document_id),
  entity_type TEXT NOT NULL, candidate_json JSONB NOT NULL, status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS approval_records (
  approval_id UUID PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
  candidate_id UUID, actor TEXT NOT NULL, decision TEXT NOT NULL,
  approval_mode TEXT NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS canonical_entities (
  entity_id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, entity_type TEXT NOT NULL,
  canonical_key TEXT NOT NULL, current_version INTEGER NOT NULL,
  lifecycle_status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(tenant_id, entity_type, canonical_key)
);
CREATE TABLE IF NOT EXISTS canonical_entity_versions (
  entity_id UUID NOT NULL REFERENCES canonical_entities(entity_id), version INTEGER NOT NULL,
  payload_json JSONB NOT NULL, checksum TEXT NOT NULL, source_batch_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(entity_id, version)
);
CREATE TABLE IF NOT EXISTS canonical_ledger (
  ledger_id UUID PRIMARY KEY, batch_id TEXT NOT NULL, entity_id UUID NOT NULL,
  version INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
  approval_mode TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS canonical_outbox (
  outbox_id UUID PRIMARY KEY, ledger_id UUID NOT NULL, event_type TEXT NOT NULL,
  payload_json JSONB NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS entity_relations (
  relation_id UUID PRIMARY KEY, from_entity UUID NOT NULL, to_entity UUID NOT NULL,
  relation_type TEXT NOT NULL, source_batch_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id UUID NOT NULL, alias TEXT NOT NULL, alias_type TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(entity_id, alias, alias_type)
);
CREATE INDEX IF NOT EXISTS idx_entity_relations_to ON entity_relations(to_entity, relation_type);
CREATE INDEX IF NOT EXISTS idx_entity_relations_from ON entity_relations(from_entity, relation_type);

-- ---------------------------------------------------------------------------
-- P4.1：全表 RLS（租户 = current_setting('app.tenant_id')，未设置即不可见）
-- ---------------------------------------------------------------------------

ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON import_batches FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE canonical_entities ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON canonical_entities FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE source_documents ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON source_documents FOR ALL
  USING (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = source_documents.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = source_documents.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE import_candidates ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON import_candidates FOR ALL
  USING (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = import_candidates.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = import_candidates.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE approval_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approval_records FOR ALL
  USING (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = approval_records.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = approval_records.batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE canonical_entity_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON canonical_entity_versions FOR ALL
  USING (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = canonical_entity_versions.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = canonical_entity_versions.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE canonical_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON canonical_ledger FOR ALL
  USING (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = canonical_ledger.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = canonical_ledger.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE canonical_outbox ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON canonical_outbox FOR ALL
  USING (EXISTS (SELECT 1 FROM canonical_ledger l
                 JOIN canonical_entities e ON e.entity_id = l.entity_id
                 WHERE l.ledger_id = canonical_outbox.ledger_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM canonical_ledger l
                 JOIN canonical_entities e ON e.entity_id = l.entity_id
                 WHERE l.ledger_id = canonical_outbox.ledger_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE entity_relations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON entity_relations FOR ALL
  USING (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = entity_relations.source_batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM import_batches b
                 WHERE b.batch_id = entity_relations.source_batch_id
                   AND b.tenant_id = current_setting('app.tenant_id', true)));

ALTER TABLE entity_aliases ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON entity_aliases FOR ALL
  USING (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = entity_aliases.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)))
  WITH CHECK (EXISTS (SELECT 1 FROM canonical_entities e
                 WHERE e.entity_id = entity_aliases.entity_id
                   AND e.tenant_id = current_setting('app.tenant_id', true)));

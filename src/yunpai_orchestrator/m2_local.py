"""M2 BOM/SOP 本地实现：模板入库、受控 BOM/SOP 生成、历史检索、run 读回。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m2_local.py``

- sha256(INT): ``d4d882da27edeb7fd51d5c3b88650474d9295ee82b32e465a602a1b78f87113d``
  （34516 字节 / 733 行）
- 归属分片: **M2**（逐工具结论见 ``_migration/rows-S3.md``）
- 状态: **已迁**（6 个工具：``onboard_m2_bom_template`` / ``generate_m2_bom_controlled`` /
  ``list_m2_runs`` / ``get_m2_run`` / ``generate_m2_sop`` / ``search_m2_bom_history``）。
  ``run_bom_sop_workflow`` 留在 ``workers.m2_bom``（V2 既有实现 + P1-9 修复）。

本地等价口径见 INT ``docs/records/artifacts/s3m2-notes.md``：
- M2RunStore：m2_runs/m2_run_artifacts/m2_templates/m2_history_lines（``YUNPAI_M2_DB``，
  缺省 ``runtime/yunpai-m2.sqlite``；tenant 表级隔离；run_id 幂等 ON CONFLICT 更新）；
- ``rule_package_path`` = JSON 规则包文件（存在性校验，不可读显式失败）；
  ``history_paths`` = 历史 BOM 文件（xlsx/csv/json）解析行并入库 m2_history_lines；
- onboard：每文件 → 模板 proposal（列结构 + 物料编码前缀规则建议）→ 模板入库；
- generate_controlled：规则包（column_aliases/required/duplicate_policy/default_uom）
  规范化 product_profile 内嵌行；无内嵌行 → 历史最相似行基底；无任何确定性依据 →
  ``bom_lines=[]`` + note（C 型自由文本→行需模型，不伪造）；
- list/get runs：M2RunStore 查询。
输出契约以 ``registry-manifests/m2.json`` 为准（loose object；list/get 带 {success,data} 信封）。

与 INT 源的差异（逐条可复核，见 ``_migration/REPORT-MIG-M2.md``「契约对齐清单」）：
1. ``_canonical_bom_lines`` **不再直连 sqlite 的 canonical 表**，改走 V2 M0 读口
   ``m0_backend.M0Store.list_entities("bom", tenant_id)``（rows-S3.md 第 1 行改造项）；
   ``YUNPAI_M0_DB`` 未配置 / DB 不存在 / 读口异常时返回空候选，并把原因**显式登记
   warning**（不再 ``except Exception: return []`` 静默吞掉）。
2. canonical 业务体加一次小归一化：``payload.attributes`` 并入顶层（顶层优先）。
   V2 没有 INT 的 ``fact_gateway.py``——BOM 的 ``lines`` 在 ``payload.lines``
   （``business_catalog.py:1088``），SOP document 的 ``route_steps`` 在
   ``payload.attributes.route_steps``（``business_catalog.py:1138``）。
3. ``onboard_bom_template`` 把历史文件名 stem 写入 ``product_name``（可回溯的输入事实，
   **不猜** ``product_code``）；rows-S3 备注：写空会让 ``search_m2_bom_history`` 按产品名
   检索退化。
4. ``m2_list_runs`` 对 ``limit`` 做契约钳制 [1, 200]（契约 minimum/maximum）。
5. ``generate_m2_sop`` 制品目录可用 ``YUNPAI_M2_ARTIFACT_DIR`` 覆盖（缺省
   ``runtime/m2-artifacts``，部署期可指向挂载卷）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from .m0_sandbox import utc_now


def _default_db_path() -> str:
    return str(os.getenv("YUNPAI_M2_DB") or "runtime/yunpai-m2.sqlite")


SCHEMA = """
CREATE TABLE IF NOT EXISTS m2_runs (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  order_id TEXT NOT NULL DEFAULT '',
  product_code TEXT NOT NULL DEFAULT '',
  product_name TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m2_runs_tenant ON m2_runs(tenant_id, product_code);
CREATE TABLE IF NOT EXISTS m2_run_artifacts (
  run_id TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(run_id, kind)
);
CREATE TABLE IF NOT EXISTS m2_templates (
  template_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  source_file TEXT NOT NULL DEFAULT '',
  columns_json TEXT NOT NULL DEFAULT '[]',
  rules_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m2_history_lines (
  line_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  product_code TEXT NOT NULL DEFAULT '',
  product_name TEXT NOT NULL DEFAULT '',
  material_code TEXT NOT NULL,
  material_name TEXT NOT NULL DEFAULT '',
  quantity TEXT NOT NULL DEFAULT '',
  uom TEXT NOT NULL DEFAULT '',
  source_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m2_history ON m2_history_lines(tenant_id, material_code);
"""

# 通用物料列别名（与 workers._extract_uploaded_bom 对齐）
_ALIASES = {
    "material_code": ("物料编码", "料号", "物料编号", "材料编码", "编码", "material_code", "materialcode"),
    "material_name": ("材料名称", "原材料名称", "物料名称", "品名", "名称", "material_name"),
    "specification": ("规格", "规格型号", "型号", "spec"),
    "quantity": ("用量", "数量", "用量/装箱数量", "单机用量", "quantity"),
    "unit": ("单位", "uom"),
}

#: 契约（m2.json#tools[5] limit minimum/maximum）钳制口径。
_LIST_LIMIT_MIN = 1
_LIST_LIMIT_MAX = 200

#: canonical 记录信封里不属于业务体的键（``_canonical_body`` 的兜底拆分口径）。
_CANONICAL_ENVELOPE_KEYS = frozenset({
    "identity", "entity_type", "evidence", "idempotency_key", "review_status",
    "reviewed_by", "schema_version", "source", "tenant_id", "canonical_key",
    "filename", "attributes",
})


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _number(v: Any) -> str:
    if v is None or _text(v) == "":
        return ""
    text = str(v).replace(",", "").strip()
    try:
        return str(float(text))
    except ValueError:
        return _text(v)


def _artifact_path_map(artifacts: list[dict[str, Any]]) -> dict[str, str]:
    """制品 kind → path 映射（前端 /api/m2/artifact?path= 直接可用，见 M2RunArtifacts.tsx）。"""
    return {
        str(item["kind"]): str(item["path"])
        for item in artifacts
        if isinstance(item, dict) and item.get("kind") and item.get("path")
    }


class M2RunStore:
    """M2 run/模板/历史行领域库（sqlite，单进程 RLock）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = str(db_path) if db_path is not None else _default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ---------- runs ----------
    def save_run(self, *, tenant_id: str, run_id: str, tool: str, status: str = "",
                 order_id: str = "", product_code: str = "", product_name: str = "",
                 summary: dict[str, Any] | None = None) -> dict[str, Any]:
        now = utc_now()
        text = json.dumps(summary or {}, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m2_runs (run_id, tenant_id, tool, status, order_id, product_code, "
                " product_name, summary_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                "order_id=excluded.order_id, product_code=excluded.product_code, "
                "product_name=excluded.product_name, summary_json=excluded.summary_json, "
                "updated_at=excluded.updated_at",
                (run_id, tenant_id, tool, status, order_id, product_code, product_name,
                 text, now, now))
        return self.get_run(tenant_id, run_id)  # type: ignore[return-value]

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m2_runs WHERE run_id=? AND tenant_id=?",
                             (run_id, tenant_id)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["summary"] = json.loads(out.pop("summary_json"))
        except ValueError:
            out["summary"] = {}
        artifacts = self.artifacts(tenant_id, run_id)
        out["artifacts"] = artifacts
        # 契约（m2.json#tools[6]）与前端 M2RunArtifacts.tsx:52 都消费 kind → path 映射。
        out["artifact_paths"] = _artifact_path_map(artifacts)
        return out

    def list_runs(self, tenant_id: str, *, product_code: str = "", product_name: str = "",
                  order_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM m2_runs WHERE tenant_id=? "
        params: list[Any] = [tenant_id]
        if product_code:
            sql += "AND product_code=? "
            params.append(product_code)
        if order_id:
            sql += "AND order_id=? "
            params.append(order_id)
        if product_name:
            sql += "AND product_name LIKE ? "
            params.append(f"%{product_name}%")
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json"))
            except ValueError:
                item["summary"] = {}
            out.append(item)
        # 契约（m2.json#tools[5]）要求返回 artifact_paths：kind → path（一次批量查询）。
        paths_by_run = self._artifact_paths_by_run([str(item["run_id"]) for item in out])
        for item in out:
            item["artifact_paths"] = paths_by_run.get(str(item["run_id"]), {})
        return out

    def _artifact_paths_by_run(self, run_ids: list[str]) -> dict[str, dict[str, str]]:
        """批量取 run_id → {kind: path}；仅返回真实登记的制品，不合成路径。"""
        if not run_ids:
            return {}
        marks = ",".join("?" for _ in run_ids)
        with self._lock, self._connect() as db:
            rows = db.execute(
                f"SELECT run_id, kind, path FROM m2_run_artifacts WHERE run_id IN ({marks})",
                tuple(run_ids)).fetchall()
        out: dict[str, dict[str, str]] = {}
        for row in rows:
            out.setdefault(str(row["run_id"]), {})[str(row["kind"])] = str(row["path"])
        return out

    def add_artifact(self, tenant_id: str, run_id: str, kind: str, path: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO m2_run_artifacts VALUES (?,?,?,?)",
                       (run_id, kind, path, utc_now()))

    def artifacts(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM m2_run_artifacts WHERE run_id=?",
                              (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- history lines ----------
    def add_history_lines(self, tenant_id: str, run_id: str, *, product_code: str,
                          product_name: str, lines: list[dict[str, Any]],
                          source_path: str = "") -> int:
        with self._lock, self._connect() as db:
            for line in lines:
                db.execute(
                    "INSERT INTO m2_history_lines (line_id, tenant_id, run_id, product_code, "
                    " product_name, material_code, material_name, quantity, uom, "
                    " source_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, tenant_id, run_id, product_code, product_name,
                     str(line.get("material_code") or ""),
                     str(line.get("material_name") or ""),
                     _number(line.get("quantity")),
                     str(line.get("uom") or line.get("unit") or ""),
                     source_path, utc_now()))
        return len(lines)

    def history_lines(self, tenant_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m2_history_lines WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # ---------- templates ----------
    def save_template(self, *, tenant_id: str, source_file: str, columns: list[str],
                      rules: dict[str, Any]) -> str:
        template_id = uuid4().hex[:12]
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m2_templates (template_id, tenant_id, source_file, columns_json, "
                " rules_json, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (template_id, tenant_id, source_file, json.dumps(columns, ensure_ascii=False),
                 json.dumps(rules, ensure_ascii=False), "active", utc_now()))
        return template_id

    def list_templates(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM m2_templates WHERE tenant_id=? AND status='active' "
                              "ORDER BY created_at DESC", (tenant_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["columns"] = json.loads(item.pop("columns_json"))
            except ValueError:
                item["columns"] = []
            try:
                item["rules"] = json.loads(item.pop("rules_json"))
            except ValueError:
                item["rules"] = {}
            out.append(item)
        return out


# ---------- 文件解析 ----------
def _read_rule_package(path: str) -> dict[str, Any]:
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"rule_package_path 不可读: {path}")
    raw = p.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"rule_package_path 必须为 JSON 规则包: {path}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"rule_package JSON 根必须为对象: {path}")
    return parsed


def _parse_history_file(path: str) -> tuple[list[str], list[dict[str, Any]]]:
    """历史 BOM 文件（xlsx/csv/json）→ (headers, rows[{material_code,..}])。"""
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"history_path 不可读: {path}")
    raw = p.read_bytes()
    lower = p.name.lower()
    headers: list[str] = []
    records: list[dict[str, Any]] = []
    if lower.endswith(".json"):
        parsed = json.loads(raw.decode("utf-8"))
        items = parsed.get("lines") or parsed.get("records") or ([parsed] if isinstance(parsed, dict) else [])
        for item in items:
            if isinstance(item, dict) and (item.get("material_code") or item.get("物料编码")):
                line = {}
                for key, aliases in _ALIASES.items():
                    for field in (key, *aliases):
                        if field in item and item.get(field) not in (None, ""):
                            line[key] = item[field]
                            break
                records.append(line)
        headers = list(dict.fromkeys(k for line in records for k in line))
        return headers, records
    if lower.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        matrix = [list(r) for r in reader]
    else:
        try:
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError as exc:
            raise ValueError("解析 xlsx 需要 openpyxl") from exc
        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except (InvalidFileException, OSError, ValueError) as exc:
            raise ValueError(f"history xlsx 无法解析: {path}") from exc
        ws = wb.worksheets[0] if wb.worksheets else None
        matrix = [list(r) for r in ws.iter_rows(values_only=True)] if ws is not None else []
        wb.close()
    # 首非空行=表头（casefold），映射列
    header_idx = None
    for i, row in enumerate(matrix):
        if any(_text(c) for c in row):
            header_idx = i
            break
    if header_idx is None:
        return [], []
    raw_headers = [_text(c).casefold() for c in matrix[header_idx]]
    headers = [h for h in raw_headers if h]
    col_index: dict[str, int] = {}
    for key, aliases in _ALIASES.items():
        for idx, h in enumerate(raw_headers):
            if h in {a.casefold() for a in aliases} or h == key:
                col_index[key] = idx
                break
    for row in matrix[header_idx + 1:]:
        code = _text(row[col_index["material_code"]]) if "material_code" in col_index and col_index["material_code"] < len(row) else ""
        if not code:
            continue
        line: dict[str, Any] = {"material_code": code}
        if "material_name" in col_index and col_index["material_name"] < len(row):
            line["material_name"] = _text(row[col_index["material_name"]])
        if "specification" in col_index and col_index["specification"] < len(row):
            line["specification"] = _text(row[col_index["specification"]])
        if "quantity" in col_index and col_index["quantity"] < len(row):
            line["quantity"] = _number(row[col_index["quantity"]])
        if "unit" in col_index and col_index["unit"] < len(row):
            line["unit"] = _text(row[col_index["unit"]])
        records.append(line)
    return headers, records


def _guess_prefix(code: str) -> str:
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*?)([-_.])?", code)
    if m:
        return m.group(1) + (m.group(2) or "")
    head = re.match(r"^[A-Za-z0-9._-]+", code)
    return head.group(0)[:2] if head else ""


# ---------- handlers ----------
def _ctx_store(ctx: dict[str, Any]) -> M2RunStore:
    """V2 的 ctx 不含 ``m2_db``（INFRA-DECISIONS §1.2：DB 路径统一走 env）。

    ``ctx.get("m2_db")`` 仅为直接调用（测试/脚本）保留的同名入口。
    """
    return M2RunStore(ctx.get("m2_db") or None)


async def onboard_bom_template(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """onboard_m2_bom_template：解析历史 BOM → 模板 proposal + 入库。"""
    rule_path = str(payload.get("rule_package_path") or "")
    _read_rule_package(rule_path)  # 存在性校验
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    history_paths = payload.get("history_paths") or []
    proposals: list[dict[str, Any]] = []
    run_id = f"m2-{str(ctx.get('task_id') or 'task')[-10:]}"
    if history_paths:
        for path in history_paths:
            headers, lines = _parse_history_file(str(path))
            if not headers:
                proposals.append({"source_file": str(path), "error": "NO_HISTORY_ROWS",
                                  "columns": [], "numbering_rule": ""})
                continue
            prefixes: dict[str, int] = {}
            for line in lines:
                prefix = _guess_prefix(str(line.get("material_code") or ""))
                if prefix:
                    prefixes[prefix] = prefixes.get(prefix, 0) + 1
            top_prefix = max(prefixes, key=prefixes.get) if prefixes else ""
            product_code = ""
            # 文件名去扩展名作为产品名启发（可回溯的输入事实，不是猜测编码；
            # rows-S3 备注：历史行 product_name 为空会让 search_m2_bom_history
            # 按产品名检索退化）。
            base = Path(str(path)).stem
            product_name = base
            proposal = {
                "source_file": str(path), "columns": headers, "row_count": len(lines),
                "numbering_rule": {"detected_prefix": top_prefix,
                                   "prefix_frequency": prefixes.get(top_prefix, 0) if top_prefix else 0,
                                   "sample_codes": [str(l.get("material_code")) for l in lines[:3]]},
                "template_name": f"{base}-template",
            }
            proposals.append(proposal)
            if lines:
                store.add_history_lines(tenant, run_id, product_code=product_code,
                                        product_name=product_name, lines=lines,
                                        source_path=str(path))
            if headers and top_prefix:
                store.save_template(tenant_id=tenant, source_file=str(path), columns=headers,
                                    rules={"detected_prefix": top_prefix})
    store.save_run(tenant_id=tenant, run_id=run_id, tool="onboard_m2_bom_template",
                   status="done", product_name="", summary={
                       "proposal_count": len(proposals)})
    return {"proposals": proposals}


async def generate_bom_controlled(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """generate_m2_bom_controlled：规则包规范化行/历史基底 → standard_bom。"""
    profile = payload.get("product_profile") or {}
    if not isinstance(profile, dict):
        raise ValueError("product_profile 必须为对象")
    rules = _read_rule_package(str(payload.get("rule_package_path") or ""))
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    product_code = str(profile.get("product_code") or "")
    product_name = str(profile.get("product_name") or "")
    alias_map = rules.get("column_aliases") if isinstance(rules.get("column_aliases"), dict) else {}
    default_uom = str(rules.get("default_uom") or "")
    duplicate_policy = str(rules.get("duplicate_policy") or "reject")
    embedded = profile.get("lines") or profile.get("bom_items") or payload.get("bom_items")
    lines: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    run_id = f"m2-{str(ctx.get('task_id') or 'task')[-10:]}"
    if isinstance(embedded, list):
        for item in embedded:
            if not isinstance(item, dict):
                continue
            line: dict[str, Any] = {}
            for key in ("material_code", "material_name", "specification", "quantity", "uom"):
                for field in (key, *alias_map.get(key, [])) if key in alias_map else (key,):
                    if item.get(field) not in (None, ""):
                        line[key] = item[field]
                        break
            if not line.get("material_code"):
                continue
            line["quantity"] = _number(line.get("quantity"))
            if not line.get("uom") and default_uom:
                line["uom"] = default_uom
            lines.append(line)
        evidence.append({"key": "embedded_profile", "locator": {},
                         "excerpt": f"来自 product_profile.lines {len(lines)} 行"})
    if not lines and payload.get("history_paths"):
        # 历史基底：解析路径并按产品名前缀/关键词取最相似组
        best: list[dict[str, Any]] = []
        best_score = -1
        for path in payload.get("history_paths") or []:
            _headers, rows = _parse_history_file(str(path))
            score = sum(1 for r in rows if product_name and str(product_name).casefold() in
                        str(r.get("material_name") or "").casefold())
            if score > best_score:
                best_score = score
                best = rows
        lines = best
        evidence.append({"key": "history_similar", "locator": {},
                         "excerpt": f"历史基底行 {len(lines)}（score={best_score}）"})
    # 去重策略
    if duplicate_policy == "reject":
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for line in lines:
            code = str(line.get("material_code") or "")
            if code in seen:
                continue
            seen.add(code)
            deduped.append(line)
        lines = deduped
    bom_lines = []
    for line in lines:
        bom_lines.append({
            "material_code": str(line.get("material_code") or ""),
            "material_name": str(line.get("material_name") or ""),
            "specification": str(line.get("specification") or "") or None,
            "quantity": _number(line.get("quantity")),
            "uom": str(line.get("uom") or default_uom or ""),
        })
    source_note = ""
    if not bom_lines:
        source_note = ("无确定性历史/规则依据可生成 BOM 行：需求自由文本→行属模型 C 型，"
                       "缺模型时显式不生成（不伪造）")
    standard_bom = {
        "product_profile": {"product_code": product_code, "product_name": product_name},
        "bom_lines": bom_lines,
        "rules_applied": {"column_aliases": alias_map, "duplicate_policy": duplicate_policy,
                          "default_uom": default_uom},
        "evidence": evidence,
        "note": source_note,
    }
    if bom_lines:
        store.add_history_lines(tenant, run_id, product_code=product_code,
                                product_name=product_name, lines=bom_lines)
    store.save_run(tenant_id=tenant, run_id=run_id, tool="generate_m2_bom_controlled",
                   status="done", product_code=product_code, product_name=product_name,
                   summary={"bom_lines": len(bom_lines), "note": source_note})
    return {"standard_bom": standard_bom}


def _clamp_limit(value: Any) -> int:
    """契约（m2.json#tools[5].input_schema.limit）minimum=1 / maximum=200 钳制。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 50
    return max(_LIST_LIMIT_MIN, min(parsed, _LIST_LIMIT_MAX))


async def m2_list_runs(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    items = store.list_runs(
        tenant, product_code=str(payload.get("product_code") or ""),
        product_name=str(payload.get("product_name") or ""),
        order_id=str(payload.get("order_id") or ""),
        limit=_clamp_limit(payload.get("limit") if payload.get("limit") is not None else 50))
    return {"success": True, "data": {"items": items, "total": len(items)}}


async def m2_get_run(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        raise ValueError("get_m2_run 需要 run_id")
    run = store.get_run(tenant, run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    return {"success": True, "data": {"run": run}}


# ---------- M2-2：SOP Word + 历史检索 ----------
def _docx_cell_text(cell) -> str:
    return _text(cell.text if hasattr(cell, "text") else "")


def _artifact_dir() -> Path:
    """制品目录：``YUNPAI_M2_ARTIFACT_DIR`` 可覆盖（缺省 runtime/m2-artifacts）。"""
    return Path(os.getenv("YUNPAI_M2_ARTIFACT_DIR") or "runtime/m2-artifacts")


async def generate_m2_sop(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """generate_m2_sop：生成 80806-129 结构 SOP Word（python-docx）。"""
    from docx import Document
    from docx.shared import Pt

    product_name = str(payload.get("product_name") or "")
    part_no = str(payload.get("part_no") or "")
    document_no = str(payload.get("document_no") or "")
    if not (product_name and part_no and document_no):
        raise ValueError("generate_m2_sop 需要 product_name/part_no/document_no")
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    run_id = f"m2-{str(ctx.get('task_id') or 'task')[-10:]}"

    doc = Document()
    title = doc.add_heading(f"SOP {document_no}", level=0)
    for run in title.runs:
        run.font.size = Pt(18)
    doc.add_paragraph(f"产品名称：{product_name}")
    doc.add_paragraph(f"料号：{part_no}")
    doc.add_paragraph(f"文档编号：{document_no}")
    bom_items = payload.get("bom_items") or []
    if isinstance(bom_items, list) and bom_items:
        doc.add_heading("用料清单", level=1)
        table = doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        for i, h in enumerate(("序号", "物料编码", "物料名称", "用量", "单位")):
            hdr[i].text = h
        for idx, item in enumerate(bom_items, start=1):
            if not isinstance(item, dict):
                continue
            cells = table.add_row().cells
            values = (str(idx), item.get("material_code"), item.get("material_name"),
                      item.get("quantity"), item.get("uom"))
            for i, v in enumerate(values):
                cells[i].text = str(v) if v is not None else ""
    routing_steps = payload.get("routing_steps") or []
    if isinstance(routing_steps, list) and routing_steps:
        doc.add_heading("作业步骤", level=1)
        step_table = doc.add_table(rows=1, cols=4)
        step_table.style = "Table Grid"
        hdr = step_table.rows[0].cells
        for i, h in enumerate(("步骤", "操作内容", "设备/工装", "要点")):
            hdr[i].text = h
        for idx, step in enumerate(routing_steps, start=1):
            if not isinstance(step, dict):
                continue
            cells = step_table.add_row().cells
            values = (str(step.get("sequence_no") or idx), step.get("operation_name")
                      or step.get("name") or step.get("content"),
                      step.get("equipment_code") or step.get("equipment") or "",
                      step.get("key_point") or "")
            for i, v in enumerate(values):
                cells[i].text = str(v) if v is not None else ""
    flow_note = "流程图：文字流程（本地无图形渲染依赖，不生成 PNG 流程图；图形流程图由 HTTP 服务提供）"
    doc.add_paragraph(flow_note)
    out_dir = _artifact_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = (out_dir / f"{document_no}.docx").resolve()
    doc.save(str(target))

    summary = {"document_no": document_no, "product_name": product_name,
               "part_no": part_no, "bom_item_count": len(bom_items),
               "routing_step_count": len(routing_steps)}
    store.add_artifact(tenant, run_id, "sop_docx", str(target))
    store.save_run(tenant_id=tenant, run_id=run_id, tool="generate_m2_sop",
                   status="done", product_name=product_name,
                   order_id=str(payload.get("order_id") or ""), summary=summary)
    return {"status": "generated",
            "artifacts": {"docx": str(target), "document_no": document_no,
                          "product_name": product_name, "part_no": part_no},
            # 契约（m2.json#tools[4].output_schema.flowchart）：本地只产文字流程，
            # 不冒充已生成图形流程图。
            "flowchart": {"status": "text_only", "note": flow_note}}


def _canonical_body(record: dict[str, Any]) -> dict[str, Any]:
    """canonical 记录 → 业务体（V2 归一化：``payload.attributes`` 并入顶层，顶层优先）。

    V2 没有 INT 的 ``fact_gateway.py``：BOM 的 ``lines`` 在 ``payload.lines``
    （``business_catalog.py:1088``），SOP document 的 ``route_steps`` 在
    ``payload.attributes.route_steps``（``business_catalog.py:1138``）。
    """
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict):
        payload = {key: value for key, value in record.items()
                   if key not in _CANONICAL_ENVELOPE_KEYS}
    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        return {**attributes, **payload}  # 顶层优先
    return dict(payload)


def _canonical_bom_lines(tenant_id: str, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    """canonical bom 实体行作为历史检索候选。

    V2 改造点（rows-S3.md 第 1 行）：**不直连 sqlite 的 canonical 表**，改走 V2 的 M0 读口
    ``m0_backend.M0Store.list_entities("bom", tenant_id)``（与 M0 HTTP/PostgreSQL 后端同一
    契约，返回 ``{entities:[{entity_id,canonical_key,payload_json,..}]}``）。读口不可用时把原因
    写进 ``warnings``（不静默退化成空候选）。
    """
    db_path = os.getenv("YUNPAI_M0_DB") or "runtime/yunpai-m0.sqlite"
    if not Path(db_path).exists():
        # 未配置/不存在 canonical 库 = 没有事实源；不隐式建库、不隐式大表读取。
        return []
    try:
        from .m0_backend import M0Store

        result = M0Store(db_path).list_entities("bom", tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 - 读口异常不阻断检索，但必须留痕
        if warnings is not None:
            warnings.append(
                f"canonical BOM 读口不可用（{type(exc).__name__}: {exc}）；本次仅用本地历史行候选")
        return []
    out: list[dict[str, Any]] = []
    for entity in result.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        record = entity.get("payload_json")
        if isinstance(record, str):
            try:
                record = json.loads(record)
            except ValueError:
                continue
        if not isinstance(record, dict):
            continue
        body = _canonical_body(record)
        lines = body.get("lines")
        if not isinstance(lines, list):
            continue
        out.append({"source": "canonical_bom",
                    "product_code": str(body.get("product_code") or entity.get("canonical_key") or ""),
                    "run_id": "", "lines": [dict(line) for line in lines if isinstance(line, dict)]})
    return out


async def search_m2_bom_history(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """search_m2_bom_history：历史 BOM 多维度相似检索。

    候选来源（全部为真实数据，不合成行）：
    ① m2_history_lines 自持行 ② M0 canonical bom 实体行（经 V2 M0 读口，见
    ``_canonical_bom_lines``）③ 显式 history_paths（xlsx/csv/json，契约声明字段；
    不可读则显式失败）。
    """
    tenant = str(ctx.get("tenant_id") or "default")
    store = _ctx_store(ctx)
    product_name = str(payload.get("product_name") or "")
    keywords = [str(k).lower() for k in (payload.get("keywords") or []) if str(k).strip()]
    limit = int(payload.get("limit") or 5)
    name_tokens = {t for t in product_name.lower().replace("-", " ").replace("_", " ").split() if len(t) > 1}
    candidates: list[dict[str, Any]] = []
    for row in store.history_lines(tenant):
        candidates.append({"source": "m2_history", "run_id": row.get("run_id") or "",
                           "product_code": row.get("product_code") or "",
                           "product_name": row.get("product_name") or "",
                           "lines": [row]})
    warnings: list[str] = []
    candidates.extend(_canonical_bom_lines(tenant, warnings))
    for path in payload.get("history_paths") or []:
        # 契约声明的额外历史库来源；不可读/不可解析 → ValueError（不静默跳过）。
        _headers, rows = _parse_history_file(str(path))
        if not rows:
            continue
        candidates.append({"source": "history_file", "run_id": "", "product_code": "",
                           "product_name": Path(str(path)).stem, "lines": rows})
    results: list[dict[str, Any]] = []
    for cand in candidates:
        score = 0.0
        matched: list[str] = []
        cand_lines = cand.get("lines") or []
        cand_text = " ".join(str(x.get("material_code")) + " " + str(x.get("material_name") or "")
                             for x in cand_lines).lower()
        cand_name = str(cand.get("product_name") or "").lower()
        name_meta = cand.get("product_code") or ""
        if product_name and (product_name.lower() in cand_name or product_name.lower() in str(name_meta).lower()):
            score += 0.6
            matched.append("product_name")
        if product_name:
            cand_tokens = {t for t in cand_name.replace("-", " ").replace("_", " ").split() if len(t) > 1}
            overlap = name_tokens & cand_tokens
            if overlap:
                score += 0.2 * len(overlap)
                matched.append("name_token")
        for kw in keywords:
            if kw in cand_text or kw in cand_name:
                score += 0.15
                matched.append(f"keyword:{kw}")
        if score <= 0:
            continue
        results.append({
            "source": cand["source"], "product_code": cand.get("product_code") or "",
            "product_name": cand.get("product_name") or "",
            "score": round(min(score, 1.0), 3), "matched_keywords": matched,
            "lines_count": len(cand_lines),
            "lines": cand_lines[:50],
            "run_id": cand.get("run_id") or "",
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"results": results[:max(limit, 0)], "warnings": warnings}


LOCAL_HANDLERS: dict[str, Any] = {
    "onboard_m2_bom_template": onboard_bom_template,
    "generate_m2_bom_controlled": generate_bom_controlled,
    "list_m2_runs": m2_list_runs,
    "get_m2_run": m2_get_run,
    "generate_m2_sop": generate_m2_sop,
    "search_m2_bom_history": search_m2_bom_history,
}

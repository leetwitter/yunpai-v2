"""Agent 驱动文件识别的确定性安全网：采样 + PII 脱敏 + 自描述结构化表。

本模块只做"事实与安全"，不做"语义理解"：
- ``sample_file`` 把大文件压成 LLM 可看的小样本（真实格式 + 表头 + 前 N 行）。
- 脱敏只针对身份类 PII（身份证/银行卡/手机号等），确定性正则，不交给 LLM；
  数值型业务字段（工资/薪资/计件单价等）**不遮罩**，保留供 agent 跨表计算。
- ``RecognizedTableStore`` 落库自描述行（kind + 列 + 行），sha256 幂等。

语义分类（"这是工资表还是日报"）由总控 agent（Qwen）负责，本模块只校验结构。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# PII 脱敏（确定性）
# ---------------------------------------------------------------------------

_ID_CARD = re.compile(r"\b\d{17}[\dXx]\b")
_BANK_CARD = re.compile(r"\b\d{16,19}\b")
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")

#: 列名命中这些 token 即视为「身份类敏感列」（值会被遮罩）。
#: 工资/薪资/计件单价等数值型业务字段**不在**此列——它们要留给 agent 跨表计算
#: （如 工资÷计件单价=日工作量），故不遮罩。隐私保护可经 ingest_recognized 的
#: ``redact=False`` 整体关闭（简单处理，出现意外仍有 sha256 幂等/schema 校验兜底）。
SENSITIVE_COLUMN_TOKENS = (
    "身份证", "银行卡", "银行账号", "手机", "电话",
    "住址", "地址", "紧急联系人", "联系方式",
)

KIND_ENUM = (
    "order", "bom", "sop", "inventory", "equipment", "station", "worker",
    "wage", "personnel_roster", "production_daily_report", "rule_config",
    "engineering_document", "engineering_drawing", "other",
)


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _redact_value(value: Any) -> Any:
    """确定性 PII 值遮罩：任何列命中身份证/银行卡/手机号即遮罩。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if _ID_CARD.fullmatch(text) or _BANK_CARD.fullmatch(text) or _PHONE.fullmatch(text):
        return _mask(text)
    return value


def _mask_column_value(value: Any) -> Any:
    """敏感列整列遮罩（字符串与数值都遮，不保留可识别信息）。"""
    if value is None:
        return None
    text = str(value).strip()
    return _mask(text) if text else value


def redact_row(row: dict[str, Any], *, sensitive_columns: set[str] | None = None) -> dict[str, Any]:
    """对一行的敏感列做确定性遮罩；敏感判定 = 显式指定列 ∪ 列名 token 命中 ∪ 值形如 PII。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        key_s = str(key)
        sensitive = (
            (key_s in (sensitive_columns or set()))
            or any(token in key_s for token in SENSITIVE_COLUMN_TOKENS)
        )
        out[key] = _mask_column_value(value) if sensitive else _redact_value(value)
    return out


# ---------------------------------------------------------------------------
# 采样（确定性，只读表头 + 前 N 行）
# ---------------------------------------------------------------------------

def _is_numeric_cell(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        return bool(re.match(r"^-?\d+(\.\d+)?%?$", text)) and text not in ("", "-")
    return False


def _header_row_index(rows: list[list[Any]]) -> int:
    """结构启发：表头行 = 前 10 行里第一个「多列且无纯数值单元格」的行。

    单列标题行被跳过；含数字/编码数值的数据行被跳过；只有「全是文字列名」的行
    才判为表头。这是结构判断（找表头位置），不做业务语义。
    """
    for index, row in enumerate(rows[:10]):
        cells = [value for value in row if value not in (None, "")]
        if len(cells) < 2:
            continue
        if not any(_is_numeric_cell(value) for value in cells):
            return index
    # 兜底：非空单元格最多的行。
    best, best_count = 0, -1
    for index, row in enumerate(rows[:10]):
        count = sum(1 for value in row if value not in (None, ""))
        if count > best_count:
            best, best_count = index, count
    return best


def _sample_rows_from_raw(rows: list[list[Any]], max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
    """从原始行列表里找表头并抽取前 max_rows 行。"""
    if not rows:
        return [], []
    header_index = _header_row_index(rows)
    headers = [str(c) for c in rows[header_index]]
    sample_rows: list[dict[str, Any]] = []
    for row in rows[header_index + 1:header_index + 1 + max_rows]:
        if any(v not in (None, "") for v in row):
            sample_rows.append({headers[i] if i < len(headers) else f"col{i}": v for i, v in enumerate(row)})
    return headers, sample_rows


def sample_file(raw: bytes, filename: str, *, max_rows: int = 10, max_sheets: int = 6) -> dict[str, Any]:
    """返回 LLM 可看的小样本；非表格/无法解析时只回嗅探结果 + 说明，不伪造内容。"""
    from .file_sniff import sniff_format

    verdict = sniff_format(raw, filename)
    headers: list[str] = []
    sample_rows: list[dict[str, Any]] = []
    sheet_names: list[str] = []
    sheets: list[dict[str, Any]] = []
    images: list[str] = []
    row_count: int | None = None
    fmt = verdict.detected_format

    try:
        if fmt in {"xlsx", "xlsm"}:
            from openpyxl import load_workbook
            import io

            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheet_names = [ws.title for ws in wb.worksheets]
            for ws in wb.worksheets[:max_sheets]:
                rows: list[list[Any]] = []
                for row in ws.iter_rows(values_only=True):
                    if len(rows) >= 40:
                        break
                    # 截断到前 25 列：真实表头/数据列在左侧，右侧常有脏格式造成的
                    # 超大 max_column（如 16369），全列读会导致样本爆炸。
                    rows.append([("" if c is None else (c.isoformat() if hasattr(c, "isoformat") else c)) for c in row[:25]])
                sheet_headers, sheet_rows = _sample_rows_from_raw(rows, max_rows)
                sheets.append({"name": ws.title, "headers": sheet_headers, "sample_rows": sheet_rows, "raw_rows": rows})
                if not headers and (sheet_headers or sheet_rows):
                    headers, sample_rows = sheet_headers, sheet_rows
            row_count = (wb.worksheets[0].max_row or 0) if wb.worksheets else None
            wb.close()
        elif fmt == "xls":
            import io

            import xlrd

            wb = xlrd.open_workbook(file_contents=raw)
            sheet_names = wb.sheet_names()
            for sh in wb.sheets()[:max_sheets]:
                rows = [[sh.cell_value(r, c) for c in range(min(sh.ncols, 25))] for r in range(min(sh.nrows, 40))]
                sheet_headers, sheet_rows = _sample_rows_from_raw(rows, max_rows)
                sheets.append({"name": sh.name, "headers": sheet_headers, "sample_rows": sheet_rows, "raw_rows": rows})
                if not headers and (sheet_headers or sheet_rows):
                    headers, sample_rows = sheet_headers, sheet_rows
            row_count = wb.sheets()[0].nrows if wb.sheets() else None
        elif fmt in {"csv", "tsv"}:
            import csv
            import io

            delim = "," if fmt == "csv" else "\t"
            reader = csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace")), delimiter=delim)
            rows = list(reader)
            if rows:
                header_index = _header_row_index(rows)
                headers = [str(h) for h in rows[header_index]]
                for row in rows[header_index + 1:header_index + 1 + max_rows]:
                    sample_rows.append({headers[i] if i < len(headers) else f"col{i}": v for i, v in enumerate(row)})
                row_count = len(rows)
        elif fmt == "json":
            data = json.loads(raw.decode("utf-8-sig"))
            records = data if isinstance(data, list) else data.get("records") if isinstance(data, dict) else []
            if isinstance(records, list) and records and isinstance(records[0], dict):
                headers = sorted({k for rec in records[:max_rows] for k in rec.keys()})
                sample_rows = records[:max_rows]
                row_count = len(records)
    except Exception:
        # 采样失败不伪造；回退到只给嗅探 + 说明。
        headers, sample_rows, row_count = [], [], None

    # 图片/PDF：确定性降采样成 base64，交给多模态 agent 读（不走 OCR）。
    try:
        if fmt in {"png", "jpg", "jpeg"}:
            from .media_sample import downscale_image

            images = [downscale_image(raw)]
        elif fmt == "pdf":
            from .media_sample import render_pdf_pages

            images = render_pdf_pages(raw)
    except Exception:
        # 渲染失败不伪造内容；agent 退化为只看嗅探 + 说明。
        images = []

    return {
        "filename": filename,
        "sniff": {"detected_format": verdict.detected_format, "mime_type": verdict.mime_type, "match": verdict.match},
        "headers": headers,
        "sample_rows": sample_rows,
        "sheet_names": sheet_names,
        "sheets": sheets,
        "images": images,
        "row_count": row_count,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_sampled": bool(sample_rows) or bool(headers) or bool(images),
    }


# ---------------------------------------------------------------------------
# 自描述结构化表（SQLite，sha256 幂等）
# ---------------------------------------------------------------------------

def _default_db_path() -> str:
    return os.getenv("YUNPAI_RECOGNIZED_DB", "runtime/yunpai-recognized.sqlite")


class RecognizedTableStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path or _default_db_path())
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            # 租户迁移（2026-09-07）：老表只有 sha256 全局 UNIQUE，跨租户混存；
            # 检出无 tenant_id 的老表时建新表回填 'default' 再删旧表，幂等键改为
            # (tenant_id, sha256)。
            existing = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='recognized_tables'"
            ).fetchone()
            legacy = existing is not None and "tenant_id" not in str(existing["sql"])
            if legacy:
                db.execute("ALTER TABLE recognized_tables RENAME TO recognized_tables_pre_tenant")
            db.execute(
                """CREATE TABLE IF NOT EXISTS recognized_tables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    columns TEXT NOT NULL,
                    rows TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    redacted_fields INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(tenant_id, sha256)
                )"""
            )
            if legacy:
                db.execute(
                    """INSERT INTO recognized_tables(tenant_id, kind, filename, sha256, columns, rows,
                                                    confidence, redacted_fields, created_at)
                       SELECT 'default', kind, filename, sha256, columns, rows,
                              confidence, redacted_fields, created_at
                       FROM recognized_tables_pre_tenant"""
                )
                db.execute("DROP TABLE recognized_tables_pre_tenant")
            db.execute("CREATE INDEX IF NOT EXISTS idx_recognized_tenant_kind ON recognized_tables(tenant_id, kind)")

    def ingest(self, *, kind: str, filename: str, sha256: str, columns: list[str],
               rows: list[dict[str, Any]], confidence: float, redact: bool = True,
               tenant_id: str = "default") -> dict[str, Any]:
        """落库自描述行；同租户同 sha256 幂等（重复上传不重复建行）。"""
        if kind not in KIND_ENUM:
            raise ValueError(f"unsupported kind: {kind}")
        tenant = str(tenant_id or "default")
        redacted = 0
        stored_rows: list[dict[str, Any]] = []
        for row in rows:
            before = json.dumps(row, ensure_ascii=False)
            out = redact_row(row) if redact else dict(row)
            if json.dumps(out, ensure_ascii=False) != before:
                redacted += 1
            stored_rows.append(out)
        created = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM recognized_tables WHERE tenant_id=? AND sha256=?", (tenant, sha256)
            ).fetchone()
            if exists:
                return {"inserted_rows": 0, "duplicate": True, "sha256": sha256, "kind": kind, "redacted_fields": 0, "tenant_id": tenant}
            db.execute(
                """INSERT INTO recognized_tables(tenant_id, kind, filename, sha256, columns, rows,
                                                confidence, redacted_fields, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (tenant, kind, filename, sha256, json.dumps(columns, ensure_ascii=False),
                 json.dumps(stored_rows, ensure_ascii=False), float(confidence), redacted, created),
            )
        return {"inserted_rows": len(stored_rows), "duplicate": False, "sha256": sha256, "kind": kind, "redacted_fields": redacted, "tenant_id": tenant}

    def query(self, *, kind: str | None = None, filters: dict[str, Any] | None = None,
              aggregate: dict[str, str] | None = None, limit: int = 200,
              tenant_id: str = "default") -> list[dict[str, Any]]:
        """读回 + 过滤 + 聚合（近似 SQL 的声明式查询）；只读本租户的行。"""
        rows: list[dict[str, Any]] = []
        with self._connect() as db:
            sql = ("SELECT tenant_id, kind, filename, sha256, columns, rows, confidence, created_at "
                   "FROM recognized_tables WHERE tenant_id=?")
            params: list[Any] = [str(tenant_id or "default")]
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            for r in db.execute(sql, params).fetchall():
                for row in json.loads(r["rows"]):
                    rows.append({"tenant_id": r["tenant_id"], "kind": r["kind"], "filename": r["filename"], **row})

        if filters:
            rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in (filters or {}).items())]

        if aggregate:
            # 支持 count / sum / avg 三类聚合，按 group_by 分组。
            group = aggregate.get("group_by")
            op = aggregate.get("op", "count")
            field = aggregate.get("field")
            buckets: dict[str, list[float]] = {}
            for r in rows:
                key = str(r.get(group)) if group else "__all__"
                val = r.get(field)
                try:
                    buckets.setdefault(key, []).append(float(val))
                except (TypeError, ValueError):
                    buckets.setdefault(key, []).append(0.0)
            out = []
            for key, vals in buckets.items():
                if op == "count":
                    v = len(vals)
                elif op == "sum":
                    v = sum(vals)
                elif op == "avg":
                    v = sum(vals) / len(vals) if vals else 0.0
                else:
                    v = None
                out.append({group or "group": key, op: v})
            return out[:limit]

        return rows[:limit]

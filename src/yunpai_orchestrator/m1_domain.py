"""M1 领域服务（S2 M1-1）：任务/文档持久化 + 确定性解析管线。

架构裁决（DESIGN_S2_M1_LOCALIZATION.md）：
- 新 sqlite 领域库（env YUNPAI_M1_DB；未配置回退 runtime/yunpai-m1.sqlite，与
  m0 sandbox 同目录先例；测试用 tmp+monkeypatch 隔离）；
- m1_tasks（含 parent_id 归档批次）+ m1_documents；任务状态机
  created→parsing→extracting→scoring→needs_review→done|failed（与旧 m1
  state.py/m1.json 契约一致，见 old-m1-semantics-notes.md）；
- tenant 表级隔离；upsert/RLock 幂等风格同 repository.py；
- 解析：确定性半边（JSON/CSV/表格订单语义）直通；自由格式（PDF/图片/工程图等）
  显式失败（C 型缺模型/外部线，不伪造）；失败码保持
  LOCAL_FIXTURE_UNSUPPORTED_FORMAT 常量以稳定契约（消息改为真实语义），
  provider 由 local_fixture 升级为 local（R027 登记）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .m0_sandbox import utc_now

TERMINAL_STATUSES = frozenset({"done", "failed"})
PAUSED_STATUSES = frozenset({"needs_review"})
UNSUPPORTED_FORMAT_CODE = "LOCAL_FIXTURE_UNSUPPORTED_FORMAT"
DOCUMENT_SCHEMA_VERSION = "m1.document.v2"

#: ``semantic_enrichment=false`` 时被剥离的订单行可选语义字段
#: （``manifests/m1.json#tools[0].input_schema.properties.semantic_enrichment``）。
SEMANTIC_LINE_FIELDS: tuple[str, ...] = (
    "name_normalized", "full_product_name", "product_category", "name_attributes",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS m1_tasks (
  task_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  tracking_task_id TEXT NOT NULL DEFAULT '',
  parent_id TEXT,
  kind TEXT NOT NULL DEFAULT 'file',          -- file | child | archive
  filename TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '',
  doc_type TEXT NOT NULL DEFAULT '',
  document_subtype TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m1_tasks_tenant_status ON m1_tasks(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_m1_tasks_parent ON m1_tasks(parent_id);
CREATE TABLE IF NOT EXISTS m1_documents (
  task_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  document_json TEXT NOT NULL,
  checksum TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES m1_tasks(task_id)
);
CREATE TABLE IF NOT EXISTS m1_reports (
  task_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  checksum TEXT NOT NULL,        -- 报告内容指纹（任务状态/置信度/文档/note）
  markdown TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES m1_tasks(task_id)
);
"""


def _default_db_path() -> str:
    return str(os.getenv("YUNPAI_M1_DB") or "runtime/yunpai-m1.sqlite")


class M1Store:
    """M1 任务/文档领域库（sqlite，单进程 + RLock）。"""

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

    # ---------- 任务 CRUD（tenant 表级隔离） ----------
    def create_task(self, *, tenant_id: str, tracking_task_id: str, kind: str,
                    filename: str = "", sha256_digest: str = "", parent_id: str | None = None) -> dict[str, Any]:
        task_id = f"m1-{uuid4().hex[:12]}"
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m1_tasks (task_id, tenant_id, tracking_task_id, parent_id, kind, "
                " filename, sha256, doc_type, document_subtype, status, stage, confidence, "
                " error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, tenant_id, tracking_task_id, parent_id, kind, filename, sha256_digest,
                 "", "", "created", "", 0.0, "", now, now))
        return self.get_task(tenant_id, task_id)  # type: ignore[return-value]

    def get_task(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m1_tasks WHERE task_id=? AND tenant_id=?",
                             (task_id, tenant_id)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, tenant_id: str, status: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM m1_tasks WHERE tenant_id=? "
        params: list[Any] = [tenant_id]
        if status:
            sql += "AND status=? "
            params.append(status)
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def children(self, tenant_id: str, parent_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m1_tasks WHERE tenant_id=? AND parent_id=? ORDER BY created_at",
                (tenant_id, parent_id)).fetchall()
        return [dict(r) for r in rows]

    def update(self, tenant_id: str, task_id: str, *, status: str | None = None,
               stage: str | None = None, confidence: float | None = None,
               error: str | None = None, doc_type: str | None = None,
               document_subtype: str | None = None) -> dict[str, Any] | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if stage is not None:
            sets.append("stage=?")
            params.append(stage)
        if confidence is not None:
            sets.append("confidence=?")
            params.append(float(confidence))
        if error is not None:
            sets.append("error=?")
            params.append(error)
        if doc_type is not None:
            sets.append("doc_type=?")
            params.append(doc_type)
        if document_subtype is not None:
            sets.append("document_subtype=?")
            params.append(document_subtype)
        if not sets:
            return self.get_task(tenant_id, task_id)
        sets.append("updated_at=?")
        params.append(utc_now())
        params += [task_id, tenant_id]
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE m1_tasks SET {', '.join(sets)} WHERE task_id=? AND tenant_id=?",
                       params)
        return self.get_task(tenant_id, task_id)

    def save_document(self, tenant_id: str, task_id: str,
                      document: dict[str, Any]) -> None:
        text = json.dumps(document, ensure_ascii=False)
        digest = sha256(text.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m1_documents (task_id, tenant_id, document_json, checksum, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "document_json=excluded.document_json, checksum=excluded.checksum",
                (task_id, tenant_id, text, digest, utc_now()))

    def get_document(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m1_documents WHERE task_id=? AND tenant_id=?",
                             (task_id, tenant_id)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["document"] = json.loads(out.pop("document_json"))
        except ValueError:
            out["document"] = None
        return out

    # ---------- 报告缓存（generate_m1_report.force 语义） ----------
    def save_report(self, tenant_id: str, task_id: str, *, checksum: str,
                    markdown: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m1_reports (task_id, tenant_id, checksum, markdown, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "checksum=excluded.checksum, markdown=excluded.markdown, "
                "created_at=excluded.created_at",
                (task_id, tenant_id, checksum, markdown, utc_now()))

    def get_report(self, tenant_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m1_reports WHERE task_id=? AND tenant_id=?",
                             (task_id, tenant_id)).fetchone()
        return dict(row) if row else None

    # ---------- 批次聚合 ----------
    def batch_summary(self, tenant_id: str, parent_id: str) -> dict[str, Any]:
        rows = self.children(tenant_id, parent_id)
        counts: dict[str, int] = {"created": 0, "parsing": 0, "extracting": 0,
                                  "scoring": 0, "needs_review": 0, "done": 0, "failed": 0}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "parent_id": parent_id, "child_count": len(rows),
            "done_count": counts["done"], "failed_count": counts["failed"],
            "review_count": counts["needs_review"], "pending_count": sum(
                counts[s] for s in ("created", "parsing", "extracting", "scoring")),
            "children": rows,
        }

    # ---------- 状态机辅助 ----------
    @staticmethod
    def next_after_parse(document: dict[str, Any] | None, *, confidence: float,
                         validation_issues: list[Any] | None) -> str:
        if document is None:
            return "failed"
        return "needs_review" if (confidence < 0.8 or bool(validation_issues)) else "done"


def task_summary(task: dict[str, Any], *, document: dict[str, Any] | None = None) -> dict[str, Any]:
    """handler 输出形态的任务摘要（对齐 manifest list_m1_tasks/get_m1_task 摘要）。"""
    out: dict[str, Any] = {
        "task_id": task["task_id"], "filename": task["filename"],
        "status": task["status"], "confidence": task["confidence"],
        "tenant_id": task["tenant_id"], "kind": task["kind"],
        "parent_id": task["parent_id"],
    }
    if task.get("error"):
        out["error"] = task["error"]
    if task.get("doc_type"):
        out["doc_type"] = task["doc_type"]
    if task.get("document_subtype"):
        out["document_subtype"] = task["document_subtype"]
    if document is not None:
        out["document"] = document
    return out


def task_readback(task: dict[str, Any], *, document: dict[str, Any] | None = None) -> dict[str, Any]:
    """``get_m1_task`` 的契约形状（``manifests/m1.json#tools[2].output_schema``）。

    与 :func:`task_summary` 的区别：这是**工具输出**（顶层 task_id/status/...），
    不是任务摘要；handler 必须返回该形状，否则 ``registry.call`` 的输出校验会报
    ``'task_id' is a required property``（信封形状只能由
    ``contracts.normalize_contract_result`` 事后补充，不能由 handler 自己造）。
    """
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "processing_stage": str(task.get("stage") or ""),
        "error": task.get("error") or None,
        "document_schema_version": DOCUMENT_SCHEMA_VERSION if document is not None else None,
        "document": document,
    }


def batch_readback(parent: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """``get_m1_batch`` 的契约形状（``manifests/m1.json#tools[3].output_schema``）。

    该契约 ``additionalProperties: false``，因此只能返回这 7 个键；trace/evidence
    由 ``contracts.normalize_contract_result`` 在 registry 层补充。
    """
    return {
        "parent": task_summary(parent),
        "children": [task_summary(child) for child in summary.get("children") or []],
        "child_count": int(summary.get("child_count") or 0),
        "done_count": int(summary.get("done_count") or 0),
        "failed_count": int(summary.get("failed_count") or 0),
        "review_count": int(summary.get("review_count") or 0),
        "pending_count": int(summary.get("pending_count") or 0),
    }


def document_readback(task: dict[str, Any], document: dict[str, Any] | None) -> dict[str, Any]:
    """``get_m1_document`` 的契约形状（``manifests/m1.json#tools[4].output_schema``）。

    契约的必填字段只有 ``source``；文档不存在时 handler 应显式失败，因此这里对
    ``document=None`` 直接抛错，绝不返回半成品文档。
    """
    if document is None:
        raise ValueError(f"document not ready for task: {task['task_id']}"
                         f"（status={task['status']}）")
    out = dict(document)
    source = out.get("source")
    if not isinstance(source, dict):
        out["source"] = {"original_filename": task.get("filename") or "",
                         "sha256": task.get("sha256") or ""}
    return out


def _json_content(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strip_semantic_fields(line: dict[str, Any]) -> dict[str, Any]:
    """剥离订单行的可选语义字段（保留基础事实）。"""
    return {key: value for key, value in line.items() if key not in SEMANTIC_LINE_FIELDS}


def process_upload_file(*, filename: str, raw: bytes, tenant_id: str,
                        tracking_task_id: str, parent_id: str | None = None,
                        kind: str = "file", fixture: dict[str, Any] | None = None,
                        store: M1Store | None = None,
                        doc_type_hint: str | None = None,
                        document_subtype_hint: str | None = None,
                        semantic_enrichment: bool = True) -> dict[str, Any]:
    """确定性解析文件 → m1.document.v2 任务结果（真实持久化，provider=local）。

    支持：JSON（order 结构）、XLSX/XLSM 订单模板（order_semantics 证据链/
    order_workbook 坐标兜底）→ 自由格式（PDF/图片/工程图/其它）显式失败
    （C 型/外部线，不伪造）。失败码保持 LOCAL_FIXTURE_UNSUPPORTED_FORMAT 常量
    以稳定契约（R027 登记语义升级）。

    契约参数（``manifests/m1.json#tools[0]``/``#tools[1]`` 的 input_schema）：
    - ``doc_type_hint`` / ``document_subtype_hint``：只影响 ``document_type`` /
      ``document_subtype`` 分类标签，**不覆盖** header/lines 等源文件事实；
    - ``semantic_enrichment=False``：剥离订单行的可选语义字段
      （name_normalized / full_product_name / product_category / name_attributes），
      订单号、型号、数量、单位等基础事实保持不变。
    """
    used_fixture = False
    store = store or M1Store()
    digest = sha256(raw).hexdigest()
    task = store.create_task(tenant_id=tenant_id, tracking_task_id=tracking_task_id,
                             kind=kind, filename=filename, sha256_digest=digest,
                             parent_id=parent_id)
    task_id = task["task_id"]
    store.update(tenant_id, task_id, status="parsing", stage="parsing")

    parsed_source: dict[str, Any] = {}
    parser_meta: dict[str, Any] = {}
    from .order_semantics import sniff_xlsx_bytes, workbook_parse_candidate

    candidate = workbook_parse_candidate(filename, raw)
    if candidate is not None:
        parsed_source = candidate.get("document") or {}
        parser_meta = {
            "name": str(candidate.get("parser_name") or "order.parser.v2"),
            "version": str(candidate.get("parser_version") or "order.parser.v2"),
            "revision": str(candidate.get("parser_version") or "order.parser.v2"),
        }
    elif sniff_xlsx_bytes(raw):
        try:
            from .order_workbook import parse_order_workbook

            legacy = parse_order_workbook(filename, raw)
            if legacy.get("order_id") or legacy.get("lines"):
                parsed_source = legacy
                parser_meta = {
                    "name": "order.workbook.coordinates.v1",
                    "version": "order.workbook.coordinates.v1",
                    "revision": "order.workbook.coordinates.v1",
                }
        except Exception:  # noqa: BLE001 - 坐标兜底失败则回落其他路径
            parsed_source = {}
    if not parsed_source and isinstance(fixture, dict) and fixture:
        parsed_source = fixture
        used_fixture = True
    if not parsed_source:
        parsed_source = _json_content(raw)

    if not parsed_source:
        message = ("本地 M1 无法解析该文件（支持 JSON、含订单结构证据的 XLSX/XLSM；"
                   "PDF/图片/工程图等自由格式需模型 Provider（C 型），缺模型显式失败不伪造）")
        store.update(tenant_id, task_id, status="failed", stage="failed",
                     error=UNSUPPORTED_FORMAT_CODE)
        return {
            "task_id": task_id, "status": "failed",
            "code": UNSUPPORTED_FORMAT_CODE, "message": message,
            "provider": "local_fixture" if used_fixture else "local",
            "fixture": used_fixture, "environment": "local_m1",
            "document": None, "document_schema_version": None, "schema_version": None,
            "needs_review": False, "overall_confidence": 0.0,
            "evidence": [],
        }

    lines = parsed_source.get("lines") or parsed_source.get("records") or []
    confidence = float(parsed_source.get("confidence", 1.0 if lines else 0.0))
    source_issues = parsed_source.get("validation_issues") if isinstance(
        parsed_source.get("validation_issues"), list) else []
    header = {
        "order_id": parsed_source.get("order_id"),
        "product_code": parsed_source.get("product_code"),
        "quantity": parsed_source.get("quantity"),
        "due_date": parsed_source.get("due_date"),
    }
    missing = [key for key, value in header.items() if value in (None, "")]
    # 契约 hint 只改分类标签，不改 header/lines 等源文件事实。
    doc_type = str(doc_type_hint or "") or "order"
    doc_subtype = str(document_subtype_hint or "") or "customer_order"
    order_prefix = str(parsed_source.get("order_id") or "order")

    def _stable_line_id(line: dict[str, Any], index: int) -> str:
        existing = line.get("line_id")
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
        sheet = line.get("sheet")
        row = line.get("row")
        if sheet is not None and row is not None:
            return f"{order_prefix}::{sheet}!R{row}"
        return f"{order_prefix}::L{index:02d}"

    normalized_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            continue
        normalized = dict(line)
        normalized["line_id"] = _stable_line_id(normalized, index)
        if "model" not in normalized:
            normalized["model"] = normalized.get("product_code")
        if "name_raw" not in normalized and normalized.get("product_name") is not None:
            normalized["name_raw"] = normalized.get("product_name")
        normalized_lines.append(normalized)
    lines = normalized_lines
    if not semantic_enrichment:
        lines = [_strip_semantic_fields(line) for line in lines]
    totals: dict[str, Any] = {}
    total_quantity = parsed_source.get("total_quantity")
    if total_quantity is None:
        total_quantity = parsed_source.get("quantity")
    if total_quantity is not None:
        totals["quantity"] = total_quantity
    if parsed_source.get("total_amount") is not None:
        totals["amount"] = parsed_source.get("total_amount")
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "source": {"original_filename": filename, "sha256": digest},
        "document_type": doc_type, "document_subtype": doc_subtype,
        "header": header, "lines": lines, "totals": totals, "field_meta": {},
        "validation_issues": [
            *source_issues,
            *({"code": "MISSING_FIELD", "message": f"缺少字段: {key}",
               "paths": [f"$.header.{key}"]} for key in missing),
        ],
    }
    field_evidence = parsed_source.get("field_evidence")
    if isinstance(field_evidence, list) and field_evidence:
        document["field_evidence"] = field_evidence
    sheet_docs = parsed_source.get("sheet_docs")
    if isinstance(sheet_docs, list) and sheet_docs:
        document["sheet_docs"] = sheet_docs
    if parser_meta:
        document["parser_name"] = parser_meta["name"]
        document["parser_version"] = parser_meta["version"]
    validation_issues = document["validation_issues"]
    status = M1Store.next_after_parse(document, confidence=confidence,
                                      validation_issues=validation_issues)
    stage = "review" if status == "needs_review" else "complete"
    store.update(tenant_id, task_id, status=status, stage=stage, confidence=confidence,
                 doc_type=doc_type, document_subtype=doc_subtype)
    store.save_document(tenant_id, task_id, document)
    provider = "local_fixture" if used_fixture else "local"
    parser_detail = ("order_semantics/" + parser_meta["version"] if parser_meta
                     else ("fixture-injected" if used_fixture else "json"))
    return {
        "task_id": task_id, "status": status,
        "processing_stage": stage,
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "document_schema_version": DOCUMENT_SCHEMA_VERSION,
        "document_subtype": doc_subtype, "needs_review": status == "needs_review",
        "overall_confidence": confidence, "document": document,
        "extraction": {"order": header, "lines": lines},
        "order": header, "lines": lines, "missing": missing,
        "parser": parser_meta or None,
        "provider": provider, "fixture": used_fixture, "environment": "local_m1",
        "evidence": [_evidence_local(filename, parser_detail)],
    }


def _evidence_local(filename: str, parser_detail: str) -> dict[str, Any]:
    return {"key": "source", "locator": {"filename": filename},
            "excerpt": f"m1.document.v2 确定性解析（{parser_detail}，本地 M1 领域库持久化）"}


# ---------------------------------------------------------------------------
# 人工审核（submit_m1_review 契约实现）
# ---------------------------------------------------------------------------

def _normalize_line_corrections(value: Any) -> list[dict[str, Any]]:
    """``line_corrections``：``[{line_id, corrections}]`` 或 ``{line_id: {..}}``。"""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or not item.get("line_id"):
                raise ValueError("line_corrections 每项必须含 line_id")
            corrections = item.get("corrections")
            if not isinstance(corrections, dict) or not corrections:
                raise ValueError(f"line_corrections[{item.get('line_id')}] 缺少 corrections")
            out.append({"line_id": str(item["line_id"]), "corrections": dict(corrections)})
        return out
    if isinstance(value, dict):
        out = []
        for line_id, corrections in value.items():
            if not isinstance(corrections, dict) or not corrections:
                raise ValueError(f"line_corrections[{line_id}] 缺少 corrections")
            out.append({"line_id": str(line_id), "corrections": dict(corrections)})
        return out
    raise ValueError("line_corrections 必须是数组或对象")


def _normalize_issue_resolutions(value: Any) -> list[dict[str, Any]]:
    """``issue_resolutions``：``[{code, resolution, comment}]`` 或 ``{code: ...}``。"""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or not item.get("code"):
                raise ValueError("issue_resolutions 每项必须含 code")
            out.append({"code": str(item["code"]),
                        "resolution": str(item.get("resolution") or "resolved"),
                        "comment": str(item.get("comment") or "")})
        return out
    if isinstance(value, dict):
        out = []
        for code, resolution in value.items():
            if isinstance(resolution, dict):
                out.append({"code": str(code),
                            "resolution": str(resolution.get("resolution") or "resolved"),
                            "comment": str(resolution.get("comment") or "")})
            else:
                out.append({"code": str(code), "resolution": str(resolution), "comment": ""})
        return out
    raise ValueError("issue_resolutions 必须是数组或对象")


def apply_review(store: M1Store, *, tenant_id: str, task_id: str, approve: bool = True,
                 reviewer: str = "anonymous", comment: str = "",
                 corrections: dict[str, Any] | None = None,
                 header_corrections: dict[str, Any] | None = None,
                 line_corrections: Any = None,
                 issue_resolutions: Any = None) -> dict[str, Any]:
    """``submit_m1_review`` 的契约实现：approve → done（应用修正），驳回 → failed。

    契约参数（``manifests/m1.json#tools[10].input_schema``）全部生效：
    ``corrections``/``header_corrections`` 改订单头字段，``line_corrections`` 按
    ``line_id`` 改明细行，``issue_resolutions`` 记录校验问题处理结果。
    引用不存在的 ``line_id``/``code`` 一律 fail-closed 抛错，绝不静默丢弃修正。
    """
    task = store.get_task(tenant_id, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    if task["status"] != "needs_review":
        raise ValueError(f"task {task_id} 不在待审核队列（status={task['status']}）")
    doc_row = store.get_document(tenant_id, task_id)
    stored = doc_row["document"] if doc_row and doc_row.get("document") else None
    document = json.loads(json.dumps(stored)) if isinstance(stored, dict) else None
    corrected: list[str] = []
    if approve and document is not None:
        header = document.get("header") if isinstance(document.get("header"), dict) else {}
        document["header"] = header
        if isinstance(header_corrections, dict) and header_corrections:
            header.update(header_corrections)
            corrected.extend(str(key) for key in header_corrections)
        if isinstance(corrections, dict) and corrections:
            for key, value in corrections.items():
                if key in header:
                    header[key] = value
                elif key in document:
                    document[key] = value
                else:
                    header[key] = value  # 顶层订单字段按 header 语义修正
                corrected.append(str(key))
        lines = document.get("lines") if isinstance(document.get("lines"), list) else []
        by_id = {str(line.get("line_id")): line for line in lines if isinstance(line, dict)}
        for item in _normalize_line_corrections(line_corrections):
            line = by_id.get(item["line_id"])
            if line is None:
                raise ValueError(f"line_corrections 引用不存在的 line_id: {item['line_id']}")
            line.update(item["corrections"])
            corrected.extend(f"{item['line_id']}.{key}" for key in item["corrections"])
        issues = document.get("validation_issues") \
            if isinstance(document.get("validation_issues"), list) else []
        for item in _normalize_issue_resolutions(issue_resolutions):
            matched = [issue for issue in issues
                       if isinstance(issue, dict) and str(issue.get("code")) == item["code"]]
            if not matched:
                raise ValueError(f"issue_resolutions 引用不存在的校验问题: {item['code']}")
            for issue in matched:
                issue["resolution"] = item["resolution"]
                issue["resolution_comment"] = item["comment"]
        if corrected:
            header_keys = {key for key in corrected if "." not in key}
            document["validation_issues"] = [
                issue for issue in issues
                if not (isinstance(issue, dict) and issue.get("code") == "MISSING_FIELD"
                        and str((issue.get("paths") or [""])[0]).replace("$.header.", "")
                        in header_keys)
            ]
            document["review"] = {"approved": True, "reviewer": reviewer,
                                  "comment": comment, "corrected_fields": corrected}
            store.save_document(tenant_id, task_id, document)
        status, stage = "done", "complete"
        confidence = float(task["confidence"] or 0.0)
        if corrected:
            confidence = max(confidence, 0.95)
    else:
        status, stage = "failed", "rejected"
        confidence = float(task["confidence"] or 0.0)
        store.update(tenant_id, task_id, error="review_rejected")
    store.update(tenant_id, task_id, status=status, stage=stage, confidence=confidence)
    scored: dict[str, Any] = {}
    if approve and document is not None:
        extraction = document.get("extraction")
        if isinstance(extraction, dict):
            scored = extraction
    return {"task_id": task_id, "status": status, "scored_result": scored}


# ---------------------------------------------------------------------------
# 识别报告（generate_m1_report 契约实现，含 force 缓存语义）
# ---------------------------------------------------------------------------

def _report_targets(store: M1Store, tenant_id: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    if task["kind"] == "archive":
        for child in store.children(tenant_id, task["task_id"]):
            doc_row = store.get_document(tenant_id, child["task_id"])
            targets.append({"task": child,
                            "document": doc_row["document"] if doc_row else None})
        return targets
    doc_row = store.get_document(tenant_id, task["task_id"])
    targets.append({"task": task, "document": doc_row["document"] if doc_row else None})
    return targets


def _report_checksum(targets: list[dict[str, Any]], note: str) -> str:
    """报告内容指纹：任务状态/置信度/文档/note 任一变化即失效缓存。"""
    payload = json.dumps(
        {"targets": [{"task_id": item["task"]["task_id"],
                      "status": item["task"]["status"],
                      "confidence": item["task"]["confidence"],
                      "document": item["document"]} for item in targets],
         "note": note},
        ensure_ascii=False, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def render_report(*, task: dict[str, Any], targets: list[dict[str, Any]], note: str = "") -> str:
    lines: list[str] = ["# M1 识别报告", f"- task: {task['task_id']}",
                        f"- 文件: {task['filename']}",
                        f"- 状态: {task['status']} (stage={task['stage']})",
                        f"- 置信度: {task['confidence'] or 0.0}"]
    for index, item in enumerate(targets, start=1):
        doc = item["document"] or {}
        header = doc.get("header") or {}
        lines.append(f"\n## 文档 {index}：{item['task']['filename']}")
        lines.append(f"- 状态: {item['task']['status']} / 置信度 {item['task']['confidence'] or 0.0}")
        lines.append(f"- 订单号: {header.get('order_id') or '-'} / "
                     f"产品: {header.get('product_code') or '-'} / "
                     f"数量: {header.get('quantity') or '-'}")
        for j, line in enumerate(doc.get("lines") or [], start=1):
            if not isinstance(line, dict):
                continue
            lines.append(f"{j}. {line.get('line_no')} | {line.get('product_code')} | "
                         f"{line.get('quantity')} | {line.get('uom')}")
        issues = doc.get("validation_issues") or []
        if issues:
            lines.append("\n校验问题：")
            lines.extend(f"- [{issue.get('code')}] {issue.get('message')}"
                         for issue in issues if isinstance(issue, dict))
    if note:
        lines.append(f"\n---\n备注：{note}")
    return "\n".join(lines)


def build_report(store: M1Store, *, tenant_id: str, task_id: str, note: str = "",
                 force: bool = False) -> dict[str, Any]:
    """``generate_m1_report`` 的契约实现。

    ``force=True`` 强制重新生成并覆盖缓存；``force=False`` 在文档/状态/备注未变化时
    直接返回缓存（``report_status="cached"``），否则生成并写入缓存。
    """
    task = store.get_task(tenant_id, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    targets = _report_targets(store, tenant_id, task)
    checksum = _report_checksum(targets, note)
    if not force:
        cached = store.get_report(tenant_id, task_id)
        if cached is not None and str(cached.get("checksum") or "") == checksum:
            return {"task_id": task_id, "report_kind": "markdown",
                    "report_status": "cached",
                    "message": str(cached.get("markdown") or "")}
    message = render_report(task=task, targets=targets, note=note)
    store.save_report(tenant_id, task_id, checksum=checksum, markdown=message)
    return {"task_id": task_id, "report_kind": "markdown",
            "report_status": "generated", "message": message}

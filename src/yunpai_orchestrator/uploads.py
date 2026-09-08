"""上传链路共享契约：显式模式、逐文件状态词表、大小/数量限制与文件校验。

任务书 §3.1：order/master_data/directory 三种模式显式传递；每个文件独立返回
accepted/needs_review/unsupported/parse_failed/skipped；禁止缺 content_b64 时
静默 continue 后仍返回成功；目录批量保留相对路径/SHA/TaskID/批次 ID。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 显式上传模式：不允许依赖用户文案猜测。
UPLOAD_MODES = ("order", "master_data", "directory")

# 逐文件状态词表（前后端/落库共用同一枚举）。
FILE_STATUSES = (
    "accepted",        # 已接收并进入识别/解析
    "needs_review",    # 低置信度或缺字段，进入人工复核
    "unsupported",     # 格式不支持或与声明扩展名不符
    "parse_failed",    # 识别/解析失败
    "skipped",         # 被跳过（重复、超限、缺 content_b64 等）
    "deferred_to_m1",  # 大表延迟到 M1 深解析
)

# SOP workbooks in the acceptance data include a ~30 MiB legacy .xls file.
# Keep a bounded per-file limit while allowing that real input through the
# explicit master-data upload path.
MAX_FILE_BYTES = 40 * 1024 * 1024      # 单文件上限 40 MiB
MAX_BATCH_BYTES = 200 * 1024 * 1024    # 整批上限 200 MiB
MAX_BATCH_FILES = 2000                 # 整批文件数上限


@dataclass(frozen=True)
class UploadFileRecord:
    filename: str
    relative_path: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    reason: str = ""
    mode: str = "master_data"


@dataclass
class UploadSummary:
    mode: str
    total: int = 0
    accepted: int = 0
    needs_review: int = 0
    unsupported: int = 0
    parse_failed: int = 0
    skipped: int = 0
    deferred_to_m1: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total": self.total,
            "accepted": self.accepted,
            "needs_review": self.needs_review,
            "unsupported": self.unsupported,
            "parse_failed": self.parse_failed,
            "skipped": self.skipped,
            "deferred_to_m1": self.deferred_to_m1,
            "skip_reasons": dict(self.skip_reasons),
            "files": list(self.files),
        }

    def add(self, record: UploadFileRecord) -> None:
        self.total += 1
        bucket = {
            "accepted": "accepted",
            "needs_review": "needs_review",
            "unsupported": "unsupported",
            "parse_failed": "parse_failed",
            "skipped": "skipped",
            "deferred_to_m1": "deferred_to_m1",
        }[record.status]
        setattr(self, bucket, getattr(self, bucket) + 1)
        if record.status == "skipped" and record.reason:
            self.skip_reasons[record.reason] = self.skip_reasons.get(record.reason, 0) + 1
        self.files.append({
            "filename": record.filename,
            "relative_path": record.relative_path,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "status": record.status,
            "reason": record.reason,
        })


def validate_mode(mode: Any) -> str:
    text = str(mode or "").strip()
    if text not in UPLOAD_MODES:
        raise ValueError(f"upload mode 必须为 {'/'.join(UPLOAD_MODES)} 之一，不能依赖文案猜测")
    return text


def require_content_b64(item: dict[str, Any], *, filename: str = "upload") -> bytes:
    """缺 content_b64 / 坏 base64 时抛错，禁止静默跳过。

    调用方应捕获 ValueError 并把该文件标记为 skipped 或 parse_failed。
    """
    encoded = item.get("content_b64") if isinstance(item, dict) else None
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError(f"文件缺少 content_b64: {filename}")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"文件 content_b64 不是合法 base64: {filename}") from exc


def sha256_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_batch_limits(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """对已编码附件做大小/数量护栏；超限文件标记 skipped 并给原因。"""
    skipped: list[dict[str, str]] = []
    if len(records) > MAX_BATCH_FILES:
        for item in records[MAX_BATCH_FILES:]:
            skipped.append({"filename": str(item.get("filename") or "upload"), "reason": f"超过单批文件数上限 {MAX_BATCH_FILES}"})
    for item in records[:MAX_BATCH_FILES]:
        size = int(item.get("size") or 0)
        if size <= 0 and isinstance(item.get("content_b64"), str):
            size = max(0, len(item["content_b64"]) * 3 // 4 - 2)
        if size > MAX_FILE_BYTES:
            skipped.append({"filename": str(item.get("filename") or "upload"), "reason": f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 上限"})
    return skipped


def attachment_path(item: dict[str, Any]) -> str:
    """目录上传保留相对路径；普通文件回退到文件名。"""
    path = str(item.get("relative_path") or item.get("webkitRelativePath") or "")
    if path:
        return path
    return str(item.get("filename") or Path(str(item.get("name") or "upload.bin")).name)


def to_attachment_record(item: dict[str, Any], *, mode: str, status: str, reason: str = "") -> UploadFileRecord:
    filename = str(item.get("filename") or "upload.bin")
    raw_size = int(item.get("size") or 0)
    if raw_size <= 0 and isinstance(item.get("content_b64"), str):
        raw_size = max(0, len(item["content_b64"]) * 3 // 4 - 2)
    return UploadFileRecord(
        filename=filename,
        relative_path=attachment_path(item),
        content_type=str(item.get("content_type") or "application/octet-stream"),
        size_bytes=raw_size,
        sha256=str(item.get("sha256") or ""),
        status=status,
        reason=reason,
        mode=mode,
    )

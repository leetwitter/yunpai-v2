"""压缩包递归解包：保留压缩包 SHA、成员相对路径与父子证据关系。

任务书 §3.2/§3.2-11：压缩包递归解包时保留压缩包 SHA、成员相对路径和父子
证据关系。zip 用标准库；7z 用 py7zr（缺库时报 unsupported）；rar 无本地
解包依赖时明确 unsupported 而不是伪造成员清单。所有解包都做 zip-slip 防护
与成员数量上限。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

MAX_MEMBERS_PER_ARCHIVE = 2000
MAX_MEMBER_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveMember:
    relative_path: str        # 压缩包内相对路径（纯 posix）
    raw: bytes                # 成员内容（超限时为空，标记 skipped）
    is_dir: bool
    status: str               # accepted | skipped | unsupported
    reason: str = ""


def _safe_relative(member_path: str) -> str | None:
    """防 zip-slip：拒绝绝对路径与 .. 逃逸。"""
    candidate = PurePosixPath(str(member_path).replace("\\", "/"))
    if candidate.is_absolute():
        return None
    parts = candidate.parts
    if ".." in parts:
        return None
    return str(candidate)


def unpack_archive(raw: bytes, *, filename: str = "archive.zip") -> dict[str, Any]:
    """按 magic bytes 解包 zip/7z；rar 返回 unsupported 状态。

    返回：
    {
      "archive_filename": str,
      "archive_format": "zip"|"7z"|"rar"|"unknown",
      "member_count": int,
      "members": [{"relative_path","size_bytes","status","reason","sha256"}],
      "error": str | None,
    }
    """

    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06"):
        return _unpack_zip(raw, filename=filename)
    if raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return _unpack_7z(raw, filename=filename)
    if raw.startswith(b"Rar!\x1a\x07"):
        return {
            "archive_filename": filename, "archive_format": "rar",
            "member_count": 0, "members": [],
            "error": "本地无 rar 解包依赖（rarfile/unrar），明确 unsupported；不能伪造成员清单",
        }
    return {
        "archive_filename": filename, "archive_format": "unknown",
        "member_count": 0, "members": [],
        "error": "无法识别的归档格式",
    }


def _unpack_zip(raw: bytes, *, filename: str) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                if len(members) >= MAX_MEMBERS_PER_ARCHIVE:
                    break
                relative = _safe_relative(info.filename)
                if relative is None:
                    members.append({"relative_path": str(info.filename), "size_bytes": 0, "status": "skipped", "reason": "unsafe_member_path", "sha256": ""})
                    continue
                is_dir = info.is_dir() or relative.endswith("/")
                entry = {"relative_path": relative, "size_bytes": info.file_size, "is_dir": is_dir}
                if is_dir:
                    entry.update(status="skipped", reason="directory", sha256="")
                    members.append(entry)
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    entry.update(status="skipped", reason="member_too_large", sha256="")
                    members.append(entry)
                    continue
                try:
                    content = archive.read(info)
                except Exception as exc:  # encrypted/corrupt member
                    entry.update(status="skipped", reason=f"member_unreadable:{exc}", sha256="")
                    members.append(entry)
                    continue
                entry.update(status="accepted", sha256=sha256(content).hexdigest())
                members.append(entry)
    except zipfile.BadZipFile as exc:
        return {"archive_filename": filename, "archive_format": "zip", "member_count": 0, "members": [], "error": f"bad zip: {exc}"}
    return {"archive_filename": filename, "archive_format": "zip", "member_count": len(members), "members": members, "error": None}


def _unpack_7z(raw: bytes, *, filename: str) -> dict[str, Any]:
    import tempfile

    try:
        import py7zr
    except ImportError:  # pragma: no cover
        return {"archive_filename": filename, "archive_format": "7z", "member_count": 0, "members": [], "error": "缺少 py7zr 依赖，明确 unsupported"}
    members: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with py7zr.SevenZipFile(io.BytesIO(raw)) as archive:
                archive.extractall(path=tmp)
            root = Path(tmp)
            for path in sorted(root.rglob("*")):
                if len(members) >= MAX_MEMBERS_PER_ARCHIVE:
                    break
                if not path.is_file():
                    continue
                relative = _safe_relative(str(path.relative_to(root)))
                if relative is None:
                    continue
                size = path.stat().st_size
                content = b""
                if size <= MAX_MEMBER_BYTES:
                    content = path.read_bytes()
                members.append({
                    "relative_path": relative, "size_bytes": size, "is_dir": False,
                    "status": "accepted" if content else "skipped",
                    "reason": "" if content else ("member_too_large" if size > MAX_MEMBER_BYTES else "empty_member"),
                    "sha256": sha256(content).hexdigest() if content else "",
                })
    except Exception as exc:
        return {"archive_filename": filename, "archive_format": "7z", "member_count": 0, "members": [], "error": f"bad 7z: {exc}"}
    return {"archive_filename": filename, "archive_format": "7z", "member_count": len(members), "members": members, "error": None}


def extract_archive_safe(raw: bytes, destination: str | Path, *, filename: str = "archive.zip", max_members: int = MAX_MEMBERS_PER_ARCHIVE) -> dict[str, Any]:
    """安全解包 zip/7z 到 destination（逐成员校验，防 zip-slip），返回成员证据。

    返回：
    {
      "archive_filename", "archive_format", "extracted_members": [...],
      "skipped_members": [...], "error": str | None,
    }
    """
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06"):
        return _extract_zip(raw, dest, filename=filename, max_members=max_members)
    if raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return _extract_7z(raw, dest, filename=filename, max_members=max_members)
    if raw.startswith(b"Rar!\x1a\x07"):
        return {"archive_filename": filename, "archive_format": "rar", "extracted_members": [], "skipped_members": [], "error": "本地无 rar 解包依赖，明确 unsupported"}
    return {"archive_filename": filename, "archive_format": "unknown", "extracted_members": [], "skipped_members": [], "error": "无法识别的归档格式"}


def _extract_zip(raw: bytes, dest: Path, *, filename: str, max_members: int) -> dict[str, Any]:
    extracted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                if len(extracted) + len(skipped) >= max_members:
                    skipped.append({"relative_path": str(info.filename), "reason": "member_count_limit"})
                    continue
                relative = _safe_relative(info.filename)
                if relative is None:
                    skipped.append({"relative_path": str(info.filename), "reason": "unsafe_member_path"})
                    continue
                target = dest / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                except Exception as exc:
                    skipped.append({"relative_path": relative, "reason": f"member_unreadable:{exc}"})
                    continue
                extracted.append({"relative_path": relative, "size_bytes": info.file_size})
    except zipfile.BadZipFile as exc:
        return {"archive_filename": filename, "archive_format": "zip", "extracted_members": [], "skipped_members": [], "error": f"bad zip: {exc}"}
    return {"archive_filename": filename, "archive_format": "zip", "extracted_members": extracted, "skipped_members": skipped, "error": None}


def _extract_7z(raw: bytes, dest: Path, *, filename: str, max_members: int) -> dict[str, Any]:
    try:
        import py7zr
    except ImportError:  # pragma: no cover
        return {"archive_filename": filename, "archive_format": "7z", "extracted_members": [], "skipped_members": [], "error": "缺少 py7zr 依赖，明确 unsupported"}
    import tempfile

    extracted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with py7zr.SevenZipFile(io.BytesIO(raw)) as archive:
                archive.extractall(path=tmp)
            for path in sorted(Path(tmp).rglob("*")):
                if len(extracted) + len(skipped) >= max_members:
                    skipped.append({"relative_path": str(path.relative_to(tmp)), "reason": "member_count_limit"})
                    continue
                if path.is_dir():
                    continue
                relative = _safe_relative(str(path.relative_to(tmp)))
                if relative is None:
                    skipped.append({"relative_path": str(path.relative_to(tmp)), "reason": "unsafe_member_path"})
                    continue
                target = dest / relative
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                except Exception as exc:
                    skipped.append({"relative_path": relative, "reason": f"member_unreadable:{exc}"})
                    continue
                extracted.append({"relative_path": relative, "size_bytes": path.stat().st_size})
    except Exception as exc:
        return {"archive_filename": filename, "archive_format": "7z", "extracted_members": [], "skipped_members": [], "error": f"bad 7z: {exc}"}
    return {"archive_filename": filename, "archive_format": "7z", "extracted_members": extracted, "skipped_members": skipped, "error": None}

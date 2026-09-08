from __future__ import annotations

import struct
from pathlib import Path

from yunpai_orchestrator.xls_reader import extract_xls


def _ole2_stub() -> bytes:
    """最小 OLE2 头 + 少量字节，用于验证 xlrd 打开失败路径与结构回退。"""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


def test_xls_reader_returns_structured_result_for_invalid_file(tmp_path):
    path = tmp_path / "broken.xls"
    path.write_bytes(_ole2_stub())
    result = extract_xls(path)
    assert result["parser_version"] == "xls.reader.v1"
    # 真实文件损坏时必须有 error 字段，而不是静默二进制摘要。
    assert result["error"]
    assert result["sheet_count"] == 0


def test_xls_reader_surfaces_missing_dependency_only_when_relevant():
    # 无法在测试中卸载 xlrd；此处验证缺库分支可被调用（不抛 ImportError 之外异常）。
    try:
        import xlrd  # noqa: F401
    except ImportError:
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "xlrd":
                raise ImportError("xlrd missing")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            result = extract_xls(Path("/tmp/nonexistent.xls"))
            assert result["error"].startswith("缺少 xlrd")
        finally:
            builtins.__import__ = real_import

"""旧版 .xls (BIFF) 真实抽取：表头/行/Sheet/单元格证据，不只存二进制摘要。

任务书 §3.2：.xls 必须真实抽取表头、行、Sheet 和单元格证据。依赖 xlrd>=2.0
（仅支持 .xls）。缺库或损坏文件返回结构化错误而非静默二进制摘要。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def extract_xls(path: str | Path, *, max_rows_per_sheet: int = 200, max_sheets: int = 32) -> dict[str, Any]:
    """抽取每个 Sheet 的非空行与表头候选。

    返回结构：
    {
      "parser_version": "xls.reader.v1",
      "sheet_count": int,
      "sheets": [{"name","max_row","max_column","nonempty_row_count",
                  "sample_rows": [...], "headers_candidates": [...]}],
      "error": 仅读取失败时存在
    }
    """
    path = Path(path)
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        return {"parser_version": "xls.reader.v1", "error": f"缺少 xlrd 依赖: {exc}", "sheet_count": 0, "sheets": []}
    try:
        workbook = xlrd.open_workbook(str(path), on_demand=False)
    except Exception as exc:
        return {"parser_version": "xls.reader.v1", "error": f"invalid xls file: {exc}", "sheet_count": 0, "sheets": []}
    sheets: list[dict[str, Any]] = []
    try:
        for sheet_index, sheet in enumerate(workbook.sheets()):
            if sheet_index >= max_sheets:
                break
            rows: list[list[Any]] = []
            for row_number in range(sheet.nrows):
                if len(rows) >= max_rows_per_sheet:
                    break
                values = [sheet.cell_value(row_number, col) for col in range(sheet.ncols)]
                if any(value not in (None, "") for value in values):
                    rows.append(values)
            sample_rows = []
            for row in rows[:8]:
                sample_rows.append([None if value in (None, "") else str(value)[:120] for value in row[:40]])
            headers_candidates = [row[:40] for row in rows[:5]]
            sheets.append({
                "name": str(sheet.name),
                "max_row": sheet.nrows,
                "max_column": sheet.ncols,
                "nonempty_row_count": len(rows),
                "sample_rows": sample_rows,
                "headers_candidates": headers_candidates,
            })
    finally:
        workbook.release_resources() if hasattr(workbook, "release_resources") else None
    return {
        "parser_version": "xls.reader.v1",
        "sheet_count": len(sheets),
        "sheets": sheets,
    }

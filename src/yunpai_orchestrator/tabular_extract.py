"""确定性批量抽取：用 LLM 识别的「列映射」遍历所有 sheet，抽取全量记录。

两段式设计（见 HISTORY.md「Agent 自由化原则」一节）：
- LLM（map_to_canonical）只负责「理解结构」——识别 entity_type + 输出 column_mapping
  （表头名 → canonical 字段名）；
- 本模块用该映射做「确定性批量抽取」——遍历全部 sheet，逐行抽全量数据，
  不占 LLM 上下文（大表/多 sheet 也能抽完）。
"""
from __future__ import annotations

from typing import Any


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


def _find_header_row(rows: list[list[str]], keys: list[str]) -> int | None:
    """找表头行：该行覆盖了 column_mapping 里至少 2 个表头名。"""
    for index, row in enumerate(rows[:10]):
        hits = sum(1 for cell in row if any(key and key in cell for key in keys))
        if hits >= 2:
            return index
    return None


def _is_code(value: str) -> bool:
    """物料/产品编码：含字母与数字、无中文、且不是明细/费用等章节标题。"""
    if not value:
        return False
    if any(token in value for token in ("明细", "费用", "合计", "材料用量")):
        return False
    return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)


def extract_bom_full(raw: bytes, filename: str) -> dict[str, Any]:
    """完整 BOM 抽取：每个产品 sheet 的材料行 + 型号(product_code) + 产品名(sheet 名)。

    BOM 结构（成品成本分析表）：
    - 每个产品 sheet 名 = 产品名；
    - 中部「材料明细」是材料行（物料编码/材料名称/用量/单位）；
    - 底部「成品成本」区块有「型号」列，列出各长度变体的型号码（W-H909...W-H915）。
    返回 records：每个 (sheet × 型号) 一条，product_code=型号、product_name=sheet 名、lines=材料行。
    """
    import io
    import os
    import tempfile
    from pathlib import Path

    from .business_catalog import _extract_bom_xlsx
    from openpyxl import load_workbook

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.write(raw)
    tmp.close()
    try:
        bom = _extract_bom_xlsx(Path(tmp.name))
    finally:
        os.unlink(tmp.name)

    sheet_lines: dict[str, list[dict[str, Any]]] = {}
    for line in (bom.get("bom_lines") or []):
        sn = str(line.get("sheet_name") or "")
        if not sn:
            continue
        sheet_lines.setdefault(sn, []).append({
            "material_code": str(line.get("material_code") or ""),
            "material_name": str(line.get("material_name") or line.get("material_code") or ""),
            "quantity": line.get("quantity"),
            "unit": str(line.get("unit") or ""),
        })

    sheet_codes: dict[str, list[str]] = {}
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            header_rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                header_rows.append([("" if c is None else str(c).strip()) for c in row[:20]])
            code_col = None
            for row in header_rows:
                if "型号" in row:
                    code_col = row.index("型号")
                    break
            if code_col is None:
                continue
            codes: list[str] = []
            seen_header = False
            for row in header_rows:
                if "型号" in row:
                    seen_header = True
                    continue
                if not seen_header:
                    continue
                val = row[code_col] if code_col < len(row) else ""
                if val and ("-" in val or "." in val) and any(ch.isdigit() for ch in val):
                    codes.append(val)
            if codes:
                sheet_codes[ws.title] = codes
    finally:
        wb.close()

    records: list[dict[str, Any]] = []
    for sheet_name, lines in sheet_lines.items():
        if not any(line.get("unit") for line in lines):
            continue  # 跳过无「单位」列的辅助 sheet（材料明细表/线材单价表）
        codes = sheet_codes.get(sheet_name) or [sheet_name]
        for code in codes:
            records.append({"product_code": code, "product_name": sheet_name, "lines": lines})
    return {"records": records, "sheet_count": len(sheet_lines), "total_codes": sum(len(sheet_codes.get(s, [sheet_name])) for s in sheet_lines)}


def extract_tabular_bulk(
    raw: bytes,
    filename: str,
    column_mapping: dict[str, str],
    *,
    entity_type: str = "bom",
    identity_field: str = "material_code",
    max_sheets: int = 100,
    max_rows_per_sheet: int = 200,
) -> dict[str, Any]:
    """用 column_mapping 遍历所有 sheet 抽取全量行。

    column_mapping 形如 {"物料编码": "material_code", "材料名称": "material_name",
    "用量": "quantity", "单位": "unit"}（表头名 → canonical 字段）。
    返回 {"records": [...], "sheet_count": N, "total_rows": M}。
    """
    from .file_sniff import sniff_format

    verdict = sniff_format(raw, filename)
    fmt = verdict.detected_format
    sheet_rows: list[tuple[str, list[list[str]]]] = []

    if fmt in {"xlsx", "xlsm"}:
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        for ws in wb.worksheets[:max_sheets]:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                if len(rows) >= max_rows_per_sheet:
                    break
                rows.append([_cell(c) for c in row[:30]])
            sheet_rows.append((ws.title, rows))
        wb.close()
    elif fmt == "xls":
        import xlrd

        wb = xlrd.open_workbook(file_contents=raw)
        for sh in wb.sheets()[:max_sheets]:
            rows = [[_cell(sh.cell_value(r, c)) for c in range(min(sh.ncols, 30))] for r in range(min(sh.nrows, max_rows_per_sheet))]
            sheet_rows.append((sh.name, rows))
    else:
        return {"records": [], "sheet_count": 0, "total_rows": 0, "error": f"unsupported format: {fmt}"}

    keys = list(column_mapping.keys())
    records: list[dict[str, Any]] = []
    total_rows = 0
    for sheet_name, rows in sheet_rows:
        header_idx = _find_header_row(rows, keys)
        if header_idx is None:
            continue
        headers = rows[header_idx]
        col_map: dict[int, str] = {}
        for col_index, header in enumerate(headers):
            for key, field in column_mapping.items():
                if key and key in header:
                    col_map[col_index] = field
                    break
        if identity_field not in col_map.values():
            continue
        lines: list[dict[str, Any]] = []
        for row in rows[header_idx + 1:]:
            line: dict[str, Any] = {}
            for col_index, field in col_map.items():
                if col_index < len(row) and row[col_index]:
                    line[field] = row[col_index]
            if not line.get(identity_field) or not _is_code(str(line.get(identity_field))):
                continue
            lines.append(line)
            total_rows += 1
        if lines:
            records.append({"product_name": sheet_name, "lines": lines})

    return {"records": records, "sheet_count": len(sheet_rows), "total_rows": total_rows}

"""tabular_extract.extract_bom_full 确定性回归测试。

覆盖真实「成品成本分析表」BOM 的多区块结构（材料明细 / 耗材包材 / 成品成本）：

- 材料明细块：用量 列 → quantity；
- 耗材包材块：用量/装箱数量 列 → quantity（回归「规格 16*18 被误当用量」的 bug）；
- 线材/纸箱用量留空 → quantity=None（长度/装箱按规格，不编造数值）；
- 型号 列 → product_code。
"""
from __future__ import annotations

import io

from openpyxl import Workbook

from yunpai_orchestrator.tabular_extract import extract_bom_full


def _bom_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "中性 2.1 灰色菱形铝壳高清线"

    # 材料明细块（header + 3 行）
    ws.append(["项目", "", "物料编码", "材料名称", "", "用量", "单位", "单价", "规格", "成本"])
    ws.append(["", "1", "XC008", "19+1 30#超导铜包钢", "", "", "M", "0.66", "1-2M", ""])  # 线材：用量留空
    ws.append(["", "2", "YA.A.01.0145003", "45P黑色插头料", "", 0.016, "PCS", "7.17", "1-10M", "0.11472"])
    ws.append(["", "3", "YA.G.10.002", "HDMI 防尘盖", "", 2, "PCS", "0.006", "", "0.012"])
    ws.append([])

    # 耗材包材块（header + 2 行）：关键回归点——规格列 vs 用量/装箱数量列
    ws.append(["耗材及包材費用明細", "", "物料编码", "包材名称", "", "规格", "", "单价", "规格", "用量/装箱数量", "成本"])
    ws.append(["", "1", "YA.G.02.071", "透明骨袋", "", "16*18", "个", "0.05", "1-1.5M", 1, "0.05"])
    ws.append(["", "2", "YA.G.04.008", "普通大纸箱", "", "55*40*29", "个", "4.67", "", "", "5M"])
    ws.append([])

    # 成品成本块：型号列 → product_code
    ws.append(["成品成本", "項目", "成品名称", "型号", "规格"])
    ws.append(["", "1", "中性 2.1 灰色菱形铝壳高清线", "W-H913", "5"])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_extract_bom_full_maps_blocks_and_model_column():
    res = extract_bom_full(_bom_xlsx(), "中性BOM.xlsx")
    records = res["records"]
    assert len(records) == 1
    rec = records[0]
    assert rec["product_code"] == "W-H913"

    by_code = {line["material_code"]: line for line in rec["lines"]}
    # 材料明细块：用量 列原样照抄
    assert by_code["YA.A.01.0145003"]["quantity"] == 0.016
    assert by_code["YA.G.10.002"]["quantity"] == 2
    # 线材：用量留空 → None（长度按规格，不编造数值）
    assert by_code["XC008"]["quantity"] is None
    # 耗材包材块：用量/装箱数量=1，而不是规格 16*18
    assert by_code["YA.G.02.071"]["quantity"] == 1
    assert by_code["YA.G.02.071"]["unit"] == ""
    # 纸箱：用量/装箱数量留空 → None
    assert by_code["YA.G.04.008"]["quantity"] is None

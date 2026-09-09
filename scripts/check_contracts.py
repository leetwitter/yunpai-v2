#!/usr/bin/env python3
"""契约加载自检（书二 §7.1）——改完 registry-manifests/ 后跑这一条命令。

V2 的 manifest 只有**一份**（``registry-manifests/``，``registry.py:101-106`` glob
``m*.json`` + ``local.json``），且没有 ``SOURCE_PROVENANCE.json``——所以旧仓库那套
「同步副本 + 更新溯源哈希」在这里**不存在**，需要的是等价的**加载/去重/对齐**校验：

硬校验（失败即退出码 1）：
  H1  manifest 能被 ``build_default_registry()`` 加载（重名工具 → ``registry.py:88``
      抛 ``duplicate tool``；非法 JSON Schema → ``registry.py:90-96`` 抛错）
  H2  ``workers.HANDLERS`` 的每个键都在 ``registry.specs`` 里（否则 ``registry.py:262-264``
      会**静默丢弃**该 handler）
  H3  随迁模块（M-INFRA 迁入的 10 个文件）都能 import（import 闭包没被改坏）
  H4  路由目录只含已绑定工具：``visible_tool_names()`` 不含
      DEPRECATED / INTENTIONALLY_UNBOUND / ORCHESTRATION_INTERNAL
  H5  manifest 之间无同名工具（给出比 ``duplicate tool`` 更具体的文件:行定位）

软校验（``--strict`` 时也算失败）：
  W1  声明了 ``review_gate`` 但 ``reviewer/rules.py`` 既无条目、也无法归一化 → Gate 漏挂
  W2  本地绑定且 ``side_effect != none``，但 ``review_gate`` 归一化为空 → 写工具无门

用法（仓库根）：
    .venv/Scripts/python scripts/check_contracts.py
    .venv/Scripts/python scripts/check_contracts.py --json
    .venv/Scripts/python scripts/check_contracts.py --strict
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

MANIFEST_DIR = REPO_ROOT / "registry-manifests"

#: M-INFRA 随迁模块（骨架或完整迁入）——破坏 import 闭包必须立刻可见。
MIGRATED_MODULES = (
    "m1_domain", "m2_local", "m3_local", "m4_purchase_local", "m4_supplier_local",
    "m4_tracking_local", "m5_work_local", "m3_store", "m4_store", "m4b_store",
)


def _duplicates_by_file() -> dict[str, list[str]]:
    """H5：扫描 manifest 原文，报告跨文件重名（不依赖加载器抛错）。"""
    owners: dict[str, list[str]] = {}
    for path in sorted(MANIFEST_DIR.glob("m*.json")) + sorted(MANIFEST_DIR.glob("local.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in data.get("tools", []):
            name = str(item.get("name") or "")
            if name:
                owners.setdefault(name, []).append(path.name)
    return {name: files for name, files in owners.items() if len(files) > 1}


def run() -> tuple[list[str], list[str], dict]:
    from yunpai_orchestrator import binding
    from yunpai_orchestrator.registry import build_default_registry
    from yunpai_orchestrator.reviewer import rules
    from yunpai_orchestrator.workers import HANDLERS

    errors: list[str] = []
    warnings: list[str] = []
    report: dict = {}

    # H1 加载（重名 / schema 非法都会在这里炸）
    registry = build_default_registry()
    specs = registry.specs
    report["tool_count"] = len(specs)
    report["handlers_count"] = len(HANDLERS)
    by_module: dict[str, int] = {}
    for spec in specs.values():
        by_module[spec.module] = by_module.get(spec.module, 0) + 1
    report["by_module"] = dict(sorted(by_module.items()))

    # H2 HANDLERS ⊆ specs
    orphan_handlers = sorted(set(HANDLERS) - set(specs))
    if orphan_handlers:
        errors.append(
            "H2 HANDLERS 中这些工具没有对应 manifest（registry.py:262-264 会静默丢弃）："
            + ", ".join(orphan_handlers)
        )

    # H3 随迁模块 import
    for name in MIGRATED_MODULES:
        try:
            importlib.import_module(f"yunpai_orchestrator.{name}")
        except Exception as exc:  # noqa: BLE001 —— 自检脚本要把失败原样报出
            errors.append(f"H3 随迁模块 import 失败：yunpai_orchestrator.{name} → {type(exc).__name__}: {exc}")

    # H4 路由目录只含已绑定工具
    visible = set(binding.visible_tool_names(registry))
    forbidden = (
        binding.DEPRECATED_TOOLS
        | binding.INTENTIONALLY_UNBOUND
        | binding.ORCHESTRATION_INTERNAL
    )
    leaked = sorted(visible & set(forbidden))
    if leaked:
        errors.append("H4 路由目录混入禁止路由的工具：" + ", ".join(leaked))

    # H5 跨文件重名
    dup = _duplicates_by_file()
    if dup:
        errors.append(
            "H5 manifest 跨文件重名：" + "; ".join(f"{name}（{', '.join(files)}）" for name, files in dup.items())
        )

    # W1/W2 Gate 覆盖
    gate_missing: list[str] = []
    write_no_gate: list[str] = []
    for name, spec in sorted(specs.items()):
        declared = str(getattr(spec, "review_gate", "") or "")
        resolved = rules.gate_type_for(name, spec)
        # ``review_gate=none`` 是「无门」的显式声明（rules.py:68），不算漏挂。
        if declared and declared != "none" and not resolved:
            gate_missing.append(f"{name}（review_gate={declared}）")
        side_effect = str(getattr(spec, "side_effect", "") or "none")
        if side_effect != "none" and not resolved and name in HANDLERS:
            write_no_gate.append(f"{name}（side_effect={side_effect}）")
    if gate_missing:
        warnings.append("W1 声明了 review_gate 但 rules 无对应门：" + ", ".join(gate_missing))
    if write_no_gate:
        warnings.append("W2 本地写工具无门（side_effect != none 且 gate 归一化为空）：" + ", ".join(write_no_gate))

    report["bindings"] = {k: len(v) for k, v in binding.describe(registry).items()}
    report["visible_tools"] = len(visible)
    return errors, warnings, report


def main() -> int:
    parser = argparse.ArgumentParser(description="契约加载自检（加载 / 去重 / 对齐）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="软校验（W1/W2）也算失败")
    args = parser.parse_args()

    errors, warnings, report = run()

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings, **report},
                         ensure_ascii=False, indent=2))
    else:
        print(f"manifest 目录：{MANIFEST_DIR}")
        print(f"工具总数：{report['tool_count']}（handler {report['handlers_count']}）")
        print("按模块：" + ", ".join(f"{k}={v}" for k, v in report["by_module"].items()))
        print("绑定面：" + ", ".join(f"{k}={v}" for k, v in report["bindings"].items()))
        print(f"路由目录可见工具：{report['visible_tools']}")
        for line in warnings:
            print(f"[WARN] {line}")
        for line in errors:
            print(f"[FAIL] {line}")
        print("OK：加载 / 去重 / 对齐全部通过" if not errors else "FAIL：见上方 [FAIL]")

    failed = bool(errors) or (args.strict and bool(warnings))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

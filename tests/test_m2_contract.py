"""M2 契约真实性 + Gate 回归（分片 M2；依据 rows-S3.md 与 R3 的 P1-9 修复）。

覆盖：
- ``run_bom_sop_workflow`` 的本地实现边界必须与 manifest 描述/输出契约一致
  （R3 契约收缩：本地为草稿占位，完整流水线只在 HTTP ``POST /api/run``）；
- **P1-9 修复锁定**：``matching`` 不再是硬编码 ``matched/1.0``、``workflow_sequence``
  与真实执行一致、``artifacts`` 恒空且如实声明；
- handler 读取的入参 / 返回的顶层字段都必须已在契约内（禁止契约外输入输出）；
- ``m2_local`` 不直连 canonical sqlite 表（rows-S3 第 1 行改造项）；
- 3 个 M2 写工具的审查门（rows-S3「需补审查」3 条）：manifest 声明 + RULES 条目 +
  真实结果触发 ``authorization`` 门 + 角色校验 + approve 落 authorized_steps；
- 6 个 M2 工具都是 BOUND_LOCAL 且进路由目录（不得 UNBOUND）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yunpai_orchestrator import m2_local
from yunpai_orchestrator.binding import BindingStatus, CatalogView, compute_bindings, visible_tool_names
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.reviewer.gates import GateError, apply_decision, authorize, make_gate
from yunpai_orchestrator.state import new_state_v2
from yunpai_orchestrator.workers import m2_bom

MANIFEST = Path(__file__).resolve().parents[1] / "registry-manifests/m2.json"

M2_LOCAL_TOOLS = (
    "run_bom_sop_workflow", "search_m2_bom_history", "generate_m2_bom_controlled",
    "onboard_m2_bom_template", "generate_m2_sop", "list_m2_runs", "get_m2_run",
)

M2_WRITE_TOOLS = ("generate_m2_bom_controlled", "onboard_m2_bom_template", "generate_m2_sop")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _tool(name: str) -> dict:
    return next(item for item in _manifest()["tools"] if item["name"] == name)


def _rule_file(tmp_path) -> str:
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"duplicate_policy": "reject", "default_uom": "PCS"}), encoding="utf-8")
    return str(p)


def _history_csv(tmp_path) -> str:
    p = tmp_path / "hist.csv"
    p.write_text("物料编码,材料名称,用量,单位\nW-001,线材,0.5,M\n", encoding="utf-8")
    return str(p)


def _unwrap_total(result: object) -> object:
    """registry.call 会套 normalize_contract_result 信封；逐层找 total。"""
    node = result
    for _ in range(4):
        if isinstance(node, dict) and "total" in node:
            return node["total"]
        node = node.get("data") if isinstance(node, dict) else None
    return None


# ---------- run_bom_sop_workflow：契约与实现边界（R3 收缩 + P1-9） ----------

def test_run_bom_sop_workflow_manifest_states_local_draft_boundary():
    """描述必须如实说明本地是草稿占位，完整流水线只在 HTTP 服务侧。"""
    spec = _tool("run_bom_sop_workflow")
    description = spec["description"]
    assert "草稿占位实现" in description
    assert "POST /api/run" in description
    assert "artifacts 恒为空" in description
    props = spec["output_schema"]["properties"]
    assert "本地草稿占位实现只返回" in props["sop_generation"]["description"]
    assert "恒为 {}" in props["artifacts"]["description"]
    assert "not_run" in props["matching"]["description"]


def test_run_bom_sop_workflow_declares_every_input_the_handler_reads():
    """handler 读取的字段必须都在 input_schema 声明（禁止契约外入参）。"""
    declared = set(_tool("run_bom_sop_workflow")["input_schema"]["properties"])
    handler_fields = {
        "product_profile", "requirement_text", "routing_steps", "rule_package_path",
        "history_bom_paths", "history_sop_paths", "template_confirmation", "customer_answers",
        "machine_hints", "station", "enable_bom_model", "enable_sop_model", "use_demo_sources",
        "bom_lines", "bom_items", "bom_version", "bom_effective_from", "bom_effective_to",
        "sop_version", "sop_effective_from", "sop_effective_to", "document_no",
        "bom_files", "sop_files",
    }
    missing = sorted(handler_fields - declared)
    assert not missing, f"input_schema 未声明 handler 读取的字段: {missing}"


@pytest.mark.asyncio
async def test_run_bom_sop_workflow_output_keys_are_declared(monkeypatch, tmp_path):
    """两条返回路径的顶层字段都必须落在 output_schema.properties 内。"""
    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    declared = set(_tool("run_bom_sop_workflow")["output_schema"]["properties"])

    draft = await m2_bom(
        {"product_profile": {"product_code": "P-1", "product_name": "样品"},
         "bom_lines": [{"material_code": "M-1", "material_name": "料", "quantity": 1, "uom": "PCS"}],
         "routing_steps": [{"operation_id": "OP-10", "station": "S1",
                            "required_equipment_codes": ["EQ-1"]}]},
        {"task_id": "T-OUT-1", "tenant_id": "T-A"})
    blocked = await m2_bom({"product_profile": {"product_name": "缺编码"}},
                           {"task_id": "T-OUT-2", "tenant_id": "T-A"})

    assert draft["status"] == "draft_created"
    assert blocked["status"] == "human_input_required" and blocked["code"] == "BLOCKED_INPUT"
    for result in (draft, blocked):
        undeclared = sorted(set(result) - declared)
        assert not undeclared, f"输出含未声明字段: {undeclared}"


@pytest.mark.asyncio
async def test_run_bom_sop_workflow_no_fake_match_and_honest_sequence(monkeypatch, tmp_path):
    """P1-9 修复锁定：不得伪造 matched/1.0；workflow_sequence 与真实执行一致。"""
    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    out = await m2_bom(
        {"product_profile": {"product_code": "P-1", "product_name": "样品"},
         "bom_lines": [{"material_code": "M-1", "quantity": 1, "uom": "PCS"}]},
        {"task_id": "T-P19", "tenant_id": "T-A"})
    assert out["matching"]["status"] == "not_run"
    assert "score" not in out["matching"], "本地占位实现不得返回相似度得分"
    assert out["matching"]["matched_by"] == []
    # 本地只做 validate_input + engineering_fact_validation（无历史检索/生成步骤）
    assert out["workflow_sequence"] == ["validate_input", "engineering_fact_validation"]
    assert out["artifacts"] == {}
    # 上传件路径才追加 parse_sources
    import base64

    xlsx = tmp_path / "bom.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["物料编码", "材料名称", "用量", "单位"])
    ws.append(["M-9", "料", 1, "PCS"])
    wb.save(xlsx)
    wb.close()
    payload = {"product_profile": {"product_code": "P-1", "product_name": "样品"},
               "bom_files": [{"filename": "bom.xlsx",
                              "content_b64": base64.b64encode(xlsx.read_bytes()).decode()}]}
    out2 = await m2_bom(payload, {"task_id": "T-P19B", "tenant_id": "T-A"})
    assert out2["workflow_sequence"] == ["validate_input", "parse_sources",
                                         "engineering_fact_validation"]


def test_m2_local_has_no_hardcoded_match_success_marker():
    """m2_local 侧不得出现写死的匹配成功标记（诚实空结果由实现保证）。"""
    source = Path(m2_local.__file__).read_text(encoding="utf-8")
    assert '"status": "matched"' not in source
    assert "'status': 'matched'" not in source
    assert '"score": 1.0' not in source


def test_canonical_bom_read_goes_through_v2_m0_read_port():
    """rows-S3 第 1 行改造项：禁止直连 canonical 表，必须走 M0 读口。"""
    source = Path(m2_local.__file__).read_text(encoding="utf-8")
    assert "canonical_entities" not in source and "canonical_entity_versions" not in source
    assert "list_entities" in source, "canonical 候选必须经 M0Store.list_entities 读口"


# ---------- search_m2_bom_history ----------

def test_search_history_declares_warnings_channel():
    """读口降级必须如实声明在契约内（不得静默吞掉）。"""
    props = _tool("search_m2_bom_history")["output_schema"]["properties"]
    assert "results" in props and "warnings" in props


@pytest.mark.asyncio
async def test_search_history_output_keys_are_declared(monkeypatch, tmp_path):
    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "absent.sqlite"))
    declared = set(_tool("search_m2_bom_history")["output_schema"]["properties"])
    out = await m2_local.search_m2_bom_history({"product_name": "任意"}, {"tenant_id": "T-A"})
    assert set(out) <= declared


# ---------- list_m2_runs / get_m2_run：artifact_paths ----------

@pytest.mark.asyncio
async def test_list_and_get_run_expose_artifact_paths_kind_map(monkeypatch, tmp_path):
    """artifact_paths 必须是真实登记的 kind → path 映射（前端直接用于 /api/m2/artifact）。"""
    db = str(tmp_path / "m2.sqlite")
    monkeypatch.setenv("YUNPAI_M2_DB", db)
    store = m2_local.M2RunStore(db)
    store.save_run(tenant_id="T-A", run_id="m2-art", tool="generate_m2_sop", status="done",
                   order_id="SO-1", product_code="P-1", product_name="样品", summary={"k": 1})
    store.add_artifact("T-A", "m2-art", "sop_docx", "runtime/m2-artifacts/SOP-1.docx")
    store.add_artifact("T-A", "m2-art", "bom_json", "runtime/m2-artifacts/bom.json")

    listed = await m2_local.m2_list_runs({"product_code": "P-1"}, {"tenant_id": "T-A"})
    item = listed["data"]["items"][0]
    assert item["artifact_paths"] == {
        "sop_docx": "runtime/m2-artifacts/SOP-1.docx",
        "bom_json": "runtime/m2-artifacts/bom.json",
    }
    got = await m2_local.m2_get_run({"run_id": "m2-art"}, {"tenant_id": "T-A"})
    run = got["data"]["run"]
    assert run["artifact_paths"] == item["artifact_paths"]
    assert {a["kind"] for a in run["artifacts"]} == {"sop_docx", "bom_json"}


@pytest.mark.asyncio
async def test_list_runs_without_artifacts_returns_empty_map(monkeypatch, tmp_path):
    """无制品时返回空映射，而不是缺字段或合成路径。"""
    db = str(tmp_path / "m2.sqlite")
    monkeypatch.setenv("YUNPAI_M2_DB", db)
    m2_local.M2RunStore(db).save_run(tenant_id="T-A", run_id="m2-none", tool="x", status="done")
    out = await m2_local.m2_list_runs({}, {"tenant_id": "T-A"})
    assert out["data"]["items"][0]["artifact_paths"] == {}


@pytest.mark.asyncio
async def test_list_m2_runs_output_passes_registry_contract(monkeypatch, tmp_path):
    """经 ToolRegistry.call 的 input/output schema 校验必须通过（含 artifact_paths）。"""
    db = str(tmp_path / "m2.sqlite")
    monkeypatch.setenv("YUNPAI_M2_DB", db)
    store = m2_local.M2RunStore(db)
    store.save_run(tenant_id="T-A", run_id="m2-reg", tool="x", status="done")
    store.add_artifact("T-A", "m2-reg", "bom_json", "runtime/m2-artifacts/bom.json")

    registry = build_default_registry()
    out = await registry.call("list_m2_runs", {"limit": 5},
                              {"tenant_id": "T-A", "task_id": "T-REG"})
    assert _unwrap_total(out) == 1


# ---------- generate_m2_sop：流程图边界 ----------

@pytest.mark.asyncio
async def test_generate_sop_reports_text_only_flowchart(monkeypatch, tmp_path):
    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    monkeypatch.setenv("YUNPAI_M2_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    out = await m2_local.generate_m2_sop(
        {"product_name": "中性线", "part_no": "W-1", "document_no": "SOP-80806-009"},
        {"task_id": "T-SOPX", "tenant_id": "T-A"})
    assert out["status"] == "generated"
    assert out["flowchart"]["status"] == "text_only"
    assert "不生成 PNG 流程图" in out["flowchart"]["note"]
    declared = set(_tool("generate_m2_sop")["output_schema"]["properties"])
    assert set(out) <= declared


# ---------- 3 条审查补齐（rows-S3「需补审查」） ----------

def test_m2_write_tools_declare_side_effect_and_authorization_gate():
    """manifest 必须声明写副作用与 authorization 门（合同默认门不再是 none）。"""
    registry = build_default_registry()
    for name in M2_WRITE_TOOLS:
        spec = registry.specs[name]
        assert spec.side_effect == "local_write", name
        assert spec.review_gate == "authorization", name
        assert rules.gate_type_for(name, spec) == "authorization", name


def test_m2_write_tool_results_open_authorization_gate():
    """三条 gate 断言：真实 handler 结果必须命中 RULES 并归一化为 authorization 门。"""
    findings = rules.evaluate("generate_m2_bom_controlled",
                              {"standard_bom": {"bom_lines": [], "note": "无依据"}})
    assert [f["gate"] for f in findings] == ["authorization"]
    findings = rules.evaluate("onboard_m2_bom_template", {"proposals": []})
    assert [f["gate"] for f in findings] == ["authorization"]
    findings = rules.evaluate("generate_m2_sop", {"status": "generated",
                                                  "artifacts": {"docx": "x.docx"}})
    assert [f["gate"] for f in findings] == ["authorization"]
    # 未产出结果（空 dict）不误开门
    assert rules.evaluate("generate_m2_bom_controlled", {}) == []
    assert rules.evaluate("generate_m2_sop", {"status": "failed"}) == []
    # 红线：任何路径都不得自动放行人工门
    assert rules.AUTO_APPROVE_ALLOWED is False


def test_m2_read_only_tools_stay_gateless():
    """只读工具不得无谓开写门（search/list/get）。"""
    registry = build_default_registry()
    for name in ("search_m2_bom_history", "list_m2_runs", "get_m2_run"):
        assert registry.specs[name].side_effect == "none"
        assert registry.specs[name].review_gate == "none"
        assert rules.evaluate(name, {"results": [], "data": {"items": []}}) == []


@pytest.mark.asyncio
async def test_onboard_result_opens_gate_then_approve_authorizes(monkeypatch, tmp_path):
    """端到端：落库 → 审查命中 authorization 门 → 角色校验 → approve 授权该工具。"""
    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    registry = build_default_registry()
    out = await registry.call("onboard_m2_bom_template",
                              {"rule_package_path": _rule_file(tmp_path),
                               "history_paths": [_history_csv(tmp_path)]},
                              {"tenant_id": "T-A", "task_id": "T-GATE"})
    findings = rules.evaluate("onboard_m2_bom_template", out,
                              registry.specs["onboard_m2_bom_template"])
    gate_finding = next((f for f in findings if f.get("gate")), None)
    assert gate_finding is not None and gate_finding["gate"] == "authorization"
    gate = make_gate(gate_finding["gate"], "onboard_m2_bom_template", gate_finding["reason"])
    with pytest.raises(GateError):
        authorize(gate, ["data-steward"])          # 角色不足 → 拒绝
    authorize(gate, ["operator"])
    updates = apply_decision(new_state_v2({"message": "m"}), gate,
                             {"decision": "approve", "actor": "op", "roles": ["operator"]})
    assert "onboard_m2_bom_template" in updates["authorized_steps"]


# ---------- 绑定面：6+1 个 M2 工具必须可见 ----------

def test_m2_tools_are_bound_local_and_visible_in_catalog():
    registry = build_default_registry()
    bindings = compute_bindings(registry)
    for name in M2_LOCAL_TOOLS:
        assert registry.specs.get(name) is not None, f"{name} 缺契约"
        assert bindings[name] == BindingStatus.BOUND_LOCAL, f"{name} 未绑定本地 handler"
    visible = set(visible_tool_names(registry))
    assert set(M2_LOCAL_TOOLS) <= visible
    assert set(M2_LOCAL_TOOLS) <= set(CatalogView(registry).specs)


# ---------- Skill 可达性（rows-S3 备注：onboard/list_runs 原先经 Skill 不可达） ----------

def test_m2_skill_operation_map_covers_onboard_and_runs():
    from yunpai_orchestrator.skills import M2_SKILL_OPERATION_MAP, build_default_skill_registry

    spec = build_default_skill_registry().specs["yunpai-m2-bom-sop"]
    assert M2_SKILL_OPERATION_MAP["onboard"] == "onboard_m2_bom_template"
    assert M2_SKILL_OPERATION_MAP["runs"] == "list_m2_runs"
    # operation map 与 SkillSpec.tools 同源（不再手写两份白名单）
    assert set(M2_SKILL_OPERATION_MAP.values()) <= set(spec.tools)
    assert set(spec.tools) == set(M2_SKILL_OPERATION_MAP.values())


@pytest.mark.asyncio
async def test_m2_skill_reaches_onboard_then_runs(monkeypatch, tmp_path):
    """经 Skill 走 operation=onboard / runs 必须真的能落库并读回。"""
    from yunpai_orchestrator.skills import build_default_skill_registry

    monkeypatch.setenv("YUNPAI_M2_DB", str(tmp_path / "m2.sqlite"))
    skills = build_default_skill_registry(build_default_registry())
    ctx = {"tenant_id": "T-A", "task_id": "T-SKILL"}
    onboarded = await skills.call(
        "yunpai-m2-bom-sop",
        {"operation": "onboard", "rule_package_path": _rule_file(tmp_path),
         "history_paths": [_history_csv(tmp_path)]},
        ctx)
    assert onboarded["invoked_tool"] == "onboard_m2_bom_template"
    assert onboarded["proposals"]
    runs = await skills.call("yunpai-m2-bom-sop", {"operation": "runs", "limit": 5}, ctx)
    assert runs["invoked_tool"] == "list_m2_runs"
    assert _unwrap_total(runs) == 1

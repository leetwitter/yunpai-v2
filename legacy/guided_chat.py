"""引导 AI 组织架构图对话（F-015 v3：模型定内容，代码做模板与落库）。

原则（用户裁定）：凡是能交给 AI 做的判断就交给 AI。组织架构的**内容**（部门
名单、人员归属、角色分配）全部由本地大模型（``QwenRouter.guide_chat``）产出
并维护；组织树的**绘制**由前端模板完成（本模块只给扁平数据）。本模块承担：

1. 首开把模型返回的三档规模建议部门名单（small/medium/large）交给前端渲染
   成三张组织架构图；
2. 校验模型输出的部门名单与人员分配（角色 code 白名单/名称非空）并暂存；
3. 模型判定「确认落地」时，把部门与绑定写库（人工确认 Gate）。

模型不可用时 fail-loud，绝不静默回退到写死规则。
"""
from __future__ import annotations

from typing import Any

from .identity import (
    DEFAULT_ROLE_SEEDS,
    PERMISSION_CATALOG,
    IdentityStore,
    normalize_dept_name,
)

SCALE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "small", "label": "小规模（30 人以内，老板直接管）"},
    {"value": "medium", "label": "中规模（30 ~ 200 人）"},
    {"value": "large", "label": "大规模（200 人以上，分工较细）"},
)

_ROLE_CODES = {role["role_code"] for role in DEFAULT_ROLE_SEEDS}
_ROLE_NAME = {role["role_code"]: role["name"] for role in DEFAULT_ROLE_SEEDS}


def _dept_org_id(name: str) -> str:
    return f"dept:{normalize_dept_name(name)}"


def _roster_view(roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"name": w.get("worker_name") or "", "skill": w.get("skill") or "",
         "dept": w.get("shift") or w.get("dept") or ""}
        for w in roster if isinstance(w, dict)
    ]


def sanitize_departments(raw: Any) -> list[str]:
    """部门名单去重/去空。"""
    seen: list[str] = []
    for item in (raw or []) if isinstance(raw, list) else []:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def sanitize_assignments(raw: Any) -> list[dict[str, Any]]:
    """人员分配：角色 code 白名单过滤，name 非空；保留 dept（组织单元）与
    manager（上级姓名，汇报关系，仅用于组织架构图渲染，不入库）。"""
    result: list[dict[str, Any]] = []
    for item in (raw or []) if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        roles = [str(r) for r in (item.get("roles") or []) if str(r) in _ROLE_CODES]
        if not name or not roles:
            continue
        result.append({
            "name": name,
            "roles": roles,
            "dept": str(item.get("dept") or "").strip(),
            "manager": str(item.get("manager") or "").strip(),
        })
    return result


def _resolve_user_id(roster: list[dict[str, Any]], name: str) -> tuple[str, dict[str, Any] | None]:
    for entry in roster:
        if isinstance(entry, dict) and entry.get("worker_name") == name:
            return str(entry.get("worker_code") or name), entry
    return name, None


def _model_unavailable(state: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "reply": f"本地引导模型不可用，请先启动并配置 QWEN_BASE_URL / QWEN_MODEL / QWEN_API_KEY"
                 f"（{detail}）。模型就绪后重试即可，当前方案未改变。",
        "options": [], "plan": None, "scale_departments": {},
        "state": state, "done": False, "applied": None,
    }


async def handle_message(store: IdentityStore, *, tenant_id: str, user_id: str,
                         message: str, roster: list[dict[str, Any]] | None = None,
                         state: dict[str, Any] | None = None,
                         router: Any = None) -> dict[str, Any]:
    """对话引导主入口（LLM 产出/维护扁平内容）。返回
    {reply, options, plan, scale_departments, state, done, applied}。"""
    roster = roster or []
    state = dict(state) if state else {}
    msg = str(message or "").strip()

    if router is None or not hasattr(router, "guide_chat"):
        return _model_unavailable(state, "未注入引导模型 router")

    decision = await router.guide_chat(
        scale=state.get("scale"),
        departments=list(state.get("departments") or []),
        assignments=[dict(a) for a in (state.get("assignments") or [])],
        roster=_roster_view(roster),
        message=msg,
        roles=[{"role_code": r["role_code"], "name": r["name"],
                "permissions": r["permissions"]} for r in DEFAULT_ROLE_SEEDS],
        permissions=[{"code": p["code"], "label": p["label"]} for p in PERMISSION_CATALOG],
    )
    if not decision.get("ok"):
        return _model_unavailable(state, decision.get("error") or decision.get("status") or "unknown")

    if decision.get("scale") in {"small", "medium", "large"}:
        state["scale"] = decision["scale"]

    departments = sanitize_departments(decision.get("departments"))
    assignments = sanitize_assignments(decision.get("assignments"))
    if departments or assignments:
        state["departments"] = departments
        state["assignments"] = assignments

    reply = str(decision.get("reply") or "")

    # 首开 / 还需先选规模：返回三档规模的建议部门名单（前端渲染三张图）
    if decision.get("needs_scale") or not state.get("scale"):
        scale_departments = {
            k: sanitize_departments(v)
            for k, v in (decision.get("scale_departments") or {}).items()
            if isinstance(v, list)
        }
        return {"reply": reply, "options": [dict(o) for o in SCALE_OPTIONS],
                "plan": None, "scale_departments": scale_departments,
                "role_names": dict(_ROLE_NAME),
                "state": state, "done": False, "applied": None}

    # 人工确认 Gate：模型判定用户明确要落地 → 才写库
    if decision.get("confirm"):
        applied = _apply(store, tenant_id=tenant_id, user_id=user_id,
                         roster=roster, departments=departments, assignments=assignments)
        return {"reply": reply, "options": [], "plan": None, "scale_departments": {},
                "state": None, "done": True, "applied": applied}

    plan = {
        "departments": departments,
        "assignments": assignments,
        "role_names": dict(_ROLE_NAME),
    }
    return {
        "reply": reply,
        "options": [{"value": "就这样", "label": "就这样，落地"}],
        "plan": plan, "scale_departments": {},
        "role_names": dict(_ROLE_NAME),
        "state": state, "done": False, "applied": None,
    }


def _apply(store: IdentityStore, *, tenant_id: str, user_id: str,
           roster: list[dict[str, Any]], departments: list[str],
           assignments: list[dict[str, Any]]) -> dict[str, Any]:
    """人工确认 Gate：把部门与绑定写库（引导建的结构标 manual，派生不覆盖）。"""
    all_depts = list(departments)
    for item in assignments:
        if item.get("dept") and item["dept"] not in all_depts:
            all_depts.append(item["dept"])
    store.upsert_org(tenant_id=tenant_id, org_id="company", name="公司",
                     org_type="company", source="manual")
    for dept in all_depts:
        store.upsert_org(tenant_id=tenant_id, org_id=_dept_org_id(dept), name=dept,
                         parent_id="company", org_type="dept", source="manual")
    bindings: list[dict[str, Any]] = []
    for item in assignments:
        user_code, entry = _resolve_user_id(roster, item["name"])
        bindings.append({
            "user_id": user_code, "role_codes": item["roles"],
            "org_id": _dept_org_id(item["dept"]) if item.get("dept") else None,
            "skill": (entry or {}).get("skill"),
        })
    if bindings:
        store.bind_users_bulk(tenant_id=tenant_id, bindings=bindings)
    return {"departments": len(all_depts), "bindings": len(bindings),
            "departments_list": all_depts,
            "assignments": [{"user_id": b["user_id"], "role_codes": b["role_codes"],
                             "org_id": b["org_id"]} for b in bindings]}

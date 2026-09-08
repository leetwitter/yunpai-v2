"""书二 §2：RunState v2 基础行为。"""
from yunpai_orchestrator.state import new_state_v2, public_state


def test_new_state_defaults():
    s = new_state_v2({"message": "hi"})
    assert s["run_id"].startswith("run-") and s["task_id"].startswith("task-")
    assert s["thread_id"].startswith("thread-")
    assert s["status"] == "queued" and s["route"] == "chat"
    assert s["messages"] == [] and s["retry_counts"] == {}
    # 旧字段兼容（随迁组件依赖）
    assert "request" in s and "outputs" in s and "steps" in s
    assert "pending_gate" in s and "approvals" in s


def test_new_state_reads_attachments_and_tenant():
    s = new_state_v2({"message": "m", "attachments": [{"filename": "a.xlsx"}], "tenant_id": "t1"})
    assert s["attachments"] == [{"filename": "a.xlsx"}]
    assert s["tenant_id"] == "t1"


def test_public_state_masks_content_b64():
    state = new_state_v2({"message": "m"})
    state["outputs"] = {"x": {"content_b64": "A" * 200, "data": {"ok": 1}}}
    view = public_state(state)
    assert view["outputs"]["x"]["content_b64"] == "[omitted]"
    assert view["outputs"]["x"]["data"] == {"ok": 1}
    assert "request" not in view  # 原始请求（含附件 b64）不进对外视图

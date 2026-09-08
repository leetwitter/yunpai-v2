"""权限清单目录（F-015 引导的静态底表）。

仅提供权限/角色目录的只读列举（供引导模型与前端渲染参考），**不含任何
岗位→角色映射或部门模板**——这类判断已全部交由 ``guided_chat`` 调用本地
大模型完成（用户裁定：能让 AI 做的判断就交给 AI）。
"""
from __future__ import annotations

from typing import Any

from .identity import DEFAULT_ROLE_SEEDS, PERMISSION_CATALOG


def catalog_payload() -> dict[str, Any]:
    """权限清单 + 种子角色（引导AI 与前端共用；静态只读，不涉敏感数据）。"""
    return {
        "permissions": [dict(item) for item in PERMISSION_CATALOG],
        "roles": [dict(role) for role in DEFAULT_ROLE_SEEDS],
        "note": "查看类权限（order.view/report.view/worker.view）附数据范围 self|dept|tenant；"
                "v1 执行层按 tenant 隔离，dept 过滤待组织树链路稳定后启用",
    }

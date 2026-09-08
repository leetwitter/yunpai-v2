from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# 任务分级（设计文稿 §7.1）：L0 确定性 / L1 27B / L2 27B→DeepSeek / L3 DeepSeek。
LEVEL_L1 = "L1"
LEVEL_L2 = "L2"
LEVEL_L3 = "L3"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DeepSeekConfig:
    enabled: bool = False
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    timeout_s: float = 90.0
    daily_limit: int = 200

    @classmethod
    def from_env(cls) -> "DeepSeekConfig":
        return cls(
            enabled=_env_bool("DEEPSEEK_ENABLED", False),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            timeout_s=float(os.getenv("DEEPSEEK_TIMEOUT_S", "90")),
            daily_limit=int(os.getenv("DEEPSEEK_DAILY_LIMIT", "200")),
        )

    def public(self) -> dict[str, Any]:
        return {"provider": "deepseek", "model": self.model, "base_url": self.base_url,
                "enabled": self.enabled, "configured": bool(self.api_key)}


class TaskRouter:
    """27B → DeepSeek → BLOCKED 三级路由（确定性决策；真实调用由 QwenRouter/HTTP 完成）。"""

    def __init__(self, config: DeepSeekConfig | None = None) -> None:
        self.config = config or DeepSeekConfig.from_env()

    def route(self, level: str, *, qwen_ok: bool | None = None) -> dict[str, Any]:
        """返回下一步执行策略。qwen_ok=None 表示未尝试 27B（L3 直通 DeepSeek）。"""
        level = level.upper()
        if level == LEVEL_L1:
            return {"target": "qwen", "reason": "L1 轻量任务由 27B 本地执行，失败即确定性回退"}
        if level == LEVEL_L2:
            if qwen_ok:
                return {"target": "qwen", "reason": "27B 成功"}
            if self.config.enabled and self.config.api_key:
                return {"target": "deepseek", "reason": "27B 失败，转 DeepSeek 兜底"}
            return {"target": "blocked", "reason": "27B 失败且 DeepSeek 未开启"}
        if level == LEVEL_L3:
            if self.config.enabled and self.config.api_key:
                return {"target": "deepseek", "reason": "L3 hard 任务直接 DeepSeek（结果仍须人工 Gate）"}
            return {"target": "blocked", "reason": "L3 任务需要 DeepSeek 增强模型，当前未开启"}
        return {"target": "blocked", "reason": f"未知任务级别: {level}"}

"""全系统唯一配置入口（书二 §1.3，治痛点 9：配置三处打架）。

规则：
- 默认值只允许在本文件定义；任何模块不得再写第二份默认值。
- 环境变量名以此处为准；``.env.example`` 的键必须与这里的 os.getenv 键一一对应
  （tests/test_config.py 强制校验）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMConfig:
    # 联调默认：本机 27B（llama.cpp OpenAI-compatible）；生产切 GB10 35B。
    base_url: str = _env("QWEN_BASE_URL", "http://127.0.0.1:8088/v1")
    model: str = _env("QWEN_MODEL", "qwen3.8-27b")
    api_key: str = _env("QWEN_API_KEY", "")
    timeout_s: float = float(_env("QWEN_TIMEOUT_S", "45"))
    enabled: bool = _env_bool("QWEN_ROUTER_ENABLED", True)


@dataclass(frozen=True)
class TransportConfig:
    tool_transport: str = _env("YUNPAI_TOOL_TRANSPORT", "local")  # local | http
    http_modules: str = _env("YUNPAI_HTTP_MODULES", "m0,m1,m2,m3,m4,m5")
    env: str = _env("YUNPAI_ENV", "sandbox")  # production 护栏沿用 registry.py
    local_m4: bool = _env_bool("YUNPAI_LOCAL_M4", False)
    module_urls: dict = field(default_factory=lambda: {
        m: _env(f"{m.upper()}_URL", "") for m in ("m0", "m1", "m2", "m3", "m4", "m5")
    })


@dataclass(frozen=True)
class StorageConfig:
    runtime_dir: str = _env("YUNPAI_RUNTIME_DIR", "runtime")
    run_db: str = _env("YUNPAI_RUN_DB", "runtime/yunpai-runs.sqlite")
    evolution_db: str = _env("YUNPAI_EVOLUTION_DB", "runtime/yunpai-evolution.sqlite")
    identity_db: str = _env("YUNPAI_IDENTITY_DB", "runtime/yunpai-identity.sqlite")
    manifest_dir: str = _env("YUNPAI_MANIFEST_DIR", "registry-manifests")


@dataclass(frozen=True)
class OrchestratorConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    evolution_enabled: bool = _env_bool("YUNPAI_EVOLUTION_ENABLED", True)
    redline_interval_hours: int = int(_env("YUNPAI_REDLINE_INTERVAL_HOURS", "24"))
    max_step_retries: int = int(_env("YUNPAI_MAX_STEP_RETRIES", "2"))
    knowledge_top_k: int = int(_env("YUNPAI_KNOWLEDGE_TOP_K", "5"))
    require_trusted_principal: bool = _env_bool("YUNPAI_REQUIRE_TRUSTED_PRINCIPAL", False)


#: 本文件声明的全部环境变量键（.env.example 必须与其一致，测试校验用）。
ENV_KEYS: tuple[str, ...] = (
    "QWEN_BASE_URL", "QWEN_MODEL", "QWEN_API_KEY", "QWEN_TIMEOUT_S", "QWEN_ROUTER_ENABLED",
    "YUNPAI_TOOL_TRANSPORT", "YUNPAI_HTTP_MODULES", "YUNPAI_ENV", "YUNPAI_LOCAL_M4",
    "M0_URL", "M1_URL", "M2_URL", "M3_URL", "M4_URL", "M5_URL",
    "YUNPAI_RUNTIME_DIR", "YUNPAI_RUN_DB", "YUNPAI_EVOLUTION_DB", "YUNPAI_IDENTITY_DB",
    "YUNPAI_MANIFEST_DIR", "YUNPAI_EVOLUTION_ENABLED", "YUNPAI_REDLINE_INTERVAL_HOURS",
    "YUNPAI_MAX_STEP_RETRIES", "YUNPAI_KNOWLEDGE_TOP_K", "YUNPAI_REQUIRE_TRUSTED_PRINCIPAL",
)


def load() -> OrchestratorConfig:
    return OrchestratorConfig()

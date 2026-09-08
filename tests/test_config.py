"""书二 §1.3：配置单一来源——.env.example 与 config.ENV_KEYS 双向同步。"""
from pathlib import Path

from yunpai_orchestrator.config import ENV_KEYS, OrchestratorConfig, load

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_matches_config_keys():
    env_file = REPO_ROOT / ".env.example"
    assert env_file.exists(), ".env.example 必须存在"
    text = env_file.read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    missing = set(ENV_KEYS) - declared
    extra = declared - set(ENV_KEYS)
    assert not missing, f".env.example 缺键: {sorted(missing)}"
    assert not extra, f".env.example 多出未在 config 声明的键: {sorted(extra)}"


def test_config_loadable_and_frozen():
    cfg = load()
    assert isinstance(cfg, OrchestratorConfig)
    assert cfg.max_step_retries >= 0
    assert cfg.knowledge_top_k >= 1


def test_single_source_no_second_llm_default(monkeypatch):
    """llm.QwenConfig.from_env 默认值必须与 config.LLMConfig 一致（治痛点 9）。"""
    from yunpai_orchestrator.config import LLMConfig
    from yunpai_orchestrator.llm import QwenConfig
    for key in ("QWEN_BASE_URL", "QWEN_MODEL"):
        monkeypatch.delenv(key, raising=False)
    q = QwenConfig.from_env()
    c = LLMConfig()
    assert q.base_url == c.base_url, "QWEN_BASE_URL 默认值两处不一致"
    assert q.model == c.model, "QWEN_MODEL 默认值两处不一致"

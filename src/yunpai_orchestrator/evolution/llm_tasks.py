from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..llm import QwenConfig

logger = logging.getLogger("yunpai.evolution.llm")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", flags=re.IGNORECASE | re.DOTALL)


class EvolutionLLM:
    """演化层 LLM 任务客户端（OpenAI-compatible，本机 27B / 远端 vLLM）。

    只做四类短任务：分类、映射、归一、归纳。全部 temperature=0、JSON 输出、
    enable_thinking=false（本机 qwen3.8-27b 是思维链模型，不关会空 content）。
    任何失败回退确定性结果，绝不静默伪造。
    """

    def __init__(self, config: QwenConfig | None = None) -> None:
        self.config = config or QwenConfig.from_env()
        self.enabled = bool(self.config.enabled and self.config.api_key)
        self.capture: list[dict[str, Any]] = []  # 对话捕获：每次 complete 的完整 prompt + 原始回复

    def public(self) -> dict[str, Any]:
        return {**self.config.public(), "evolution_llm_enabled": self.enabled}

    async def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                       timeout_s: float | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.enabled:
            return {"ok": False, "status": "disabled", "error": "27B 未配置"}
        import httpx

        # DeepSeek 的 response_format=json_object 要求 prompt 含「json」字样，否则 400。
        if "json" not in (system + user).lower():
            system = system + " 只输出 JSON 对象。"

        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_object"},
        }
        token = self.config.api_key or "local"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=timeout_s or max(self.config.timeout_s, 180.0), trust_env=False) as client:
                response = await client.post(f"{self.config.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
            content = self._content(payload)
            data = self._parse_json(content)
            elapsed = round(time.perf_counter() - started, 1)
            logger.info("evolution.llm ok model=%s latency_ms=%s", self.config.model, int(elapsed * 1000))
            self.capture.append({"system": system, "user": user, "ok": True,
                                 "raw_content": content, "data": data, "latency_s": elapsed})
            return {"ok": True, "status": "ok", "data": data, "latency_s": elapsed}
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 1)
            logger.warning("evolution.llm error model=%s latency_s=%s error=%s", self.config.model, elapsed, exc)
            self.capture.append({"system": system, "user": user, "ok": False,
                                 "error": str(exc), "latency_s": elapsed})
            return {"ok": False, "status": "error", "error": str(exc), "latency_s": elapsed}

    @staticmethod
    def _content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("no choices in response")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content (可能未关 enable_thinking)")
        return content

    @staticmethod
    def _parse_json(content: str) -> Any:
        cleaned = _FENCE_RE.sub("", content.strip()).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                raise ValueError("response is not JSON")
            return json.loads(match.group(0))

    # ------------------------------------------------------------------
    # P1：BOM 行分类 + 规格规范化（批量，降 latency）
    # ------------------------------------------------------------------
    async def categorize_bom_lines(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not lines:
            return []
        payload = [{"name": str(line.get("name") or line.get("material_name") or ""),
                    "spec": str(line.get("specification") or "")} for line in lines]
        system = (
            "你是制造业 BOM 行分类器。把每行物料归入：线材/端子/外壳/辅料/包材/连接器/电路/其他；"
            "并从名称+规格提取 spec_normalized（材质/镀层/线规/触点数/线长等，只提取能确定的，不要编）。"
            "只输出一个 JSON 对象 {\"lines\":[{\"category\":\"...\",\"spec_normalized\":{...}}, ...]}，"
            "顺序与输入一致，条数相同。"
        )
        result = await self.complete(system=system, user=json.dumps({"lines": payload}, ensure_ascii=False), max_tokens=2048)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return [{"category": "", "spec_normalized": {}, "llm_status": result.get("status", "error")} for _ in lines]
        items = result["data"].get("lines") if isinstance(result["data"].get("lines"), list) else []
        out: list[dict[str, Any]] = []
        for index in range(len(lines)):
            item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
            out.append({"category": str(item.get("category") or ""),
                        "spec_normalized": item.get("spec_normalized") if isinstance(item.get("spec_normalized"), dict) else {},
                        "llm_status": "ok"})
        return out

    # ------------------------------------------------------------------
    # P2：工序名归一（批量）
    # ------------------------------------------------------------------
    async def normalize_operation_names(self, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        system = (
            "你是制造工序名归一器。把口语化工序名归一到标准名称（如 焊接端子/端子焊接→端子焊接、"
            "外观检查/目检→外观检查）。只输出 JSON {\"names\":[{\"canonical_name\":\"...\",\"confidence\":0.0~1.0}, ...]}，顺序与输入一致。"
        )
        result = await self.complete(system=system, user=json.dumps({"names": names}, ensure_ascii=False), max_tokens=1024)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return [{"canonical_name": name, "confidence": 0.0, "llm_status": result.get("status", "error")} for name in names]
        items = result["data"].get("names") if isinstance(result["data"].get("names"), list) else []
        out = []
        for index, name in enumerate(names):
            item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
            out.append({"canonical_name": str(item.get("canonical_name") or name),
                        "confidence": _clamp(float(item.get("confidence", 0.0))),
                        "llm_status": "ok"})
        return out

    # ------------------------------------------------------------------
    # P5/P6：图纸/承认书碎片 → 特征卡
    # ------------------------------------------------------------------
    async def summarize_drawing(self, text: str) -> dict[str, Any]:
        if not text.strip():
            return {}
        system = (
            "你是工程图/承认书特征归纳器。从文字碎片提取几何与物理特征（长度/材质/镀层/连接器类型/"
            "孔位数量/公差/标题栏图号）。只输出 JSON 对象，字段尽量用英文 key；拿不准的字段不要编，"
            "无法确定的就不要输出该字段。"
        )
        result = await self.complete(system=system, user=text[:4000], max_tokens=1024)
        if result.get("ok") and isinstance(result.get("data"), dict):
            return result["data"]
        return {}

    # ------------------------------------------------------------------
    # P7：身份对齐（规格签名 → 产品族）
    # ------------------------------------------------------------------
    async def resolve_product(self, text: str, known_codes: list[str]) -> dict[str, Any]:
        system = (
            "你是产品身份对齐器。判断给定描述与已知型号是否同一产品/同族。"
            "只输出 JSON {\"same_family\":bool,\"product_code\":\"\",\"confidence\":0.0~1.0,\"reason\":\"\"}。"
            "拿不准时 same_family=false、confidence 低于 0.7。"
        )
        result = await self.complete(
            system=system,
            user=json.dumps({"text": text, "known_codes": known_codes}, ensure_ascii=False),
            max_tokens=512,
        )
        if result.get("ok") and isinstance(result.get("data"), dict):
            return result["data"]
        return {"same_family": False, "product_code": "", "confidence": 0.0, "reason": "27B 不可用"}

    # ------------------------------------------------------------------
    # 对话：自由文本（不强制 JSON）
    # ------------------------------------------------------------------
    async def chat(self, *, system: str, user: str, max_tokens: int = 512,
                   timeout_s: float | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.enabled:
            return {"ok": False, "status": "disabled", "error": "模型未配置"}
        import httpx

        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        token = self.config.api_key or "local"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=timeout_s or max(self.config.timeout_s, 180.0), trust_env=False) as client:
                response = await client.post(f"{self.config.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
            content = self._content(payload)
            elapsed = round(time.perf_counter() - started, 1)
            self.capture.append({"system": system, "user": user, "ok": True,
                                 "raw_content": content, "latency_s": elapsed})
            return {"ok": True, "status": "ok", "reply": content, "latency_s": elapsed}
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 1)
            self.capture.append({"system": system, "user": user, "ok": False,
                                 "error": str(exc), "latency_s": elapsed})
            return {"ok": False, "status": "error", "error": str(exc), "latency_s": elapsed}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
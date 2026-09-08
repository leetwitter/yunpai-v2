from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .registry import ToolRegistry, build_runtime_registry


class MCPServer:
    """无供应商锁定的 MCP facade；可被 FastMCP/HTTP host 包装，也可直接 stdio 运行。"""
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_runtime_registry()

    def list_tools(self) -> dict[str, Any]: return {"tools": self.registry.mcp_tools()}

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            result = await self.registry.call(name, arguments, context or {"run_id": "mcp", "task_id": "mcp"})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result, "isError": False}
        except Exception as exc:
            error = {"code": "TOOL_CALL_FAILED", "tool": name, "message": str(exc)}
            return {"content": [{"type": "text", "text": json.dumps(error, ensure_ascii=False)}], "structuredContent": error, "isError": True}

    async def serve_stdio(self) -> None:
        for line in sys.stdin:
            if not line.strip(): continue
            message = json.loads(line)
            method, req_id = message.get("method"), message.get("id")
            if method == "initialize": result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "yunpai-mcp", "version": "0.2.0"}}
            elif method == "tools/list": result = self.list_tools()
            elif method == "tools/call": result = await self.call_tool(message["params"]["name"], message["params"].get("arguments", {}))
            else: result = {"error": {"code": -32601, "message": f"method not found: {method}"}}
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False), flush=True)


def main() -> None: asyncio.run(MCPServer().serve_stdio())


if __name__ == "__main__":
    main()

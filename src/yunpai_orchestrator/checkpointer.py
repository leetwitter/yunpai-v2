"""Checkpointer 装配（书二 §3.3）：执行状态官方 saver + runs 表业务视图双写。

实现注记：图以**同步** SqliteSaver + 同步 invoke/stream（API 层经线程池调用）运行。
曾试 AsyncSqliteSaver：其构造绑定首个事件循环，TestClient 每请求新建循环直接崩
（"Lock bound to a different event loop"）；同步方案跨循环稳定且与 SQLite 单写者
语义一致。runs.sqlite 只增 langgraph_checkpoints 等表，不动 runs 表。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def memory_checkpointer() -> Any:
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


def sqlite_checkpointer(path: str | Path) -> Any:
    """AsyncSqliteSaver：必须在运行中的事件循环内构造（api/app.py 的专属循环负责）。"""
    import aiosqlite

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver(aiosqlite.connect(str(path)))


def default_checkpointer(path: str | Path | None = None) -> Any:
    if path is None:
        return memory_checkpointer()
    return sqlite_checkpointer(path)

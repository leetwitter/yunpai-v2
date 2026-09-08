"""SQLite 底座统一约定：WAL + busy_timeout + 用后即关的事务上下文。

此前各存储用 ``with self._connect() as db:``——sqlite3 的连接上下文管理器
只负责事务提交/回滚，**不关闭连接**（38 处调用、0 处显式 close，连接对象
全靠 GC 兜底）；也未设置 busy_timeout/WAL，并发读写靠默认 5 秒隐式超时。

统一到这里：
- ``connect_sqlite``：每连接设 busy_timeout（默认 30s），可选强制外键；
- ``enable_wal``：初始化时切 WAL（对库文件持久，读写不再互斥）；
- ``transactional``：commit-on-success / rollback-on-error / **必定 close**。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect_sqlite(path: str | Path, *, foreign_keys: bool = False,
                   busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """打开一个设置好底座约定的连接（调用方负责关闭，或用 transactional）。"""
    db = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
    db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if foreign_keys:
        db.execute("PRAGMA foreign_keys=ON")
    return db


def enable_wal(db: sqlite3.Connection) -> None:
    """切 WAL 日志模式（对库文件持久化；只需在初始化时执行一次）。"""
    db.execute("PRAGMA journal_mode=WAL")


@contextmanager
def transactional(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务上下文：成功提交、异常回滚、无论如何关闭连接。"""
    try:
        with db:
            yield db
    finally:
        db.close()

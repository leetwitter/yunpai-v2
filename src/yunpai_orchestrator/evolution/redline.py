from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

# 只读红线：知识/快捷操作/档案是运行库数据，绝不修改这些路径。
_PROTECTED_DIRS = ("workflows", "skills", "src", "migrations", "tools", "registry", "scripts")
_PROTECTED_FILENAMES = {"AGENTS.md", "pyproject.toml", "langgraph.json"}


def repo_root() -> Path:
    """推导开发仓库根目录（可被 YUNPAI_REPO_ROOT 覆盖）。"""
    override = os.getenv("YUNPAI_REPO_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    # .../src/yunpai_orchestrator/evolution/redline.py -> 仓库根 = parents[3]
    return Path(__file__).resolve().parents[3]


def protected_paths(root: Path | None = None) -> list[Path]:
    root = (root or repo_root()).resolve()
    paths: list[Path] = []
    for name in _PROTECTED_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            paths.append(candidate)
    for dirname in _PROTECTED_DIRS:
        directory = root / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and ".venv" not in path.parts and "__pycache__" not in path.parts:
                paths.append(path)
    # 各级 AGENTS.md（当前仓库只保护根级，保留扩展能力）。
    return paths


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path | None = None) -> dict[str, str]:
    """返回受保护路径的相对路径 -> sha256。"""
    root = (root or repo_root()).resolve()
    result: dict[str, str] = {}
    for path in protected_paths(root):
        try:
            result[path.relative_to(root).as_posix()] = _hash_file(path)
        except OSError:
            continue
    return result


def verify(before: dict[str, str], *, root: Path | None = None) -> dict[str, Any]:
    """对比快照，返回发生变化的受保护文件清单。空 changes = 红线未触碰。"""
    after = snapshot(root)
    changed = [rel for rel in after if before.get(rel) != after[rel]]
    removed = [rel for rel in before if rel not in after]
    added = [rel for rel in after if rel not in before]
    return {"changed": sorted(changed), "removed": sorted(removed), "added": sorted(added), "clean": not (changed or removed or added)}


class RedlineMonitor:
    """启动自检 + 周期监测用。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or repo_root()).resolve()
        self.baseline = snapshot(self.root)

    def check(self) -> dict[str, Any]:
        return verify(self.baseline, root=self.root)

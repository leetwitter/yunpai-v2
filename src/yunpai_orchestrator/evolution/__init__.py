"""知识自进化与产品理解（EV-1 治理底座起）。

本包实现设计文稿《知识自进化与产品理解-功能设计文稿-20260908》的演进层：
- ``repository``：10 张表（ok_* 8 张经验沉淀 + pu_* 2 张产品理解）+ 候选状态机
- ``signals``：四类信号观察（重复操作 / 经常出错 / 人工纠正 / 成功偏好）
- ``gate``：晋升门禁（3 独立任务 或 强证据）
- ``injection``：确定性结构化检索 + Planner 注入
- ``redline``：编排 / AGENTS.md / src 只读红线监测
- ``api``：治理台与决策 REST 路由

知识/快捷操作/产品档案全部是运行库数据，绝不修改编排文件或源码。
"""

from .repository import EvolutionRepository
from .signals import observe_run

__all__ = ["EvolutionRepository", "observe_run"]

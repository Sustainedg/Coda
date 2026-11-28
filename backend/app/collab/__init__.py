"""协作域：多智能体工单协作（设计见 ``docs/计划任务/collaboration-design.md``）。

对外只暴露两样东西：``app.api`` 挂载的 ``api.router``，以及启动恢复用的
``recover_interrupted`` / ``backfill_run_graphs``。其余模块属于内部实现。
"""
from .store import backfill_run_graphs, recover_interrupted  # noqa: F401

__all__ = ["recover_interrupted", "backfill_run_graphs"]

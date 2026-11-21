"""KV 缓存离线碎片整理规划器。"""

from .model import ActiveRequest, Block, ExecutionResult, Hole, Move, PausePoint, RelocationPlan
from .memory import MemoryMap
from .planner import DefragPlanner, NoFeasibleSpaceError
from .executor import CancellationToken, MoveError, PlanExecutor

__all__ = [
    "ActiveRequest",
    "Block",
    "CancellationToken",
    "DefragPlanner",
    "ExecutionResult",
    "Hole",
    "MemoryMap",
    "Move",
    "MoveError",
    "NoFeasibleSpaceError",
    "PausePoint",
    "PlanExecutor",
    "RelocationPlan",
]

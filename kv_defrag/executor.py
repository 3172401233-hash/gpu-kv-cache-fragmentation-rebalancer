"""搬迁计划的模拟执行器。

执行器负责把离线计划逐步应用到 :class:`MemoryMap`，并提供三类
生产环境需要的控制点：

* ``move_fn``：模拟真实显存拷贝，抛出异常即视为本次搬迁失败；
* ``CancellationToken``：任意两条搬迁之间请求暂停，已完成的拷贝
  保留在原位，后续可从断点继续；
* 失败记忆：拷贝失败的 ``(块, 目标地址)`` 会回写给规划器，重新
  规划时不会重复尝试同一组合。
"""

from typing import Callable, List, Optional

from .memory import MemoryMap
from .model import ExecutionResult, Move, PausePoint, RelocationPlan


class MoveError(RuntimeError):
    """单条搬迁指令在执行期失败。"""


class CancellationToken:
    """协作式取消令牌，可在暂停后复用继续执行。"""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def reset(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled


MoveFn = Callable[[Move], None]


class PlanExecutor:
    """按计划顺序应用搬迁，并维护可恢复的暂停点。"""

    def __init__(
        self,
        memory: MemoryMap,
        move_fn: Optional[MoveFn] = None,
        token: Optional[CancellationToken] = None,
        failure_recorder: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.memory = memory
        self.move_fn = move_fn or self._default_move_fn
        self.token = token or CancellationToken()
        self.failure_recorder = failure_recorder

    @staticmethod
    def _default_move_fn(move: Move) -> None:
        return None

    def execute(
        self,
        plan: RelocationPlan,
        start_index: int = 0,
    ) -> ExecutionResult:
        if not plan.feasible:
            return ExecutionResult(
                status="rejected",
                error=plan.reason or "plan_not_feasible",
            )

        completed: List[Move] = []
        pause_points: List[PausePoint] = list(
            plan.pause_points[: start_index + 1]
        )

        for index in range(start_index, plan.move_count):
            if self.token.cancelled:
                return ExecutionResult(
                    status="cancelled",
                    completed_moves=completed,
                    pause_points=pause_points,
                )
            move = plan.moves[index]
            try:
                self.move_fn(move)
            except Exception as error:  # 拷贝失败：布局不推进，计划中断
                error = MoveError(str(error))
                if self.failure_recorder is not None:
                    self.failure_recorder(move.block_id, move.target_address)
                return ExecutionResult(
                    status="failed",
                    completed_moves=completed,
                    pause_points=pause_points,
                    failed_move=move,
                    error=str(error),
                )
            try:
                self.memory.apply_move(move)
            except MoveLayoutError as error:
                if self.failure_recorder is not None:
                    self.failure_recorder(move.block_id, move.target_address)
                return ExecutionResult(
                    status="failed",
                    completed_moves=completed,
                    pause_points=pause_points,
                    failed_move=move,
                    error=str(error),
                )
            completed.append(move)
            pause_points.append(plan.pause_points[index + 1])
            if self.token.cancelled:
                return ExecutionResult(
                    status="cancelled",
                    completed_moves=completed,
                    pause_points=pause_points,
                )

        return ExecutionResult(
            status="completed",
            completed_moves=completed,
            pause_points=pause_points,
        )

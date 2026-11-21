"""离线碎片整理规划器。

规划器只做静态分析并产出搬迁计划，不执行真实显存拷贝：

1. 活动请求依赖的块（钉住块）与已经搬迁过的块作为屏障，把显存
   切成若干可整理段；
2. 对跨度足够的段，模拟把段内可搬迁块逐一搬到段外空洞；
3. 共享块（引用计数 > 1 或显式标记）排在最后，且任何块在一次
   整理生命周期内最多搬迁一次，避免反复拷贝；
4. 所有同尺寸候选按确定性规则破平，保证计划可复现；
5. 历史上失败过的 ``(块, 目标地址)`` 组合直接跳过，支持中断或
   拷贝失败后基于当前布局重新规划。
"""

from typing import Dict, List, Optional, Set, Tuple

from .memory import MemoryMap, Segment, carve_hole
from .model import Hole, Move, PausePoint, RelocationPlan


class NoFeasibleSpaceError(RuntimeError):
    """总空闲显存足够，但无法凑出所需连续空间。"""

    def __init__(self, message: str, reason: str, details: Optional[dict] = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class DefragPlanner:
    """根据内存布局生成离线搬迁计划的规划器。"""

    def __init__(self, memory: MemoryMap):
        self.memory = memory
        self.failed_attempts: Set[Tuple[str, int]] = set()

    def remember_failure(self, block_id: str, target_address: int) -> None:
        """记录一次执行期拷贝失败，后续规划不再尝试该组合。"""

        self.failed_attempts.add((block_id, target_address))

    def build_plan(self, needed_size: int) -> RelocationPlan:
        """为满足 ``needed_size`` 的连续 KV 空间生成搬迁计划。"""

        if not isinstance(needed_size, int) or needed_size <= 0:
            raise ValueError("needed_size 必须是正整数")

        largest = self.memory.largest_hole()
        if largest is not None and largest.size >= needed_size:
            return RelocationPlan(
                needed_size=needed_size,
                feasible=True,
                moves=[],
                pause_points=[PausePoint(0, None, largest.address, largest.size)],
                target_address=largest.address,
                target_size=largest.size,
            )

        candidates: List[RelocationPlan] = []
        infeasible_reasons: List[Dict[str, object]] = []
        for segment in self.memory.segments():
            if segment.size < needed_size:
                continue
            plan = self._plan_for_segment(segment, needed_size)
            if plan is not None:
                candidates.append(plan)
            else:
                infeasible_reasons.append(
                    {
                        "segment_address": segment.address,
                        "segment_size": segment.size,
                    }
                )

        if not candidates:
            total_free = sum(hole.size for hole in self.memory.holes)
            raise NoFeasibleSpaceError(
                "无法整理出 %d 的连续空间（当前空闲总量 %d）"
                % (needed_size, total_free),
                reason="no_feasible_segment",
                details={
                    "needed_size": needed_size,
                    "total_free": total_free,
                    "largest_hole": largest.size if largest else 0,
                    "segments_tried": infeasible_reasons,
                },
            )

        return min(
            candidates,
            key=lambda plan: (
                plan.move_count,
                plan.target_address if plan.target_address is not None else -1,
            ),
        )

    def try_build_plan(self, needed_size: int) -> RelocationPlan:
        """与 :meth:`build_plan` 相同，但失败时返回不可行计划而非抛异常。"""

        try:
            return self.build_plan(needed_size)
        except NoFeasibleSpaceError as error:
            return RelocationPlan(
                needed_size=needed_size,
                feasible=False,
                reason=error.reason,
                failed_attempts=set(self.failed_attempts),
            )

    def _plan_for_segment(
        self, segment: Segment, needed_size: int
    ) -> Optional[RelocationPlan]:
        blocks = [
            self.memory.blocks[block_id] for block_id in segment.block_ids
        ]
        if any(self.memory.is_pinned(block.block_id) for block in blocks):
            return None

        external_holes = [
            Hole(hole.address, hole.size)
            for hole in self.memory.holes
            if not self._inside(hole.address, segment)
        ]
        eviction_order = sorted(blocks, key=self._eviction_key)
        moves: List[Move] = []

        for block in eviction_order:
            target = self._choose_target(block, external_holes)
            if target is None:
                return None
            move = Move(
                block_id=block.block_id,
                size=block.size,
                source_address=block.address,
                target_address=target,
                shared=block.is_shared,
            )
            moves.append(move)
            external_holes = carve_hole(external_holes, target, block.size)

        cleared_hole = Hole(segment.address, segment.size)

        pause_points = [
            PausePoint(0, None, cleared_hole.address, cleared_hole.size)
        ]
        for index, move in enumerate(moves, start=1):
            pause_points.append(
                PausePoint(
                    move_index=index,
                    completed_block_id=move.block_id,
                    free_address=cleared_hole.address,
                    free_size=cleared_hole.size,
                )
            )
        return RelocationPlan(
            needed_size=needed_size,
            feasible=True,
            moves=moves,
            pause_points=pause_points,
            target_address=cleared_hole.address,
            target_size=cleared_hole.size,
            failed_attempts=set(self.failed_attempts),
        )

    def _choose_target(
        self, block, external_holes: List[Hole]
    ) -> Optional[int]:
        candidates = [
            hole for hole in external_holes if hole.size >= block.size
        ]
        candidates.sort(key=lambda hole: (hole.size, hole.address))
        for hole in candidates:
            if (block.block_id, hole.address) not in self.failed_attempts:
                return hole.address
        return None

    @staticmethod
    def _inside(address: int, segment: Segment) -> bool:
        return segment.address <= address < segment.end

    @staticmethod
    def _eviction_key(block) -> Tuple[int, int, int, str]:
        # 非共享块优先；同尺寸时引用少的优先；再按大块优先、ID 破平。
        return (
            1 if block.is_shared else 0,
            block.ref_count,
            -block.size,
            block.block_id,
        )

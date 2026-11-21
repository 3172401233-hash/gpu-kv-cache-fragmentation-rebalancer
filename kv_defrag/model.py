"""规划器使用的不可变数据结构。

所有地址与大小均为整数，语义为 KV 缓存存储区的线性地址单元，
区间统一采用左闭右开 ``[address, address + size)``。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple


@dataclass
class Block:
    """一块 KV 缓存（通常对应一个前缀长度的注意力 K/V 张量）。

    ``ref_count`` 为引用该块的请求数量；大于 1 时即为共享前缀块。
    也可以通过 ``shared=True`` 显式标记共享块（例如引用计数尚未统计
    到所有依赖请求时）。
    """

    block_id: str
    size: int
    address: int
    ref_count: int = 1
    shared: bool = False
    moved: bool = False

    @property
    def end(self) -> int:
        return self.address + self.size

    @property
    def is_shared(self) -> bool:
        return self.shared or self.ref_count > 1


@dataclass
class Hole:
    """一段连续空闲显存（空洞）。"""

    address: int
    size: int

    @property
    def end(self) -> int:
        return self.address + self.size


@dataclass
class ActiveRequest:
    """当前活动请求；其依赖的块属于钉住块，禁止搬迁。"""

    request_id: str
    block_ids: List[str] = field(default_factory=list)
    needed_size: int = 0


@dataclass
class Move:
    """一条离线搬迁指令。

    仅描述“把哪个块搬到哪个空洞的什么位置”，不直接触碰显存；
    真正的拷贝由推理引擎在执行阶段按计划完成。
    """

    block_id: str
    size: int
    source_address: int
    target_address: int
    shared: bool = False


@dataclass
class PausePoint:
    """可恢复的暂停检查点。

    每执行（或计划执行）一次搬迁后产生一个检查点，中断后可从
    ``move_index`` 指向的下一条指令继续，无需回滚已完成的拷贝。
    """

    move_index: int
    completed_block_id: Optional[str]
    free_address: int
    free_size: int


@dataclass
class RelocationPlan:
    """一次碎片整理的完整离线计划。"""

    needed_size: int
    feasible: bool
    moves: List[Move] = field(default_factory=list)
    pause_points: List[PausePoint] = field(default_factory=list)
    target_address: Optional[int] = None
    target_size: int = 0
    reason: Optional[str] = None
    failed_attempts: Set[Tuple[str, int]] = field(default_factory=set)

    @property
    def move_count(self) -> int:
        return len(self.moves)


@dataclass
class ExecutionResult:
    """执行器应用搬迁计划后的结果。"""

    status: str
    completed_moves: List[Move] = field(default_factory=list)
    pause_points: List[PausePoint] = field(default_factory=list)
    failed_move: Optional[Move] = None
    error: Optional[str] = None

    @property
    def completed_count(self) -> int:
        return len(self.completed_moves)

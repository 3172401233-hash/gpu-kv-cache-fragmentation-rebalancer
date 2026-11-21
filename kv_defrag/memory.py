"""显存布局建模与维护。"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .model import ActiveRequest, Block, Hole, Move


def add_free_range(holes: List[Hole], address: int, size: int) -> List[Hole]:
    """把一段空闲区并入空洞列表，自动合并相邻/重叠区间。"""

    if size <= 0:
        return sorted(holes, key=lambda hole: hole.address)
    merged = [(address, address + size)]
    for hole in holes:
        merged.append((hole.address, hole.end))
    merged.sort()
    ranges: List[Tuple[int, int]] = []
    for start, end in merged:
        if ranges and start <= ranges[-1][1]:
            if end > ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return [Hole(start, end - start) for start, end in ranges]


def allocate_from_hole(
    holes: Iterable[Hole], size: int
) -> Tuple[Optional[int], List[Hole]]:
    """按 best-fit（同尺寸取最低地址）从空洞中分配 ``size``。

    返回分配到的起始地址与分配后剩余的空洞列表；无合适空洞时
    返回 ``(None, 原空洞副本)``。
    """

    remaining = sorted(
        (Hole(hole.address, hole.size) for hole in holes),
        key=lambda hole: hole.address,
    )
    candidates = [hole for hole in remaining if hole.size >= size]
    if not candidates:
        return None, remaining
    chosen = min(candidates, key=lambda hole: (hole.size, hole.address))
    target = chosen.address
    chosen.address += size
    chosen.size -= size
    leftover = [hole for hole in remaining if hole.size > 0]
    return target, leftover


def carve_hole(holes: Iterable[Hole], address: int, size: int) -> List[Hole]:
    """从覆盖 ``[address, address + size)`` 的空洞中切出一段空间。"""

    end = address + size
    result: List[Hole] = []
    matched = False
    for hole in holes:
        if not matched and hole.address <= address and end <= hole.end:
            matched = True
            left = address - hole.address
            right = hole.end - end
            if left:
                result.append(Hole(hole.address, left))
            if right:
                result.append(Hole(end, right))
        else:
            result.append(Hole(hole.address, hole.size))
    if not matched:
        raise MoveLayoutError(
            "地址 %d 处没有可容纳 %d 单元的空洞" % (address, size)
        )
    return sorted(result, key=lambda hole: hole.address)


@dataclass
class Segment:
    """两个钉住块之间、可整体清空的一段连续区域。"""

    address: int
    size: int
    block_ids: List[str]

    @property
    def end(self) -> int:
        return self.address + self.size


class MemoryMap:
    """某个时刻 KV 缓存的完整布局快照。"""

    def __init__(
        self,
        blocks: List[Block],
        holes: List[Hole],
        active_requests: Optional[List[ActiveRequest]] = None,
        capacity: Optional[int] = None,
    ) -> None:
        self.blocks: Dict[str, Block] = {block.block_id: block for block in blocks}
        if len(self.blocks) != len(blocks):
            raise ValueError("block_id 必须唯一")
        self.holes = sorted(
            (Hole(hole.address, hole.size) for hole in holes),
            key=lambda hole: hole.address,
        )
        self.active_requests = list(active_requests or [])
        self._validate_layout(capacity)
        max_end = 0
        for block in blocks:
            max_end = max(max_end, block.end)
        for hole in self.holes:
            max_end = max(max_end, hole.end)
        self.capacity = capacity if capacity is not None else max_end
        self.pinned_block_ids = self._collect_pinned()

    def _validate_layout(self, capacity: Optional[int]) -> None:
        intervals = [(block.address, block.end, "block:" + block.block_id)
                     for block in self.blocks.values()]
        intervals.extend((hole.address, hole.end, "hole") for hole in self.holes)
        for start, end, label in intervals:
            if start < 0 or end <= start:
                raise ValueError("非法区间: %s [%d, %d)" % (label, start, end))
            if capacity is not None and end > capacity:
                raise ValueError("区间 %s 超出容量 %d" % (label, capacity))
        intervals.sort()
        for (prev_start, prev_end, prev_label), (start, end, label) in zip(
            intervals, intervals[1:]
        ):
            if start < prev_end:
                raise ValueError(
                    "区间重叠: %s [%d, %d) 与 %s [%d, %d)"
                    % (prev_label, prev_start, prev_end, label, start, end)
                )
        for block in self.blocks.values():
            if block.ref_count < 0:
                raise ValueError("引用计数不能为负: %s" % block.block_id)

    def _collect_pinned(self) -> set:
        pinned: set = set()
        for request in self.active_requests:
            for block_id in request.block_ids:
                if block_id not in self.blocks:
                    raise ValueError(
                        "请求 %s 依赖了不存在的块 %s"
                        % (request.request_id, block_id)
                    )
                pinned.add(block_id)
        return pinned

    def is_pinned(self, block_id: str) -> bool:
        return block_id in self.pinned_block_ids

    def movable_blocks(self) -> List[Block]:
        """可搬迁块：未被活动请求钉住、且历史上未搬迁过。"""

        return [
            block
            for block in self.blocks.values()
            if not self.is_pinned(block.block_id) and not block.moved
        ]

    def segments(self) -> List[Segment]:
        """按屏障切分可整理段。

        钉住块、已搬迁块以及现有空洞都是屏障：空洞是现成的空闲区，
        不参与段的清空计算；两个屏障之间的可搬迁块构成一个候选段。
        """

        ordered = sorted(self.blocks.values(), key=lambda block: block.address)
        segments: List[Segment] = []
        current: List[Block] = []
        for block in ordered:
            is_barrier = (
                self.is_pinned(block.block_id)
                or block.moved
                or any(
                    hole.address <= block.address < hole.end
                    for hole in self.holes
                )
            )
            if is_barrier:
                if current:
                    segments.append(
                        self._blocks_to_segment(current, self.holes)
                    )
                    current = []
                continue
            current.append(block)
        if current:
            segments.append(self._blocks_to_segment(current, self.holes))
        return segments

    @staticmethod
    def _blocks_to_segment(
        blocks: List[Block], holes: List[Hole]
    ) -> Segment:
        start = blocks[0].address
        end = max(block.end for block in blocks)
        gap = sum(
            min(hole.end, end) - max(hole.address, start)
            for hole in holes
            if hole.address < end and hole.end > start
        )
        return Segment(
            address=start,
            size=end - start - gap,
            block_ids=[block.block_id for block in blocks],
        )

    def largest_hole(self) -> Optional[Hole]:
        if not self.holes:
            return None
        return max(self.holes, key=lambda hole: (hole.size, -hole.address))

    def apply_move(self, move: Move) -> None:
        """在布局模型上应用一次搬迁（不含真实显存拷贝）。"""

        block = self.blocks.get(move.block_id)
        if block is None:
            raise MoveLayoutError("块不存在: %s" % move.block_id)
        if self.is_pinned(move.block_id):
            raise MoveLayoutError("钉住块禁止搬迁: %s" % move.block_id)
        if block.moved:
            raise MoveLayoutError("块已搬迁过，禁止二次搬迁: %s" % move.block_id)
        if block.address != move.source_address or block.size != move.size:
            raise MoveLayoutError(
                "搬迁指令与当前布局不一致: %s" % move.block_id
            )
        target_end = move.target_address + move.size
        for hole in self.holes:
            if (
                hole.address <= move.target_address
                and target_end <= hole.end
            ):
                break
        else:
            raise MoveLayoutError(
                "目标地址 %d 没有足够空洞容纳块 %s"
                % (move.target_address, move.block_id)
            )
        self.holes = add_free_range(self.holes, block.address, block.size)
        self.holes = carve_hole(
            self.holes, move.target_address, block.size
        )
        block.address = move.target_address
        block.moved = True

    def snapshot_blocks(self) -> List[Block]:
        return [
            Block(
                block_id=block.block_id,
                size=block.size,
                address=block.address,
                ref_count=block.ref_count,
                shared=block.shared,
                moved=block.moved,
            )
            for block in self.blocks.values()
        ]


class MoveLayoutError(RuntimeError):
    """搬迁指令与当前内存布局不一致。"""

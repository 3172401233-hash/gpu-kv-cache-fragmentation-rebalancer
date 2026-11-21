import unittest

from kv_defrag import (
    ActiveRequest,
    Block,
    CancellationToken,
    DefragPlanner,
    Hole,
    MemoryMap,
    Move,
    NoFeasibleSpaceError,
    PlanExecutor,
)
from kv_defrag.memory import MoveLayoutError


def build_shared_prefix_map(capacity=60):
    """构造共享前缀场景：

    地址布局（容量 60）::

        p[0,10)  b2[10,20)  bs[20,30)  p2[30,40)  h1[40,50) h2[50,60)

    p/p2 被活动请求钉住；b2 为普通块；bs 被两个请求共享（ref_count=2）。
    目标段 [10,30) 跨度 20，但被两个块占满，外部只有两个 10 单元空洞。
    """

    blocks = [
        Block("p", size=10, address=0, ref_count=1),
        Block("b2", size=10, address=10, ref_count=1),
        Block("bs", size=10, address=20, ref_count=2),
        Block("p2", size=10, address=30, ref_count=1),
    ]
    holes = [Hole(40, 10), Hole(50, 10)]
    request = ActiveRequest(
        request_id="r1", block_ids=["p", "p2"], needed_size=20
    )
    return MemoryMap(blocks, holes, [request], capacity=capacity)


class SharedPrefixPlannerTest(unittest.TestCase):
    def test_shared_blocks_move_last_and_only_once(self):
        memory = build_shared_prefix_map()
        planner = DefragPlanner(memory)

        self.assertEqual(
            sorted(block.block_id for block in memory.movable_blocks()),
            ["b2", "bs"],
        )

        plan = planner.build_plan(20)

        self.assertTrue(plan.feasible)
        self.assertEqual(
            [move.block_id for move in plan.moves], ["b2", "bs"]
        )
        self.assertFalse(plan.moves[0].shared)
        self.assertTrue(plan.moves[1].shared)
        self.assertEqual(plan.moves[0].target_address, 40)
        self.assertEqual(plan.moves[1].target_address, 50)
        self.assertEqual(plan.target_address, 10)
        self.assertEqual(plan.target_size, 20)

        self.assertEqual(
            [point.move_index for point in plan.pause_points], [0, 1, 2]
        )
        self.assertIsNone(plan.pause_points[0].completed_block_id)
        self.assertEqual(plan.pause_points[1].completed_block_id, "b2")
        self.assertEqual(plan.pause_points[2].completed_block_id, "bs")
        self.assertEqual(plan.pause_points[2].free_address, 10)
        self.assertEqual(plan.pause_points[2].free_size, 20)

        executor = PlanExecutor(memory)
        result = executor.execute(plan)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.completed_count, 2)

        self.assertEqual(memory.blocks["p"].address, 0)
        self.assertEqual(memory.blocks["p2"].address, 30)
        self.assertEqual(memory.blocks["b2"].address, 40)
        self.assertTrue(memory.blocks["b2"].moved)
        self.assertEqual(memory.blocks["bs"].address, 50)
        self.assertTrue(memory.blocks["bs"].moved)
        cleared = next(hole for hole in memory.holes if hole.address == 10)
        self.assertEqual(cleared.size, 20)

        # 已搬迁块成为屏障，重新规划时零搬迁且不会二次移动共享块。
        self.assertEqual([block.block_id for block in memory.movable_blocks()], [])
        replanner = DefragPlanner(memory)
        next_plan = replanner.build_plan(20)
        self.assertTrue(next_plan.feasible)
        self.assertEqual(next_plan.move_count, 0)
        self.assertEqual(next_plan.target_address, 10)

    def test_same_size_candidates_are_deterministic(self):
        blocks = [
            Block("p", 10, 0),
            Block("bz", 10, 10),
            Block("bx", 10, 20),
            Block("p2", 10, 30),
        ]
        holes = [Hole(40, 10), Hole(50, 10)]
        request = ActiveRequest("r1", ["p", "p2"])
        memory = MemoryMap(blocks, holes, [request], capacity=60)

        first = DefragPlanner(memory).build_plan(20)
        second = DefragPlanner(
            MemoryMap(
                [Block(block.block_id, block.size, block.address)
                 for block in blocks],
                [Hole(hole.address, hole.size) for hole in holes],
                [ActiveRequest("r1", ["p", "p2"])],
                capacity=60,
            )
        ).build_plan(20)

        # 同尺寸候选按 block_id 破平，计划可复现。
        self.assertEqual(
            [move.block_id for move in first.moves], ["bx", "bz"]
        )
        self.assertEqual(
            [(move.block_id, move.target_address) for move in first.moves],
            [(move.block_id, move.target_address) for move in second.moves],
        )

    def test_existing_hole_means_zero_move_plan(self):
        blocks = [
            Block("p", 10, 0),
            Block("b", 10, 10),
            Block("p2", 10, 20),
        ]
        memory = MemoryMap(
            blocks, [Hole(30, 30)],
            [ActiveRequest("r1", ["p", "p2"])],
            capacity=60,
        )
        plan = DefragPlanner(memory).build_plan(25)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.move_count, 0)
        self.assertEqual(plan.target_address, 30)
        self.assertEqual(memory.blocks["b"].address, 10)

    def test_invalid_needed_size(self):
        planner = DefragPlanner(build_shared_prefix_map())
        with self.assertRaises(ValueError):
            planner.build_plan(0)


class InfeasibleSpaceTest(unittest.TestCase):
    def test_total_free_enough_but_no_hole_fits_block(self):
        # 总空闲 20（段内空洞 10 + 两个 5 单元外部空洞），
        # 但任何外部空洞都放不下 10 单元的块。
        blocks = [
            Block("p", 10, 0),
            Block("b1", 10, 10),
            Block("b2", 10, 30),
            Block("p2", 10, 40),
        ]
        holes = [Hole(20, 10), Hole(50, 5), Hole(55, 5)]
        memory = MemoryMap(
            blocks, holes, [ActiveRequest("r1", ["p", "p2"])], capacity=60
        )
        planner = DefragPlanner(memory)

        with self.assertRaises(NoFeasibleSpaceError) as context:
            planner.build_plan(20)
        self.assertEqual(context.exception.reason, "no_feasible_segment")
        self.assertEqual(context.exception.details["total_free"], 20)
        self.assertEqual(context.exception.details["largest_hole"], 10)

        soft_plan = planner.try_build_plan(20)
        self.assertFalse(soft_plan.feasible)
        self.assertEqual(soft_plan.reason, "no_feasible_segment")


class MoveFailureTest(unittest.TestCase):
    def test_failed_move_is_retried_with_different_target(self):
        memory = build_shared_prefix_map()
        planner = DefragPlanner(memory)
        plan = planner.build_plan(20)

        def failing_move(move):
            if move.block_id == "b2":
                raise RuntimeError("simulated DMA fault")

        executor = PlanExecutor(
            memory,
            move_fn=failing_move,
            failure_recorder=planner.remember_failure,
        )
        result = executor.execute(plan)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_move.block_id, "b2")
        self.assertEqual(result.completed_count, 0)
        self.assertEqual(memory.blocks["b2"].address, 10)
        self.assertIn(("b2", 40), planner.failed_attempts)

        # 重新规划：b2 改去 50，bs 改用 40，失败组合不再出现。
        revised = planner.build_plan(20)
        self.assertTrue(revised.feasible)
        self.assertEqual(
            [(move.block_id, move.target_address) for move in revised.moves],
            [("b2", 50), ("bs", 40)],
        )
        self.assertNotIn(
            ("b2", 40),
            [(move.block_id, move.target_address) for move in revised.moves],
        )

        retry = PlanExecutor(memory).execute(revised)
        self.assertEqual(retry.status, "completed")
        self.assertEqual(memory.blocks["b2"].address, 50)
        self.assertEqual(memory.blocks["bs"].address, 40)

        # 旧计划的指令在新布局上已失效，执行器拒绝应用。
        with self.assertRaises(MoveLayoutError):
            memory.apply_move(plan.moves[0])

    def test_failure_without_alternative_becomes_infeasible(self):
        blocks = [
            Block("p", 10, 0),
            Block("b2", 10, 10),
            Block("bs", 10, 20, ref_count=2),
            Block("bm", 10, 30, moved=True),
        ]
        memory = MemoryMap(
            blocks, [Hole(40, 10), Hole(50, 10)],
            [ActiveRequest("r1", ["p"])],
            capacity=60,
        )
        planner = DefragPlanner(memory)
        plan = planner.build_plan(20)

        def failing_move(move):
            if move.block_id == "b2":
                raise RuntimeError("simulated DMA fault")

        result = PlanExecutor(
            memory,
            move_fn=failing_move,
            failure_recorder=planner.remember_failure,
        ).execute(plan)
        self.assertEqual(result.status, "failed")

        # 两个候选目标都无法搬迁该块：手动补记另一个失败组合，
        # 模拟硬件层面该块当前不可搬迁。
        planner.remember_failure("b2", 50)

        with self.assertRaises(NoFeasibleSpaceError):
            planner.build_plan(20)


class CancellationTest(unittest.TestCase):
    def test_cancel_before_start_then_run_full_plan(self):
        memory = build_shared_prefix_map()
        plan = DefragPlanner(memory).build_plan(20)
        token = CancellationToken()
        token.cancel()

        executor = PlanExecutor(memory, token=token)
        result = executor.execute(plan)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.completed_count, 0)
        self.assertEqual(memory.blocks["b2"].address, 10)

        token.reset()
        resumed = executor.execute(plan, start_index=0)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.completed_count, 2)

    def test_cancel_between_moves_and_resume_from_pause_point(self):
        memory = build_shared_prefix_map()
        planner = DefragPlanner(memory)
        plan = planner.build_plan(20)
        token = CancellationToken()

        def cancel_after_first(move):
            token.cancel()

        executor = PlanExecutor(memory, move_fn=cancel_after_first, token=token)
        result = executor.execute(plan)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(
            [move.block_id for move in result.completed_moves], ["b2"]
        )
        self.assertEqual(len(result.pause_points), 2)
        self.assertEqual(result.pause_points[-1].completed_block_id, "b2")
        self.assertEqual(memory.blocks["b2"].address, 40)
        self.assertTrue(memory.blocks["b2"].moved)
        self.assertEqual(memory.blocks["bs"].address, 20)
        self.assertFalse(memory.blocks["bs"].moved)

        token.reset()
        resumed = PlanExecutor(memory, token=token).execute(
            plan, start_index=1
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.completed_count, 1)
        self.assertEqual(
            [move.block_id for move in resumed.completed_moves], ["bs"]
        )
        self.assertEqual(memory.blocks["bs"].address, 50)
        cleared = next(hole for hole in memory.holes if hole.address == 10)
        self.assertEqual(cleared.size, 20)


if __name__ == "__main__":
    unittest.main()

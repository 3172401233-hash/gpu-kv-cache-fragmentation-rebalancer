# gpu-kv-cache-fragmentation-rebalancer

推理节点长时间运行后，KV 缓存的前缀块被反复分配/释放，总空闲显存
看似充足，却没有足够长的连续区域容纳新请求。本项目实现一个
**离线 KV 缓存碎片整理规划器**：输入当前块布局、引用计数、请求依赖、
共享前缀与显存空洞，输出一份搬迁计划（哪些块搬到哪里、在哪里暂停），
由推理引擎在执行阶段按计划完成真实拷贝。

> 注意：本项目是**离线搬迁计划器（offline relocation planner）**，
> 只做静态分析并产出 `Move` 指令与暂停点，本身不执行任何 GPU 内存
> 拷贝，也不接管推理引擎的分配器。执行语义由 `PlanExecutor` 在内存
> 布局模型上模拟，供验证与断点续跑使用。

## 约束与策略

- **钉住块不可移动**：活动请求依赖的块（`ActiveRequest.block_ids`）
  构成屏障，永远不会出现在搬迁计划里。
- **共享块只搬一次**：引用计数大于 1（或显式 `shared=True`）的共享
  前缀块在淘汰排序中排在最后；所有块搬迁后都会被标记 `moved`，成为
  后续规划的屏障，杜绝同一前缀被反复拷贝。
- **降低大块请求失败率**：优先选择搬迁次数最少的候选段（最小化拷贝
  开销），并在段跨度足够时才输出可行计划。
- **确定性破平**：同尺寸候选按“是否共享 → 引用计数 → 块大小 →
  block_id”排序；目标空洞按 best-fit（同容量取最低地址）选择，
  计划可复现、可审计。
- **无可行空间显式失败**：当总空闲足够但没有任何空洞能容纳待搬迁块
  时，抛出 `NoFeasibleSpaceError`，携带空闲总量、最大空洞与尝试过的
  段等诊断信息；也可用 `try_build_plan` 拿到 `feasible=False` 的计划。
- **失败与中断可恢复**：执行期拷贝失败的 `(块, 目标地址)` 组合会被
  记忆，重新规划时自动跳过；`CancellationToken` 支持在两条搬迁之间
  暂停，已完成的拷贝保留，通过 `start_index` 从暂停点续跑。

## 目录结构

- `kv_defrag/model.py`：`Block`、`Hole`、`ActiveRequest`、`Move`、
  `PausePoint`、`RelocationPlan`、`ExecutionResult` 数据结构。
- `kv_defrag/memory.py`：`MemoryMap` 布局模型，负责钉住块识别、
  空洞合并/切分、候选段切分与搬迁应用。
- `kv_defrag/planner.py`：`DefragPlanner` 离线规划器与
  `NoFeasibleSpaceError`。
- `kv_defrag/executor.py`：`PlanExecutor`、`CancellationToken`、
  `MoveError`，模拟执行、失败注入与断点续跑。
- `tests/test_defrag_planner.py`：unittest 覆盖。

## 快速使用

```python
from kv_defrag import (
    ActiveRequest, Block, DefragPlanner, Hole, MemoryMap, PlanExecutor,
)

memory = MemoryMap(
    blocks=[
        Block("p", size=10, address=0),
        Block("b", size=10, address=10),
        Block("p2", size=10, address=20),
    ],
    holes=[Hole(30, 10)],
    active_requests=[ActiveRequest("r1", ["p", "p2"])],
    capacity=40,
)

planner = DefragPlanner(memory)
plan = planner.try_build_plan(needed_size=20)
if plan.feasible:
    PlanExecutor(memory).execute(plan)  # 仅在布局模型上模拟应用
```

## 运行测试

代码面向 Python 3.8.2 语法（`typing` 注解、dataclass，无 3.9+ 的
泛型内建语法）。在仓库根目录执行：

```bash
python -m unittest
```

覆盖场景：

- 共享前缀块排在最后且只搬迁一次，暂停点可用于续跑；
- 同尺寸多候选的确定性排序与现有空洞足够时的零搬迁计划；
- 总空闲足够但空洞放不下待搬迁块时的不可行判定；
- 搬迁失败后自动改选目标、无替代目标时重新判定不可行；
- 执行前取消、两条搬迁之间取消并从暂停点恢复。

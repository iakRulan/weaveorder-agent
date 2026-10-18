# WeaveOrder v0.4 消融/基线评测（live）

- 生成时间：2026-10-18T11:04:02
- 模型：`intern-s2` / fast `qwen3.8-27b`
- 任务：3 条 × 4 变体

| 变体 | 移除的组件 | 通过 | 平均耗时(s) | 步骤 ok/total | 重规划 | 修复/优化 | 失败任务 |
|---|---|---|---|---|---|---|---|
| `ours` | 完整织序：DAG 并行 + 确定性校验/验收 + 重规划 | 3/3 | 76.8 | 19/21 | 0 | 3 | — |
| `w/o-verifier` | 移除确定性校验与整体验收 | 1/3 | 62.5 | 18/21 | 2 | 0 | T02, T01-inject |
| `w/o-replanner` | 移除失败重规划（仅剩步骤内修复） | 3/3 | 57.3 | 19/21 | 0 | 3 | — |
| `serial-exec` | 同层并行退化为串行流水线 | 3/3 | 55.2 | 19/21 | 0 | 3 | — |

## 明细

| 变体 | Task | 期望 | 状态 | 结果 | 总费用 | 重规划 | 修复 | 耗时 |
|---|---|---|---|---|---|---|---|---|
| `ours` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 119.27s |
| `ours` | T02 | degraded | degraded | ✅ | ¥2290 | 0 | 2 | 89.15s |
| `ours` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 22.02s |
| `w/o-verifier` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 55.34s |
| `w/o-verifier` | T02 | degraded | ok | ❌ | ¥2960 | 0 | 0 | 44.22s |
| `w/o-verifier` | T01-inject | ok | failed | ❌ | — | 2 | 0 | 87.91s |
| `w/o-replanner` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 39.97s |
| `w/o-replanner` | T02 | degraded | degraded | ✅ | ¥2290 | 0 | 2 | 72.72s |
| `w/o-replanner` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 59.33s |
| `serial-exec` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 43.32s |
| `serial-exec` | T02 | degraded | degraded | ✅ | ¥2290 | 0 | 2 | 80.81s |
| `serial-exec` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 41.45s |

## 与技术方案的对应

- `ours` = 完整编排（§3 架构全量）。
- `w/o-verifier` / `w/o-replanner` = §6.4 消融。
- `serial-exec` = §6.3 固定流程基线（同层并行关闭）。
- ReAct 单 Agent 基线 = `mode=agent`，见 results-live-*.md 中 T13（Agent）vs T01（DAG）。
- 并行的时间收益用 live 模式计时体现（mock 工具瞬时返回，耗时差为 0）。

## 口径

- 通过判定与主评测一致：期望 ok/degraded/failed 分别校验。
- `w/o-verifier` 下预算无解任务（T02/T08）若被判 ok 但总费用超预算，即为验收缺失的直接证据。
- T01-inject 注入去程航班空结果：ours 依赖步骤内修复/重规划恢复；w/o-verifier 会把空结果采信并向下游传播。

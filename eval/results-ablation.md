# WeaveOrder v0.4 消融/基线评测（mock）

- 生成时间：2026-10-18T10:48:52
- 模型：`intern-s2` / fast `qwen3.8-27b`
- 任务：5 条 × 4 变体

| 变体 | 移除的组件 | 通过 | 平均耗时(s) | 步骤 ok/total | 重规划 | 修复/优化 | 失败任务 |
|---|---|---|---|---|---|---|---|
| `ours` | 完整织序：DAG 并行 + 确定性校验/验收 + 重规划 | 5/5 | 0.0 | 31/35 | 0 | 7 | — |
| `w/o-verifier` | 移除确定性校验与整体验收 | 1/5 | 0.0 | 32/35 | 0 | 0 | T02, T05, T08, T01-inject |
| `w/o-replanner` | 移除失败重规划（仅剩步骤内修复） | 5/5 | 0.0 | 31/35 | 0 | 7 | — |
| `serial-exec` | 同层并行退化为串行流水线 | 5/5 | 0.0 | 31/35 | 0 | 7 | — |

## 明细

| 变体 | Task | 期望 | 状态 | 结果 | 总费用 | 重规划 | 修复 | 耗时 |
|---|---|---|---|---|---|---|---|---|
| `ours` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 0.01s |
| `ours` | T02 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `ours` | T05 | ok | ok_with_warnings | ✅ | ¥2480 | 0 | 2 | 0.01s |
| `ours` | T08 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `ours` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 0.01s |
| `w/o-verifier` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 0.01s |
| `w/o-verifier` | T02 | degraded | ok | ❌ | ¥2960 | 0 | 0 | 0.01s |
| `w/o-verifier` | T05 | ok | ok | ❌ | ¥2940 | 0 | 0 | 0.01s |
| `w/o-verifier` | T08 | degraded | ok | ❌ | ¥2960 | 0 | 0 | 0.01s |
| `w/o-verifier` | T01-inject | ok | failed | ❌ | — | 0 | 0 | 0.0s |
| `w/o-replanner` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 0.01s |
| `w/o-replanner` | T02 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `w/o-replanner` | T05 | ok | ok_with_warnings | ✅ | ¥2480 | 0 | 2 | 0.01s |
| `w/o-replanner` | T08 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `w/o-replanner` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 0.01s |
| `serial-exec` | T01 | ok | ok | ✅ | ¥2960 | 0 | 0 | 0.01s |
| `serial-exec` | T02 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `serial-exec` | T05 | ok | ok_with_warnings | ✅ | ¥2480 | 0 | 2 | 0.01s |
| `serial-exec` | T08 | degraded | degraded | ✅ | ¥2130 | 0 | 2 | 0.01s |
| `serial-exec` | T01-inject | ok | ok | ✅ | ¥2960 | 0 | 1 | 0.01s |

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

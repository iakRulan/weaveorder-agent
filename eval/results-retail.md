# WeaveOrder 评测结果（retail · 智慧零售经营分析场景）

- 生成时间：2026-11-03T10:45:02
- 任务数：7（含注入与消融用例）
- 通过：7/7
- 总耗时：0.63s（确定性，可复现）

## 黄金值（评测脚本从 CSV 独立重算）

- Tables 利润率：-8.56%
- 亏损子品类数：3
- 折扣>30% 利润率：-48.16%
- 整体利润率：12.47%（销售额 2,297,201）
- 退货订单数：296；退货率最高：Machines 11.61%

| Task | 期望 | 状态 | 结果 | 断言 | 说明 |
|---|---|---|---|---|---|
| R01 | ok | ok | ✅ PASS | 全部通过 | 主用例：亏损归因（黄金值由评测脚本从 CSV 独立重算） |
| R02 | ok | ok | ✅ PASS | 全部通过 | 意图识别：trend |
| R03 | ok | ok | ✅ PASS | 全部通过 | 意图识别：region（关联 people 表负责人） |
| R04 | ok | ok | ✅ PASS | 全部通过 | 意图识别：category_overview |
| R05 | failed | failed | ✅ PASS | 全部通过 | 注入错误指标 + 校验开 → 必须拦截，不发布 |
| R06 | ok | ok | ✅ PASS | 全部通过 | 注入 + 校验关 → 错误数字直接发布（w/o-verifier 失败模式） |
| R07 | ok | ok | ✅ PASS | 全部通过 | 跨表退货率：orders × returns JOIN，最高为 Machines 11.6% |

## 口径

- 黄金值由评测脚本直接从 `data/superstore_orders.csv` / `superstore_returns.csv` 独立重算，与被测实现零共享代码。
- R05/R06 为同一注入的对照：校验开 → 拦截（failed）；校验关 → 错误数字直接发布（w/o-verifier 失败模式）。
- R07 验证跨表贯通：orders × returns 按 order_id JOIN 计算退货率。

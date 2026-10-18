# WeaveOrder v0.4 评测结果（analysis · 经营分析场景）

- 生成时间：2026-10-18T12:36:25
- 任务数：6（含注入与消融用例）
- 通过：6/6
- 总耗时：0.03s（确定性，可复现）

| Task | 期望 | 状态 | 结果 | 断言 | 说明 |
|---|---|---|---|---|---|
| A01 | degraded | degraded | ✅ PASS | 全部通过 | 主场景：华东数码骤降归因；缺失值披露→degraded |
| A02 | degraded | degraded | ✅ PASS | 全部通过 | 正向对照：家电稳增 |
| A03 | degraded | degraded | ✅ PASS | 全部通过 | 默认大区路由：未指明地区时取华东 |
| A04 | failed | failed | ✅ PASS | 全部通过 | 不可达用例：数据源不覆盖→诚实失败，不编造 |
| A05 | failed | failed | ✅ PASS | 全部通过 | 注入：渲染时算错指标→校验器拦截，不会发布 |
| A06 | degraded | degraded | ✅ PASS | 全部通过 | 消融 w/o-verifier：错误数字 -29.2% 直接发布（正确值 -58.4%）【已发布错误数字（正确值 -58.4%）——w/o-verifier 的失败模式】 |

## 口径

- 黄金值由评测脚本直接从 `data/sales_2026Q3.csv` 独立重算，与被测实现零共享代码。
- A05/A06 为同一注入的对照：校验开 → 拦截（failed）；校验关 → 错误数字直接发布（w/o-verifier 失败模式）。
- A04 数据源不覆盖西北大区：诚实失败，不编造。

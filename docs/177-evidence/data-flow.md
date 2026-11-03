# 数据流转记录

## 数据源

| 表 | 行数 | 说明 |
|---|---|---|
| orders | 9,994 | 订单明细（21 列，含销售/利润/折扣/品类/区域） |
| returns | 296 | 退货订单清单 |
| people | 4 | 区域负责人 |

数据来自公开的 Tableau Superstore 零售数据集，装载进内存 SQLite 后由 `sql_query` 真执行。

## 流转路径

```
superstore_orders.csv ─┐
superstore_returns.csv ─┼─→ SQLite(orders/returns/people) ─→ sql_query ─→ 报告指标 ─→ report_render ─→ Markdown
superstore_people.csv ──┘
```

## 分析步骤与数据引用

| 步骤 | 目标 | 工具 | 依赖 |
|---|---|---|---|
| s1 | 读取零售数据集（orders/returns/people 三张表画像） | `file_read` | — |
| s2 | 子品类盈亏榜（按利润升序） | `sql_query` | s1 |
| s3 | 折扣档位盈亏（定位亏损是否由折扣驱动） | `sql_query` | s1 |
| s4 | 退货率最高的子品类（跨表 JOIN orders×returns） | `sql_query` | s1 |
| s5 | 品类汇总 | `sql_query` | s1 |
| s6 | 整体汇总指标 | `sql_query` | s1 |
| s7 | 生成经营分析报告（指标全部引用 SQL 结果） | `report_render` | s2, s3, s4, s5, s6 |

## 跨表贯通

退货率分析把 `orders` 与 `returns` 通过 `order_id` 做 LEFT JOIN，
实现跨表数据贯通（非单表统计）。

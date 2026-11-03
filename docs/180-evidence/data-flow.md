# 数据流转记录

## 数据源

| 表 | 行数 | 说明 |
|---|---|---|
| clients | 30,000 | 授信客户明细（25 列：额度/还款状态×6/账单×6/还款额×6/标签） |

数据来自 UCI 公开数据集「Default of Credit Card Clients」（台湾地区某银行 2005 年 4–9 月信用卡授信客户），装载进内存 SQLite 后由 `sql_query` 真执行只读 SELECT。

## 流转路径

```
credit_clients.csv ─→ SQLite(clients) ─→ sql_query ─→ 报告指标 ─→ report_render ─→ Markdown
```

## 分析步骤与数据引用

| 步骤 | 目标 | 工具 | 依赖 |
|---|---|---|---|
| s1 | 读取信贷数据集（clients 表 30,000 户画像） | `file_read` | — |
| s2 | 整体违约率与客群概览 | `sql_query` | s1 |
| s3 | 最近一月还款状态分组违约率（最强信号） | `sql_query` | s1 |
| s4 | 历史最长逾期月数分组违约率 | `sql_query` | s1 |
| s5 | 额度分档违约率 | `sql_query` | s1 |
| s6 | 额度使用率分组违约率 | `sql_query` | s1 |
| s7 | 风险分层（高危/关注/中风险/低风险） | `sql_query` | s1 |
| s8 | 生成信贷风险归因报告（指标全部引用 SQL 结果） | `report_render` | s2, s3, s4, s5, s6, s7 |

## 派生指标口径

所有派生指标在 SQL 内现算，不预先固化，保证报告数字可回溯到同一条 SQL：

- 最长逾期月数 `mx = MAX(pay_0, pay_2, pay_3, pay_4, pay_5, pay_6)`；`mx<0` 为正常/循环，`mx=0` 为最低还款，`mx>=1` 为逾期
- 近 6 月逾期次数 `cnt = (pay_0>=1)+(pay_2>=1)+…+(pay_6>=1)`
- 额度使用率 `util = bill_amt1 / limit_bal`；`util<0` 为溢缴款（账单为负）
- 违约标签 `is_default`（装载时由保留字 `default` 重命名）

---
name: travel-orchestration
description: 企业差旅编排技能。用户要求安排出差、客户拜访、行程、机票/酒店/预算/审批时使用：先查差旅政策，再搜航班和酒店，用工具计算费用，生成可审阅行程，最后请求人工确认。适用于织序 WeaveOrder、Claude Code、OpenClaw 等支持 Agent Skills 的 harness。
---

# Travel Orchestration

把一句自然语言差旅目标转成可执行、可校验、可追溯的行程，而不是直接给一段建议。

## When to use

- 用户说“帮我安排/规划一次出差”“订机票酒店”“做行程”“预算内安排客户拜访”
- 需要同时满足：日期、预算、差旅政策、早班/酒店位置等约束
- 需要给出可审阅的行程单和费用明细

## Workflow

1. **解析目标**：出发地、目的地、去程/返程日期、预算、偏好。
2. **查政策**：调用 `check_policy`，拿到机票舱位、酒店标准、审批阈值。
3. **搜航班**：调用 `search_flights`，优先 `best`（综合价格与时间），不要只看最低价。
4. **搜酒店**：调用 `search_hotels`，优先 `best`（综合价格与距离），注意目的地城市标准。
5. **算费用**：调用 `compute_trip_cost`，输入机票价、酒店单价、晚数；不要让模型心算。
6. **组装行程**：调用 `draft_itinerary`，传入航班、酒店、政策、总费用。
7. **校验**：
   - 总费用 ≤ 预算
   - 单程机票 ≤ 1800 元（示例政策）
   - 深圳/广州酒店 ≤ 550 元/晚（示例政策）
   - 避免红眼航班；酒店距客户地点尽量 ≤ 3 km
   - 费用 = 机票 + 酒店单价 × 晚数
8. **人工确认**：`request_approval` 是高风险管理动作，必须在预订前暂停，展示动作、金额和政策依据。

## Tools

优先通过 MCP server（`mcp_server.py`）调用；也可以直接调用 `agent.py` 中的同名函数。

| Tool | 用途 |
|---|---|
| `check_policy(query)` | 检索差旅政策 |
| `search_flights(origin, destination, date, max_price?)` | 搜航班，返回 options/cheapest/best |
| `search_hotels(city, checkin, checkout, max_price?)` | 搜酒店，返回 options/cheapest/best |
| `compute_trip_cost(flight_price, hotel_price, nights)` | 算总费用与明细 |
| `draft_itinerary(goal, flight, hotel, policy, total_cost)` | 生成行程草案 |
| `request_approval(action, detail)` | 预订前人工确认 |

## Guardrails

- 不要把“最低价”当成“最优解”：先满足政策和偏好，再比价格。
- 不要在预算不足时静默降级：把“红眼航班/远距离酒店/超标准”作为冲突显式列出。
- 不要自动执行预订、付款、发送类动作；必须人工确认。
- 不要编造价格、库存或政策条款；所有结论必须来自工具返回。
- 如果没有可用航班/酒店，触发一次重规划（放宽价格或换日期），仍失败则明确报告。

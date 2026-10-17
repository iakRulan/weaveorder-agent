# 织序 WeaveOrder

**可审计、可干预、可复用的任务编排智能体** — 第三届“兴智杯”·大模型智能体关键能力技术创新赛参赛项目。

## 一句话

用户只给一句自然语言目标，织序负责把它拆成可执行的任务 DAG，调用工具、校验结果、失败重规划，并在高风险动作前请求人工确认，最终产出可追溯的执行报告。

## 为什么是它

赛题考察的三项关键能力，正好对应引擎的三层：

| 赛题能力 | 织序的实现 |
|---|---|
| 自主规划 | Planner 生成带依赖关系的 DAG；Verifier 失败时触发 Replanner |
| 工具调用 | 类型化 Tool Registry + JSON Schema 参数校验 + 超时/重试 + 结果引用 |
| 多步推理 | Blackboard 汇总中间结果；依赖调度；数值/政策/完整性校验 |

主场景是**企业差旅编排**：一句话完成“查政策 → 搜航班 → 搜酒店 → 算预算 → 校验合规 → 人工确认 → 出行程单”。第二个场景是**经营分析报告**，用于证明引擎不是只能做单一任务。

## 当前状态

第一版是**最小可运行原型**，包含：

- 编排引擎：规划、依赖调度、工具调用、校验、一次修复/重规划、Trace
- 差旅场景：政策检索、航班/酒店 mock 搜索、费用计算、行程草案
- 无 API Key 的 `--mock` 模式，便于先跑通流程
- 书生 Intern-S2 的 OpenAI 兼容接入（有 Key 时启用 LLM 规划）

尚未完成（下一步）：Web Trace UI、经营分析场景、评测集与基线、技术文档与演示视频。

## 快速开始

```bash
cd weaveorder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 不配置 API Key 也能跑：使用内置规则规划和 mock 数据
python agent.py --mock "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元，偏好早班机"

# 配置书生 API Key 后，使用 LLM 规划
cp .env.example .env
# 编辑 .env，填入 INTERN_API_KEY
set -a; source .env; set +a
python agent.py "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元"
```

运行后会在 `runs/<时间戳>/` 下生成：

- `plan.json`：规划结果
- `trace.jsonl`：逐步执行轨迹
- `report.md`：最终行程草案 + 执行摘要

## 目录

```
weaveorder/
├── agent.py                 # 最小可运行原型（编排引擎 + 差旅工具 + CLI）
├── data/
│   ├── policy.md            # 差旅政策（mock）
│   ├── flights.json         # 航班（mock）
│   └── hotels.json          # 酒店（mock）
├── docs/
│   ├── 01-前置准备清单.md
│   ├── 02-技术方案.md
│   ├── 03-提交材料清单.md
│   └── 04-演示脚本.md
├── requirements.txt
└── .env.example
```

## 已验证

在当前环境用 Python 3.9.6 跑过：

- `python3 -m py_compile agent.py` 通过
- mock 正常路径：政策 → 航班 → 酒店 → 费用 → 行程 → 审批，全链路通过
- `--inject-failure`：航班空结果 → 校验失败 → 放宽价格上限 → 恢复
- 预算 2000：总费用超预算 → 压缩航班/酒店 → 修复，并输出“红眼航班/远距离酒店”冲突提示
- 审批：输入 `y` 通过，输入 `n` 标记 `skipped`

LLM 规划路径需要配置 `INTERN_API_KEY` 后验证。

## 常见问题

- **接口返回 `401 / A0211 user token expired`**：书生 token 已过期。重新登录 https://internlm.intern-ai.org.cn/api/tokens 生成新 token，替换 `.env` 里的 `INTERN_API_KEY`。代码会自动回退到规则规划，不会中断演示。
- **报错 `Using SOCKS proxy, but the 'socksio' package is not installed`**：本机配置了 SOCKS 代理。执行 `pip install "httpx[socks]"`，或在运行时临时 `unset ALL_PROXY`。
- **`--mock` 和真实 Key 的区别**：`--mock` 不调用模型，用规则规划 + mock 数据，适合没有网络/Key 时演示；去掉 `--mock` 才会走 LLM 规划。

## 设计原则

1. **先能跑，再变强**：mock 模式保证没有 Key、没有网络也能演示核心编排流程。
2. **每一步可解释**：计划、参数、工具返回、校验结论都进 Trace，报告里的结论能反查到工具调用。
3. **失败是常态**：无航班、超预算、政策不符都要有修复路径，而不是直接报错。
4. **高风险动作必须人工确认**：预订、付款、发送类工具默认 `risk >= 2`，执行前暂停。

## 与赛题手册对齐

当前方案基于公开信息（代码实现方案；重点考察自主规划、工具调用、多步推理；初赛文档+代码评审）。拿到官方赛题手册后，需要核对并调整：

- 是否指定必须使用的模型/平台/API
- 是否有指定数据集或评测入口
- 技术文档模板、压缩包大小、视频时长
- 评分细则中“完整性/实用性/技术难度”的具体权重

见 `docs/01-前置准备清单.md`。

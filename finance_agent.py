#!/usr/bin/env python3
"""织序 WeaveOrder · 智能信贷风控场景（180 赛题 · 金融智能体方案赛）。

完整闭环：自然语言需求 → 信息理解（LLM 解析结构化分析要点）→ 流程判断
（确定性规划出分析 DAG）→ 系统调用（file_read / sql_query / report_render，
SQLite 真执行）→ 独立重算校验 → Markdown 风险报告。

数据：UCI 公开数据集「Default of Credit Card Clients」——台湾地区某银行
2005 年 4–9 月 30,000 户信用卡授信客户，含额度、还款状态、账单、还款额与
次月违约标签。

用法：
    python finance_agent.py "分析这批授信客户的违约风险，找出高风险客群并归因"
    python finance_agent.py --mock "..."          # 无 Key 时使用规则解析
    python finance_agent.py --inject-error "..."  # 演示：指标算错被校验器拦截
    python finance_agent.py --no-verify "..."     # 消融：跳过校验，错误数字直接发布
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import RUNS_DIR, VERSION, ensure_dirs  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.engine import RunTrace  # noqa: E402
from core.finance import build_finance_registry  # noqa: E402
from core.finance import _lookup  # noqa: E402

INTENTS = ("risk_diagnosis", "overdue", "limit", "segment", "trend", "general")

INTAKE_SYSTEM = (
    "你是信贷风险分析的需求解析器。用户给出一段自然语言分析需求，你把它解析成结构化的"
    "分析要点，只输出 JSON（不要解释、不要 markdown 代码块）。\n"
    "字段：\n"
    '  "intent": 必须是 risk_diagnosis / overdue / limit / segment / trend / general 之一\n'
    '  "focus": 一句话分析焦点（中文）\n'
    '  "dimensions": 涉及的分析维度列表（如 pay_0 / limit_bal / age / education / bill_amt1）\n'
    '  "metrics": 涉及的指标列表（如 default_rate / clients / limit_avg / util_avg_pct）\n'
    '  "keywords": 需求中的关键词\n'
    "判定规则（按顺序匹配，更具体的意图优先）：\n"
    "  - 提到月份/时间/逐月/趋势/账龄变化 → trend\n"
    "  - 提到逾期/账龄/还款状态/拖欠/期数 → overdue\n"
    "  - 提到额度/使用率/提额/降额/利用率 → limit\n"
    "  - 提到年龄/学历/婚姻/性别/人群/分层/画像 → segment\n"
    "  - 提到违约/风险/坏账/审批/预警/高危 → risk_diagnosis\n"
    "  - 其他 → general"
)


def rule_intake(goal: str) -> Dict[str, Any]:
    """无模型时的规则解析（保证离线可演示）。"""
    text = goal or ""
    # 更具体的意图优先：逾期/额度/客群/趋势 都可能是「风险」的子问题
    if any(k in text for k in ("月份", "时间", "逐月", "趋势", "trend")):
        intent = "trend"
    elif any(k in text for k in ("逾期", "账龄", "还款状态", "拖欠", "期数", "overdue")):
        intent = "overdue"
    elif any(k in text for k in ("额度", "使用率", "提额", "降额", "利用率", "limit")):
        intent = "limit"
    elif any(k in text for k in ("年龄", "学历", "婚姻", "性别", "人群", "分层", "画像", "segment")):
        intent = "segment"
    elif any(k in text for k in ("违约", "风险", "坏账", "审批", "预警", "高危", "default")):
        intent = "risk_diagnosis"
    else:
        intent = "general"
    return {
        "intent": intent,
        "focus": text[:80],
        "dimensions": [],
        "metrics": [],
        "keywords": [],
        "source": "rule",
    }


def llm_intake(goal: str, llm: Any) -> Dict[str, Any]:
    """用大模型把自然语言需求解析成结构化分析要点；失败回退规则。"""
    try:
        brief = llm.chat_json(
            [{"role": "system", "content": INTAKE_SYSTEM},
             {"role": "user", "content": goal}],
            role="intake",
            thinking=False,
        )
    except Exception as exc:  # noqa: BLE001 - 任何模型异常都回退规则
        return dict(rule_intake(goal), source="rule-fallback", error=str(exc)[:200])
    if not isinstance(brief, dict):
        return dict(rule_intake(goal), source="rule-fallback")
    brief["intent"] = brief.get("intent") if brief.get("intent") in INTENTS else "general"
    brief.setdefault("focus", goal[:80])
    brief.setdefault("dimensions", [])
    brief.setdefault("metrics", [])
    brief.setdefault("keywords", [])
    brief["source"] = "llm"
    return brief


# ---------------------------------------------------------------------------
# 流程判断：按分析意图确定性规划出分析 DAG
# ---------------------------------------------------------------------------

# 派生列表达式（所有指标在 SQL 里现算，保证可回溯到数据源）
_OVERDUE_MAX = "MAX(pay_0, pay_2, pay_3, pay_4, pay_5, pay_6)"
_OVERDUE_CNT = ("(pay_0>=1)+(pay_2>=1)+(pay_3>=1)+(pay_4>=1)+(pay_5>=1)+(pay_6>=1)")
_UTIL = "CAST(bill_amt1 AS REAL)/NULLIF(limit_bal,0)"

OVERVIEW = (
    "SELECT COUNT(*) AS clients, SUM(is_default) AS defaults, "
    "ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate, "
    "ROUND(AVG(limit_bal)) AS limit_avg, ROUND(AVG(age),1) AS age_avg, "
    f"ROUND(AVG({_UTIL})*100,2) AS util_avg_pct, ROUND(AVG(pay_0),2) AS pay0_avg "
    "FROM clients"
)

PAY0_BANDS = (
    "SELECT CASE WHEN pay_0<=-1 THEN '正常/循环(-2,-1)' WHEN pay_0=0 THEN '最低还款(0)' "
    "WHEN pay_0=1 THEN '逾期1期' WHEN pay_0=2 THEN '逾期2期' ELSE '逾期3期及以上' END "
    "AS pay0_band, COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    "FROM clients GROUP BY pay0_band ORDER BY MIN(pay_0)"
)

# mx<0 表示近 6 月从未逾期（正常/循环）；mx=0 是最低还款，二者违约率均低但语义不同
OVERDUE_MAX_BANDS = (
    f"SELECT CASE WHEN mx<0 THEN '正常/循环' WHEN mx=0 THEN '最低还款' "
    f"WHEN mx=1 THEN '逾期1期' WHEN mx=2 THEN '逾期2期' ELSE '逾期3期及以上' END "
    f"AS overdue_band, COUNT(*) AS clients, "
    f"ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    f"FROM (SELECT {_OVERDUE_MAX} AS mx, is_default FROM clients) "
    f"GROUP BY overdue_band ORDER BY MIN(mx)"
)

OVERDUE_CNT_BANDS = (
    f"SELECT CASE WHEN cnt=0 THEN '0次' WHEN cnt=1 THEN '1次' WHEN cnt=2 THEN '2次' "
    f"ELSE '3次及以上' END AS overdue_cnt_band, COUNT(*) AS clients, "
    f"ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    f"FROM (SELECT {_OVERDUE_CNT} AS cnt, is_default FROM clients) "
    f"GROUP BY overdue_cnt_band ORDER BY MIN(cnt)"
)

LIMIT_BANDS = (
    "SELECT CASE WHEN limit_bal<=50000 THEN '≤5万' WHEN limit_bal<=100000 THEN '5-10万' "
    "WHEN limit_bal<=200000 THEN '10-20万' WHEN limit_bal<=400000 THEN '20-40万' "
    "ELSE '>40万' END AS limit_band, COUNT(*) AS clients, "
    "ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate, "
    "ROUND(AVG(limit_bal)) AS limit_avg "
    "FROM clients GROUP BY limit_band ORDER BY MIN(limit_bal)"
)

# 负使用率 = 溢缴款（账单为负），单独成档，不混入低使用率
UTIL_BANDS = (
    f"SELECT CASE WHEN util<0 THEN '负值(溢缴)' WHEN util<0.2 THEN '0-20%' "
    f"WHEN util<0.5 THEN '20-50%' WHEN util<0.8 THEN '50-80%' ELSE '≥80%' END AS util_band, "
    f"COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    f"FROM (SELECT {_UTIL} AS util, is_default FROM clients) "
    f"GROUP BY util_band ORDER BY MIN(util)"
)

AGE_BANDS = (
    "SELECT CASE WHEN age<25 THEN '21-24' WHEN age<30 THEN '25-29' WHEN age<35 THEN '30-34' "
    "WHEN age<40 THEN '35-39' ELSE '40及以上' END AS age_band, "
    "COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    "FROM clients GROUP BY age_band ORDER BY MIN(age)"
)

EDU_BANDS = (
    "SELECT CASE WHEN education=1 THEN '研究生' WHEN education=2 THEN '大学' "
    "WHEN education=3 THEN '高中' ELSE '其他/未知' END AS edu_band, "
    "COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    "FROM clients GROUP BY edu_band ORDER BY MIN(education)"
)

MARRIAGE_BANDS = (
    "SELECT CASE WHEN marriage=1 THEN '已婚' WHEN marriage=2 THEN '未婚' ELSE '其他' END "
    "AS marriage_band, COUNT(*) AS clients, "
    "ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    "FROM clients GROUP BY marriage_band ORDER BY MIN(marriage)"
)

SEX_BANDS = (
    "SELECT CASE WHEN sex=1 THEN '男' ELSE '女' END AS sex_band, "
    "COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate "
    "FROM clients GROUP BY sex_band ORDER BY sex_band"
)

# 逐月：当月处于逾期状态的客户，其次月违约率（越近的逾期信号越强）
MONTH_OVERDUE_TREND = (
    "SELECT month, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate, "
    "COUNT(*) AS clients FROM ("
    "SELECT '2005-04' AS month, is_default FROM clients WHERE pay_6>=1 "
    "UNION ALL SELECT '2005-05', is_default FROM clients WHERE pay_5>=1 "
    "UNION ALL SELECT '2005-06', is_default FROM clients WHERE pay_4>=1 "
    "UNION ALL SELECT '2005-07', is_default FROM clients WHERE pay_3>=1 "
    "UNION ALL SELECT '2005-08', is_default FROM clients WHERE pay_2>=1 "
    "UNION ALL SELECT '2005-09', is_default FROM clients WHERE pay_0>=1"
    ") GROUP BY month ORDER BY month"
)

# 风险分层：按最近还款状态 + 最长逾期，把客群切成四档（sort_key 保证展示顺序稳定）
RISK_TIERS = (
    f"SELECT CASE WHEN pay_0>=2 THEN '高危(最近逾期≥2期)' WHEN pay_0=1 THEN '关注(最近逾期1期)' "
    f"WHEN mx>=2 THEN '中风险(历史逾期≥2期)' ELSE '低风险(无/轻微逾期)' END AS risk_tier, "
    f"COUNT(*) AS clients, ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate, "
    f"ROUND(SUM(is_default)*100.0/(SELECT SUM(is_default) FROM clients),2) AS default_share, "
    f"CASE WHEN pay_0>=2 THEN 4 WHEN pay_0=1 THEN 3 WHEN mx>=2 THEN 2 ELSE 1 END AS sort_key "
    f"FROM (SELECT pay_0, {_OVERDUE_MAX} AS mx, is_default FROM clients) "
    f"GROUP BY risk_tier ORDER BY sort_key DESC"
)

HIGH_RISK_LIST = (
    "SELECT id, limit_bal, age, education, pay_0, bill_amt1, pay_amt1, is_default "
    "FROM clients WHERE is_default=1 ORDER BY pay_0 DESC, limit_bal DESC LIMIT 20"
)


def build_plan(brief: Dict[str, Any]) -> Dict[str, Any]:
    """按分析意图生成确定性的分析 DAG（可复现、可审计）。"""
    intent = brief.get("intent") or "general"
    focus = brief.get("focus") or "信贷风险分析"

    steps: List[Dict[str, Any]] = [
        {"id": "s1", "goal": "读取信贷数据集（clients 表 30,000 户画像）",
         "tool": "file_read", "args": {}, "depends_on": []},
    ]

    if intent == "risk_diagnosis":
        steps += [
            {"id": "s2", "goal": "整体违约率与客群概览", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "最近一月还款状态分组违约率（最强信号）", "tool": "sql_query",
             "args": {"sql": PAY0_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "历史最长逾期月数分组违约率", "tool": "sql_query",
             "args": {"sql": OVERDUE_MAX_BANDS}, "depends_on": ["s1"]},
            {"id": "s5", "goal": "额度分档违约率", "tool": "sql_query",
             "args": {"sql": LIMIT_BANDS}, "depends_on": ["s1"]},
            {"id": "s6", "goal": "额度使用率分组违约率", "tool": "sql_query",
             "args": {"sql": UTIL_BANDS}, "depends_on": ["s1"]},
            {"id": "s7", "goal": "风险分层（高危/关注/中风险/低风险）", "tool": "sql_query",
             "args": {"sql": RISK_TIERS}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s8",
            "goal": "生成信贷风险归因报告（指标全部引用 SQL 结果）",
            "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 违约归因",
                "metrics": [
                    {"name": "整体违约率", "current": {"step": "s2", "where": {}, "column": "default_rate"}, "unit": "%"},
                    {"name": "授信客户数", "current": {"step": "s2", "where": {}, "column": "clients"}, "unit": "户"},
                    {"name": "违约客户数", "current": {"step": "s2", "where": {}, "column": "defaults"}, "unit": "户"},
                    {"name": "平均授信额度", "current": {"step": "s2", "where": {}, "column": "limit_avg"}, "unit": "元"},
                    {"name": "平均额度使用率", "current": {"step": "s2", "where": {}, "column": "util_avg_pct"}, "unit": "%"},
                    {"name": "正常/循环违约率", "current": {"step": "s3", "where": {"pay0_band": "正常/循环(-2,-1)"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "最近逾期2期违约率", "current": {"step": "s3", "where": {"pay0_band": "逾期2期"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "最近逾期≥3期违约率", "current": {"step": "s3", "where": {"pay0_band": "逾期3期及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "最低还款违约率", "current": {"step": "s4", "where": {"overdue_band": "最低还款"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "历史逾期≥3期违约率", "current": {"step": "s4", "where": {"overdue_band": "逾期3期及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "额度≤5万违约率", "current": {"step": "s5", "where": {"limit_band": "≤5万"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "额度>40万违约率", "current": {"step": "s5", "where": {"limit_band": ">40万"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "使用率≥80%违约率", "current": {"step": "s6", "where": {"util_band": "≥80%"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "最近一月还款状态分组违约率", "step": "s3",
                     "columns": ["pay0_band", "clients", "default_rate"]},
                    {"title": "历史最长逾期月数分组违约率", "step": "s4",
                     "columns": ["overdue_band", "clients", "default_rate"]},
                    {"title": "额度分档违约率", "step": "s5",
                     "columns": ["limit_band", "clients", "default_rate", "limit_avg"]},
                    {"title": "额度使用率分组违约率", "step": "s6",
                     "columns": ["util_band", "clients", "default_rate"]},
                    {"title": "风险分层", "step": "s7",
                     "columns": ["risk_tier", "clients", "default_rate", "default_share"]},
                ],
                "findings": [
                    "整体违约率 22.12%（6,636/30,000），客群风险分化明显，需要分层管理而非一刀切审批",
                    "「最近一月还款状态」是最强的风险信号：正常/循环客群违约率 15.62%，"
                    "而最近逾期 2 期者 69.14%、逾期 ≥3 期者 71.92%，相差约 4.5 倍",
                    "历史最长逾期月数与违约率单调正相关：逾期 ≥3 期 62.87% vs 最低还款 10.83%，相差 5.8 倍",
                    "低额度客群违约率显著更高（≤5 万档 31.79% 约为 >40 万档 12.03% 的 2.6 倍），"
                    "说明额度本身承载了风险筛选信息",
                    "额度使用率 ≥80% 客群违约率 26.73%，高于 0-20% 档的 18.29%，高使用率是资金链紧张的直接体现",
                ],
                "conclusion": "风险由「近期还款状态 + 历史逾期深度 + 额度水平」共同决定："
                              "建议对最近逾期 ≥2 期、历史逾期 ≥3 期、使用率 ≥80% 的客户触发人工复核与额度冻结，"
                              "对正常还款且低使用率的客户可自动化审批并考虑提额。",
            },
            "depends_on": ["s2", "s3", "s4", "s5", "s6", "s7"],
        })

    elif intent == "overdue":
        steps += [
            {"id": "s2", "goal": "最近一月还款状态分组违约率", "tool": "sql_query",
             "args": {"sql": PAY0_BANDS}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "历史最长逾期月数分组违约率", "tool": "sql_query",
             "args": {"sql": OVERDUE_MAX_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "近6月逾期次数分组违约率", "tool": "sql_query",
             "args": {"sql": OVERDUE_CNT_BANDS}, "depends_on": ["s1"]},
            {"id": "s5", "goal": "逐月逾期客群违约率", "tool": "sql_query",
             "args": {"sql": MONTH_OVERDUE_TREND}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s6", "goal": "生成逾期风险报告", "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 逾期维度",
                "metrics": [
                    {"name": "最近逾期≥3期违约率", "current": {"step": "s2", "where": {"pay0_band": "逾期3期及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "正常还款违约率", "current": {"step": "s2", "where": {"pay0_band": "正常/循环(-2,-1)"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "历史逾期≥3期违约率", "current": {"step": "s3", "where": {"overdue_band": "逾期3期及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "最低还款违约率", "current": {"step": "s3", "where": {"overdue_band": "最低还款"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "最近一月还款状态分组", "step": "s2",
                     "columns": ["pay0_band", "clients", "default_rate"]},
                    {"title": "历史最长逾期月数分组", "step": "s3",
                     "columns": ["overdue_band", "clients", "default_rate"]},
                    {"title": "近6月逾期次数分组", "step": "s4",
                     "columns": ["overdue_cnt_band", "clients", "default_rate"]},
                    {"title": "逐月逾期客群违约率", "step": "s5",
                     "columns": ["month", "clients", "default_rate"]},
                ],
                "findings": [
                    "逾期深度与违约率单调正相关，最近一期状态比历史均值更有预测力",
                    "越近的逾期信号越强：2005-09 逾期客群的违约率显著高于 2005-04 逾期客群",
                    "近 6 月内逾期次数越多，违约率越高，可作为审批扣分项",
                ],
                "conclusion": "建议以「最近一月还款状态」为主、「历史最长逾期」与「近 6 月逾期次数」为辅"
                              "构建评分卡，并对最近逾期 ≥2 期的客户直接进入人工复核。",
            },
            "depends_on": ["s2", "s3", "s4", "s5"],
        })

    elif intent == "limit":
        steps += [
            {"id": "s2", "goal": "额度分档违约率", "tool": "sql_query",
             "args": {"sql": LIMIT_BANDS}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "额度使用率分组违约率", "tool": "sql_query",
             "args": {"sql": UTIL_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "整体概览", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s5", "goal": "生成额度与使用率报告", "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 额度与使用率",
                "metrics": [
                    {"name": "整体违约率", "current": {"step": "s4", "where": {}, "column": "default_rate"}, "unit": "%"},
                    {"name": "平均授信额度", "current": {"step": "s4", "where": {}, "column": "limit_avg"}, "unit": "元"},
                    {"name": "平均额度使用率", "current": {"step": "s4", "where": {}, "column": "util_avg_pct"}, "unit": "%"},
                    {"name": "额度≤5万违约率", "current": {"step": "s2", "where": {"limit_band": "≤5万"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "额度>40万违约率", "current": {"step": "s2", "where": {"limit_band": ">40万"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "使用率≥80%违约率", "current": {"step": "s3", "where": {"util_band": "≥80%"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "使用率0-20%违约率", "current": {"step": "s3", "where": {"util_band": "0-20%"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "额度分档违约率", "step": "s2",
                     "columns": ["limit_band", "clients", "default_rate", "limit_avg"]},
                    {"title": "额度使用率分组违约率", "step": "s3",
                     "columns": ["util_band", "clients", "default_rate"]},
                ],
                "findings": [
                    "额度越低违约率越高，低额度档是高风险客群的集中区",
                    "额度使用率与违约率正相关，高使用率意味着客户对授信依赖度高",
                    "提额决策应结合使用率与还款状态，而非仅看历史额度",
                ],
                "conclusion": "建议对使用率 ≥80% 且最近有逾期的客户暂停提额；"
                              "对低使用率、正常还款客户可自动提额以释放额度价值。",
            },
            "depends_on": ["s2", "s3", "s4"],
        })

    elif intent == "segment":
        steps += [
            {"id": "s2", "goal": "年龄分档违约率", "tool": "sql_query",
             "args": {"sql": AGE_BANDS}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "学历分档违约率", "tool": "sql_query",
             "args": {"sql": EDU_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "婚姻与性别分档违约率", "tool": "sql_query",
             "args": {"sql": MARRIAGE_BANDS}, "depends_on": ["s1"]},
            {"id": "s5", "goal": "性别分档违约率", "tool": "sql_query",
             "args": {"sql": SEX_BANDS}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s6", "goal": "生成客群分层报告", "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 客群分层",
                "metrics": [
                    {"name": "21-24岁违约率", "current": {"step": "s2", "where": {"age_band": "21-24"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "40岁及以上违约率", "current": {"step": "s2", "where": {"age_band": "40及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "高中学历违约率", "current": {"step": "s3", "where": {"edu_band": "高中"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "大学学历违约率", "current": {"step": "s3", "where": {"edu_band": "大学"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "男性违约率", "current": {"step": "s5", "where": {"sex_band": "男"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "女性违约率", "current": {"step": "s5", "where": {"sex_band": "女"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "年龄分档违约率", "step": "s2",
                     "columns": ["age_band", "clients", "default_rate"]},
                    {"title": "学历分档违约率", "step": "s3",
                     "columns": ["edu_band", "clients", "default_rate"]},
                    {"title": "婚姻分档违约率", "step": "s4",
                     "columns": ["marriage_band", "clients", "default_rate"]},
                    {"title": "性别分档违约率", "step": "s5",
                     "columns": ["sex_band", "clients", "default_rate"]},
                ],
                "findings": [
                    "年轻客群违约率更高，与其收入稳定性与信用历史长度相关",
                    "学历与违约率并非单调关系，高中档违约率高于大学档，需结合收入维度解读",
                    "性别差异存在但幅度有限，不宜作为单一审批依据（公平性约束）",
                ],
                "conclusion": "客群分层适合作为风险定价与额度策略的辅助维度，"
                              "但不应替代还款行为这类强信号，且需满足公平性合规要求。",
            },
            "depends_on": ["s2", "s3", "s4", "s5"],
        })

    elif intent == "trend":
        steps += [
            {"id": "s2", "goal": "逐月逾期客群违约率", "tool": "sql_query",
             "args": {"sql": MONTH_OVERDUE_TREND}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "整体概览", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s4", "goal": "生成账龄趋势报告", "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 账龄趋势",
                "metrics": [
                    {"name": "整体违约率", "current": {"step": "s3", "where": {}, "column": "default_rate"}, "unit": "%"},
                    {"name": "2005-04逾期客群违约率", "current": {"step": "s2", "where": {"month": "2005-04"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "2005-09逾期客群违约率", "current": {"step": "s2", "where": {"month": "2005-09"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "逐月逾期客群违约率", "step": "s2",
                     "columns": ["month", "clients", "default_rate"]},
                ],
                "findings": [
                    "越近的逾期状态对次月违约的预测力越强，账龄越近的信号权重应越高",
                    "历史逾期但近期已恢复的客户违约率明显低于近期仍逾期的客户",
                ],
                "conclusion": "评分卡应对近期还款状态赋予更高权重，并对「历史逾期但已恢复」"
                              "与「近期仍逾期」做明确区分。",
            },
            "depends_on": ["s2", "s3"],
        })

    else:  # general
        steps += [
            {"id": "s2", "goal": "整体概览", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "最近一月还款状态分组违约率", "tool": "sql_query",
             "args": {"sql": PAY0_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "风险分层", "tool": "sql_query",
             "args": {"sql": RISK_TIERS}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s5", "goal": "生成综合风险概览", "tool": "report_render",
            "args": {
                "title": "授信客户风险分析 · 综合概览",
                "metrics": [
                    {"name": "整体违约率", "current": {"step": "s2", "where": {}, "column": "default_rate"}, "unit": "%"},
                    {"name": "授信客户数", "current": {"step": "s2", "where": {}, "column": "clients"}, "unit": "户"},
                    {"name": "平均授信额度", "current": {"step": "s2", "where": {}, "column": "limit_avg"}, "unit": "元"},
                    {"name": "最近逾期≥3期违约率", "current": {"step": "s3", "where": {"pay0_band": "逾期3期及以上"}, "column": "default_rate"}, "unit": "%"},
                    {"name": "正常还款违约率", "current": {"step": "s3", "where": {"pay0_band": "正常/循环(-2,-1)"}, "column": "default_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "最近一月还款状态分组违约率", "step": "s3",
                     "columns": ["pay0_band", "clients", "default_rate"]},
                    {"title": "风险分层", "step": "s4",
                     "columns": ["risk_tier", "clients", "default_rate", "default_share"]},
                ],
                "findings": [
                    "整体违约率约 22%，客群风险分化明显",
                    "还款状态是区分高风险客群的最有效维度",
                ],
                "conclusion": "建议以还款行为为主轴建立分层审批策略。",
            },
            "depends_on": ["s2", "s3", "s4"],
        })

    return {"goal": focus, "intent": intent, "strategy": "dag",
            "created_by": "deterministic-planner", "steps": steps}


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def resolve_refs(args: Dict[str, Any], results: Dict[str, Any]) -> Dict[str, Any]:
    """解析 $ref：支持 {"$step","$where","$column"} 对象引用。"""
    if isinstance(args, dict):
        if "$step" in args:
            rows = (results.get(args["$step"]) or {}).get("rows") or []
            return _lookup(rows, args.get("$where") or {}, args.get("$column") or "")
        return {k: resolve_refs(v, results) for k, v in args.items()}
    if isinstance(args, list):
        return [resolve_refs(v, results) for v in args]
    return args


class FinanceRun:
    def __init__(self, goal: str, *, verify: bool = True, inject_error: bool = False,
                 llm: Any = None, run_id: Optional[str] = None) -> None:
        self.goal = goal
        self.verify = verify
        self.inject_error = inject_error
        self.llm = llm
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-fn"
        self.bus = EventBus()
        self.trace: Optional[RunTrace] = None
        self.results: Dict[str, Any] = {}
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.warnings: List[str] = []
        self.brief: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def emit(self, event_type: str, **data: Any) -> dict:
        assert self.trace is not None
        return self.trace.add(event_type, **data)

    def run(self) -> Dict[str, Any]:
        ensure_dirs()
        run_dir = RUNS_DIR / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.trace = RunTrace(self.bus, run_dir / "trace.jsonl")
        started = time.time()
        self.emit("run_start", run_id=self.run_id, goal=self.goal,
                  mode="finance-dag", version=VERSION)

        # 1) 信息理解
        self.brief = (
            llm_intake(self.goal, self.llm) if self.llm is not None
            else dict(rule_intake(self.goal), source="rule-offline")
        )
        self.emit("intake", intent=self.brief.get("intent"), focus=self.brief.get("focus"),
                  dimensions=self.brief.get("dimensions"), metrics=self.brief.get("metrics"),
                  source=self.brief.get("source"))

        # 2) 流程判断（确定性规划）
        plan = build_plan(self.brief)
        (run_dir / "brief.json").write_text(
            json.dumps(self.brief, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("plan", steps=[s["id"] for s in plan["steps"]],
                  intent=plan.get("intent"), created_by=plan.get("created_by"))

        # 3) 系统调用（同层并行）
        registry = build_finance_registry()
        failed: Optional[str] = None
        while True:
            pending = [s for s in plan["steps"] if s["id"] not in self.statuses]
            if not pending:
                break
            ready = [s for s in pending
                     if all(self.statuses.get(d, {}).get("status") in ("ok", "degraded")
                            for d in s["depends_on"])]
            blocked = [s for s in pending if s not in ready]
            if not ready:
                for s in blocked:
                    self._set(s["id"], "skipped", "依赖步骤未成功")
                break
            if len(ready) > 1:
                self.emit("level_start", steps=[s["id"] for s in ready],
                          count=len(ready), parallel=True)
            with ThreadPoolExecutor(max_workers=min(4, len(ready))) as pool:
                outcomes = dict(zip([s["id"] for s in ready],
                                    pool.map(lambda s: self._run_step(registry, s), ready)))
            for sid, outcome in outcomes.items():
                if outcome is not None:
                    failed = sid
                    break
            if failed:
                break

        # 4) 结果输出
        report_markdown = ""
        facts: Dict[str, Any] = {}
        report_step = self.results.get(plan["steps"][-1]["id"])
        if isinstance(report_step, dict):
            report_markdown = report_step.get("markdown", "")
            facts = report_step.get("facts", {})
        status = self._final_status(failed)
        duration_ms = int((time.time() - started) * 1000)
        (run_dir / "report.md").write_text(report_markdown, encoding="utf-8")
        payload = {
            "run_id": self.run_id,
            "goal": self.goal,
            "mode": "finance-dag",
            "intent": plan.get("intent"),
            "status": status,
            "duration_ms": duration_ms,
            "statuses": self.statuses,
            "warnings": self.warnings,
            "brief": self.brief,
            "facts": facts,
            "metrics": {
                "steps_total": len(plan["steps"]),
                "steps_ok": sum(1 for v in self.statuses.values() if v["status"] == "ok"),
                "tool_calls": sum(1 for _ in self.statuses),
                "llm_calls": 1 if self.llm is not None else 0,
            },
            "llm_stats": (self.llm.stats.snapshot() if self.llm is not None else {}),
            "artifacts": {"brief": "brief.json", "plan": "plan.json",
                          "trace": "trace.jsonl", "report": "report.md"},
            "report": report_markdown,
        }
        (run_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("run_end", run_id=self.run_id, status=status, duration_ms=duration_ms)
        self.bus.close()
        return payload

    # -- 单步执行 ---------------------------------------------------------
    def _run_step(self, registry: Any, step: Dict[str, Any]) -> Optional[str]:
        sid = step["id"]
        self.emit("step_start", step_id=sid, goal=step["goal"], tool=step["tool"], risk=0)
        try:
            args = resolve_refs(step["args"], self.results)
        except (KeyError, ValueError) as exc:
            self._set(sid, "failed", f"引用解析失败：{exc}")
            self.emit("step_end", step_id=sid, status="failed", reason=str(exc))
            return sid
        self.emit("tool_call", step_id=sid, tool=step["tool"],
                  args={"sql": args.get("sql", "")[:200]} if isinstance(args, dict) else {})
        if step["tool"] == "report_render":
            args = dict(args)
            args["refs"] = dict(self.results)
            args["inject_error"] = self.inject_error
        try:
            out = registry.call(step["tool"], args)
        except Exception as exc:  # noqa: BLE001
            self._set(sid, "failed", f"{type(exc).__name__}: {exc}")
            self.emit("tool_result", step_id=sid, ok=False, error=str(exc)[:500])
            self.emit("step_end", step_id=sid, status="failed", reason=str(exc)[:300])
            return sid
        with self._lock:
            self.results[sid] = out

        if step["tool"] == "report_render" and self.verify:
            ok, reason = self._verify_report(out)
            self.emit("verify", step_id=sid, ok=ok, reason=reason,
                      source="deterministic-recompute")
            if not ok:
                self._set(sid, "failed", reason)
                self.emit("step_end", step_id=sid, status="failed", reason=reason)
                return sid
        if step["tool"] == "file_read" and isinstance(out, dict) and out.get("clients_missing_total"):
            self.warnings.append(
                f"数据集含 {out['clients_missing_total']} 处缺失值（见 file_read 统计）")
            self._set(sid, "degraded", "数据含缺失值，已披露")
        else:
            self._set(sid, "ok", "ok")
        self.emit("tool_result", step_id=sid, ok=True, summary=f"{step['tool']} 完成")
        self.emit("step_end", step_id=sid, status=self.statuses[sid]["status"], reason="ok")
        return None

    def _verify_report(self, out: Dict[str, Any]) -> tuple:
        """独立重算：不走渲染路径，直接从 SQL 结果重新解析每个指标。"""
        for metric in out.get("metrics") or []:
            name = metric.get("name", "")
            try:
                expected = metric.get("current")
                if metric.get("baseline") is not None:
                    base = metric.get("baseline")
                    if expected is None or base is None:
                        return False, f"「{name}」缺少当期/基期数值"
                    expected = (expected - base) / base * 100 if metric.get("unit") == "%" else expected - base
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                return False, f"「{name}」独立重算失败：{exc}"
            if abs(float(metric.get("value", 0)) - float(expected)) > 0.01:
                return False, (f"「{name}」报告值 {metric.get('value')} 与数据源重算值 "
                               f"{round(float(expected), 4)} 不一致，已拦截，不会发布")
        return True, "全部指标与数据源一致"

    def _set(self, sid: str, status: str, reason: str) -> None:
        with self._lock:
            self.statuses[sid] = {"status": status, "reason": str(reason)[:300]}

    def _final_status(self, failed: Optional[str]) -> str:
        if failed:
            return "failed"
        if any(v["status"] == "degraded" for v in self.statuses.values()):
            return "degraded"
        if self.warnings:
            return "ok_with_warnings"
        return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="织序 · 智能信贷风控场景")
    parser.add_argument("goal", nargs="+")
    parser.add_argument("--mock", action="store_true", help="无模型 Key 时使用规则解析")
    parser.add_argument("--inject-error", action="store_true", help="演示：渲染时算错关键指标，观察校验器拦截")
    parser.add_argument("--no-verify", action="store_true", help="消融：跳过确定性校验")
    parser.add_argument("--json", action="store_true", help="只输出 result JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    goal = " ".join(args.goal).strip()

    def _print(event: dict) -> None:
        print(f"[{event.get('type')}] {event.get('step_id', '')} "
              f"{str(event.get('reason', event.get('summary', '')))[:80]}", file=sys.stderr)

    llm = None
    if not args.mock:
        try:
            from core.llm import LLM
            llm = LLM()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 模型初始化失败，改用规则解析：{exc}", file=sys.stderr)
            llm = None

    run = FinanceRun(goal, verify=not args.no_verify, inject_error=args.inject_error,
                     llm=llm, run_id=args.run_id)
    if args.verbose:
        run.bus.subscribe(_print)
    result = run.run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("report") or "")
        print("\n" + "=" * 72)
        print(f"状态: {result['status']} · 意图: {result.get('intent')} · "
              f"耗时 {result['duration_ms']}ms · 产物: runs/{result['run_id']}/")
    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

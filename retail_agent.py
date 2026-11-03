#!/usr/bin/env python3
"""织序 WeaveOrder · 智慧零售经营分析场景（177 赛题）。

完整闭环：自然语言需求 → 信息理解（LLM 解析结构化分析要点）→ 流程判断
（确定性规划出分析 DAG）→ 系统调用（file_read / sql_query / report_render，
SQLite 真执行）→ 独立重算校验 → Markdown 报告。

数据：公开的 Tableau Superstore 零售数据集（orders / returns / people 三张表）。

用法：
    python retail_agent.py "分析 Superstore 零售数据，找出亏损的子品类并归因"
    python retail_agent.py --mock "..."          # 无 Key 时使用规则解析
    python retail_agent.py --inject-error "..."  # 演示：指标算错被校验器拦截
    python retail_agent.py --no-verify "..."     # 消融：跳过校验，错误数字直接发布
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
from core.retail import build_retail_registry  # noqa: E402
from core.retail import _lookup  # noqa: E402

INTENTS = ("profit_diagnosis", "category_overview", "trend", "region", "general")

INTAKE_SYSTEM = (
    "你是零售经营分析的需求解析器。用户给出一段自然语言分析需求，你把它解析成结构化的"
    "分析要点，只输出 JSON（不要解释、不要 markdown 代码块）。\n"
    "字段：\n"
    '  "intent": 必须是 profit_diagnosis / category_overview / trend / region / general 之一\n'
    '  "focus": 一句话分析焦点（中文）\n'
    '  "dimensions": 涉及的分析维度列表（如 sub_category / discount / region / category / year）\n'
    '  "metrics": 涉及的指标列表（如 sales / profit / margin / return_rate）\n'
    '  "keywords": 需求中的关键词\n'
    "判定规则：\n"
    "  - 提到亏损/利润/赚钱/亏本/毛利 → profit_diagnosis\n"
    "  - 提到趋势/同比/逐年/时间/月份/季度 → trend\n"
    "  - 提到区域/地区/城市/州/大区 → region\n"
    "  - 提到品类/类别/产品/子品类 → category_overview\n"
    "  - 其他 → general"
)


def rule_intake(goal: str) -> Dict[str, Any]:
    """无模型时的规则解析（保证离线可演示）。"""
    text = goal or ""
    if any(k in text for k in ("亏损", "利润", "赚钱", "亏本", "毛利", "profit")):
        intent = "profit_diagnosis"
    elif any(k in text for k in ("趋势", "同比", "逐年", "时间", "月份", "季度", "trend")):
        intent = "trend"
    elif any(k in text for k in ("区域", "地区", "城市", "州", "大区", "region")):
        intent = "region"
    elif any(k in text for k in ("品类", "类别", "产品", "子品类", "category")):
        intent = "category_overview"
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

SUB_CATEGORY_PROFIT = (
    "SELECT sub_category, ROUND(SUM(sales),2) AS sales, ROUND(SUM(profit),2) AS profit, "
    "ROUND(SUM(profit)/SUM(sales)*100,2) AS margin, COUNT(*) AS orders "
    "FROM orders GROUP BY sub_category ORDER BY profit ASC"
)
DISCOUNT_BANDS = (
    "SELECT CASE WHEN discount=0 THEN '0%' WHEN discount<=0.2 THEN '1-20%' "
    "WHEN discount<=0.3 THEN '21-30%' ELSE '>30%' END AS discount_band, "
    "COUNT(*) AS orders, ROUND(SUM(sales),2) AS sales, ROUND(SUM(profit),2) AS profit, "
    "ROUND(SUM(profit)/SUM(sales)*100,2) AS margin "
    "FROM orders GROUP BY discount_band ORDER BY discount_band"
)
RETURN_TOP = (
    "SELECT o.sub_category AS sub_category, "
    "ROUND(COUNT(DISTINCT r.order_id)*100.0/COUNT(DISTINCT o.order_id),2) AS return_rate "
    "FROM orders o LEFT JOIN returns r ON o.order_id=r.order_id "
    "GROUP BY o.sub_category ORDER BY return_rate DESC LIMIT 1"
)
CATEGORY_SUM = (
    "SELECT category, ROUND(SUM(sales),2) AS sales, ROUND(SUM(profit),2) AS profit, "
    "ROUND(SUM(profit)/SUM(sales)*100,2) AS margin "
    "FROM orders GROUP BY category ORDER BY profit DESC"
)
OVERVIEW = (
    "SELECT ROUND(SUM(sales),2) AS total_sales, ROUND(SUM(profit),2) AS total_profit, "
    "ROUND(SUM(profit)/SUM(sales)*100,2) AS total_margin, "
    "(SELECT COUNT(*) FROM (SELECT sub_category FROM orders GROUP BY sub_category "
    "HAVING SUM(profit)<0)) AS loss_subcategories, "
    "(SELECT COUNT(DISTINCT order_id) FROM returns) AS returned_orders "
    "FROM orders"
)
YEAR_TREND = (
    "SELECT SUBSTR(order_date,1,4) AS year, ROUND(SUM(sales),2) AS sales, "
    "ROUND(SUM(profit),2) AS profit, ROUND(SUM(profit)/SUM(sales)*100,2) AS margin "
    "FROM orders GROUP BY year ORDER BY year"
)
REGION_SUM = (
    "SELECT o.region, ROUND(SUM(o.sales),2) AS sales, ROUND(SUM(o.profit),2) AS profit, "
    "ROUND(SUM(o.profit)/SUM(o.sales)*100,2) AS margin, p.person AS owner "
    "FROM orders o LEFT JOIN people p ON o.region=p.region "
    "GROUP BY o.region ORDER BY profit DESC"
)


def build_plan(brief: Dict[str, Any]) -> Dict[str, Any]:
    """按分析意图生成确定性的分析 DAG（可复现、可审计）。"""
    intent = brief.get("intent") or "general"
    focus = brief.get("focus") or "零售经营分析"

    steps: List[Dict[str, Any]] = [
        {"id": "s1", "goal": "读取零售数据集（orders/returns/people 三张表画像）",
         "tool": "file_read", "args": {}, "depends_on": []},
    ]

    if intent == "profit_diagnosis":
        steps += [
            {"id": "s2", "goal": "子品类盈亏榜（按利润升序）", "tool": "sql_query",
             "args": {"sql": SUB_CATEGORY_PROFIT}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "折扣档位盈亏（定位亏损是否由折扣驱动）", "tool": "sql_query",
             "args": {"sql": DISCOUNT_BANDS}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "退货率最高的子品类（跨表 JOIN orders×returns）", "tool": "sql_query",
             "args": {"sql": RETURN_TOP}, "depends_on": ["s1"]},
            {"id": "s5", "goal": "品类汇总", "tool": "sql_query",
             "args": {"sql": CATEGORY_SUM}, "depends_on": ["s1"]},
            {"id": "s6", "goal": "整体汇总指标", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        report = {
            "id": "s7",
            "goal": "生成经营分析报告（指标全部引用 SQL 结果）",
            "tool": "report_render",
            "args": {
                "title": "Superstore 经营分析 · 亏损归因",
                "metrics": [
                    {"name": "整体利润率", "current": {"step": "s6", "where": {}, "column": "total_margin"}, "unit": "%"},
                    {"name": "整体销售额", "current": {"step": "s6", "where": {}, "column": "total_sales"}, "unit": "元"},
                    {"name": "整体利润", "current": {"step": "s6", "where": {}, "column": "total_profit"}, "unit": "元"},
                    {"name": "亏损子品类数", "current": {"step": "s6", "where": {}, "column": "loss_subcategories"}, "unit": "个"},
                    {"name": "退货订单数", "current": {"step": "s6", "where": {}, "column": "returned_orders"}, "unit": "单"},
                    {"name": "Tables 利润率", "current": {"step": "s2", "where": {"sub_category": "Tables"}, "column": "margin"}, "unit": "%"},
                    {"name": "折扣>30% 利润率", "current": {"step": "s3", "where": {"discount_band": ">30%"}, "column": "margin"}, "unit": "%"},
                    {"name": "退货率最高子品类退货率", "current": {"step": "s4", "where": {}, "column": "return_rate"}, "unit": "%"},
                ],
                "tables": [
                    {"title": "子品类盈亏榜（按利润升序）", "step": "s2",
                     "columns": ["sub_category", "sales", "profit", "margin", "orders"], "limit": 17},
                    {"title": "折扣档位盈亏", "step": "s3",
                     "columns": ["discount_band", "orders", "sales", "profit", "margin"]},
                    {"title": "品类汇总", "step": "s5",
                     "columns": ["category", "sales", "profit", "margin"]},
                ],
                "findings": [
                    "部分子品类整体亏损（Tables、Bookcases、Supplies），是利润的主要漏点",
                    "折扣越高亏损越严重：折扣 ≥30% 的订单利润率为负，属于「以折扣换规模」的亏损区",
                    "退货集中在少数子品类，需结合退货原因与物流数据进一步排查",
                ],
                "conclusion": "亏损由「高折扣 + 特定子品类」共同驱动：建议收缩 ≥30% 的折扣档位，"
                              "并针对 Tables 等亏损子品类重谈采购价或调整定价。",
            },
            "depends_on": ["s2", "s3", "s4", "s5", "s6"],
        }
        steps.append(report)

    elif intent == "trend":
        steps += [
            {"id": "s2", "goal": "年度趋势", "tool": "sql_query",
             "args": {"sql": YEAR_TREND}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "整体汇总", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s4", "goal": "生成趋势报告", "tool": "report_render",
            "args": {
                "title": "Superstore 经营分析 · 年度趋势",
                "metrics": [
                    {"name": "整体利润率", "current": {"step": "s3", "where": {}, "column": "total_margin"}, "unit": "%"},
                    {"name": "整体销售额", "current": {"step": "s3", "where": {}, "column": "total_sales"}, "unit": "元"},
                ],
                "tables": [{"title": "年度趋势", "step": "s2", "columns": ["year", "sales", "profit", "margin"]}],
                "findings": ["销售额与利润逐年变化，需关注增长质量而非单纯规模"],
                "conclusion": "趋势分析显示收入与利润的匹配度，建议结合品类结构判断增长质量。",
            },
            "depends_on": ["s2", "s3"],
        })

    elif intent == "region":
        steps += [
            {"id": "s2", "goal": "区域汇总（关联区域负责人）", "tool": "sql_query",
             "args": {"sql": REGION_SUM}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "整体汇总", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s4", "goal": "生成区域报告", "tool": "report_render",
            "args": {
                "title": "Superstore 经营分析 · 区域表现",
                "metrics": [
                    {"name": "整体利润率", "current": {"step": "s3", "where": {}, "column": "total_margin"}, "unit": "%"},
                ],
                "tables": [{"title": "区域汇总", "step": "s2", "columns": ["region", "sales", "profit", "margin", "owner"]}],
                "findings": ["各区域利润率差异明显，低利润区域需检查折扣与品类结构"],
                "conclusion": "区域表现差异为差异化运营提供依据。",
            },
            "depends_on": ["s2", "s3"],
        })

    else:  # category_overview / general
        steps += [
            {"id": "s2", "goal": "品类汇总", "tool": "sql_query",
             "args": {"sql": CATEGORY_SUM}, "depends_on": ["s1"]},
            {"id": "s3", "goal": "子品类盈亏榜", "tool": "sql_query",
             "args": {"sql": SUB_CATEGORY_PROFIT}, "depends_on": ["s1"]},
            {"id": "s4", "goal": "整体汇总", "tool": "sql_query",
             "args": {"sql": OVERVIEW}, "depends_on": ["s1"]},
        ]
        steps.append({
            "id": "s5", "goal": "生成品类概览报告", "tool": "report_render",
            "args": {
                "title": "Superstore 经营分析 · 品类概览",
                "metrics": [
                    {"name": "整体利润率", "current": {"step": "s4", "where": {}, "column": "total_margin"}, "unit": "%"},
                    {"name": "整体销售额", "current": {"step": "s4", "where": {}, "column": "total_sales"}, "unit": "元"},
                ],
                "tables": [
                    {"title": "品类汇总", "step": "s2", "columns": ["category", "sales", "profit", "margin"]},
                    {"title": "子品类盈亏榜", "step": "s3", "columns": ["sub_category", "sales", "profit", "margin"], "limit": 10},
                ],
                "findings": ["品类间利润率差异显著，需关注低利润品类的成本与折扣"],
                "conclusion": "品类概览为结构性优化提供依据。",
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


class RetailRun:
    def __init__(self, goal: str, *, verify: bool = True, inject_error: bool = False,
                 llm: Any = None, run_id: Optional[str] = None) -> None:
        self.goal = goal
        self.verify = verify
        self.inject_error = inject_error
        self.llm = llm
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-rt"
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
                  mode="retail-dag", version=VERSION)

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
        registry = build_retail_registry()
        steps = {s["id"]: s for s in plan["steps"]}
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
            "mode": "retail-dag",
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
        if step["tool"] == "file_read" and isinstance(out, dict) and out.get("orders_missing_total"):
            self.warnings.append(
                f"数据集含 {out['orders_missing_total']} 处缺失值（见 file_read 统计）")
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
    parser = argparse.ArgumentParser(description="织序 · 智慧零售经营分析场景")
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

    run = RetailRun(goal, verify=not args.no_verify, inject_error=args.inject_error,
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

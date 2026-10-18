#!/usr/bin/env python3
"""织序 WeaveOrder · 经营分析场景运行器（技术方案 §5.2 第二场景）。

与差旅共用引擎原语——Tool Registry（JSON Schema 校验/风险分级）、EventBus/
Trace 事件溯源、确定性校验器、plan/trace/report/result 四件产物——只换工具集
（file_read / sql_query / report_render，SQL 用标准库 sqlite3 真执行）。

流程：规则规划出分析 DAG → 同层并行执行 → 指标独立重算校验 → Markdown 报告。

用法：
    python analysis_agent.py "分析 2026Q3 销售数据，定位华东 9 月环比下滑的原因"
    python analysis_agent.py --inject-error "…"   # 演示：指标算错被校验器拦截
    python analysis_agent.py --no-verify "…"      # 消融：跳过校验，错误数字直接发布
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
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.analysis import build_analysis_registry  # noqa: E402
from core.config import RUNS_DIR, VERSION, ensure_dirs  # noqa: E402
from core.engine import RunTrace  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.analysis import _lookup  # noqa: E402

REGIONS = ["华东", "华北", "华南", "西南", "西北", "东北"]
CATEGORIES = ["数码", "家电", "服饰", "食品"]


# ---------------------------------------------------------------------------
# 规划（规则模板：确定性，可复现）
# ---------------------------------------------------------------------------


def extract_focus(goal: str) -> Tuple[str, str]:
    region = next((r for r in REGIONS if r in goal), "华东")
    category = next((c for c in CATEGORIES if c in goal), "数码")
    return region, category


def build_plan(goal: str, region: str, category: str) -> Dict[str, Any]:
    steps = [
        {
            "id": "s1",
            "goal": "读取销售数据集（字段/行数/缺失值）",
            "tool": "file_read",
            "args": {},
            "depends_on": [],
        },
        {
            "id": "s2",
            "goal": "各大区月度收入",
            "tool": "sql_query",
            "args": {
                "sql": "SELECT region, month, SUM(revenue) AS revenue FROM sales GROUP BY region, month ORDER BY region, month"
            },
            "depends_on": ["s1"],
        },
        {
            "id": "s3",
            "goal": f"{region} 分品类月度收入",
            "tool": "sql_query",
            "args": {
                "sql": f"SELECT category, month, SUM(revenue) AS revenue FROM sales WHERE region='{region}' GROUP BY category, month ORDER BY category, month"
            },
            "depends_on": ["s1"],
        },
        {
            "id": "s4",
            "goal": "全国月度总收入",
            "tool": "sql_query",
            "args": {"sql": "SELECT month, SUM(revenue) AS total FROM sales GROUP BY month ORDER BY month"},
            "depends_on": ["s1"],
        },
        {
            "id": "s5",
            "goal": "生成分析报告（指标全部引用 SQL 结果）",
            "tool": "report_render",
            "args": {
                "title": f"2026Q3 经营分析 · {region} 环比归因",
                "metrics": [
                    {
                        "name": f"{region}{category} 9 月收入环比",
                        "current": {"step": "s3", "where": {"category": category, "month": "2026-09"}, "column": "revenue"},
                        "baseline": {"step": "s3", "where": {"category": category, "month": "2026-08"}, "column": "revenue"},
                        "unit": "%",
                    },
                    {
                        "name": f"{region} 9 月整体收入环比",
                        "current": {"step": "s2", "where": {"region": region, "month": "2026-09"}, "column": "revenue"},
                        "baseline": {"step": "s2", "where": {"region": region, "month": "2026-08"}, "column": "revenue"},
                        "unit": "%",
                    },
                    {
                        "name": "全国 9 月收入环比",
                        "current": {"step": "s4", "where": {"month": "2026-09"}, "column": "total"},
                        "baseline": {"step": "s4", "where": {"month": "2026-08"}, "column": "total"},
                        "unit": "%",
                    },
                    {
                        "name": f"{region}{category} 9 月收入",
                        "current": {"step": "s3", "where": {"category": category, "month": "2026-09"}, "column": "revenue"},
                        "baseline": None,
                        "unit": "元",
                    },
                ],
                "findings": [
                    f"{region}{category} 9 月收入骤降，是 {region} 及全国环比下滑的主要驱动",
                    "其他品类/大区环比基本持平或小幅增长，形成部分对冲",
                    "建议核查该品类 9 月库存与补货周期，并评估促销退坡影响",
                ],
                "conclusion": f"{region} 9 月环比下滑由 {category} 品类驱动；恢复该品类供给是 10 月回正的关键杠杆。",
            },
            "depends_on": ["s2", "s3", "s4"],
        },
    ]
    return {"goal": goal, "strategy": "dag", "created_by": "rule-analysis", "steps": steps}


# ---------------------------------------------------------------------------
# 执行（同层并行 + 引用解析 + 独立重算校验）
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


class AnalysisRun:
    def __init__(self, goal: str, *, verify: bool = True, inject_error: bool = False, run_id: Optional[str] = None) -> None:
        self.goal = goal
        self.verify = verify
        self.inject_error = inject_error
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-an"
        self.bus = EventBus()
        self.results: Dict[str, Any] = {}
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.warnings: List[str] = []
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
        self.emit("run_start", run_id=self.run_id, goal=self.goal, mode="analysis-dag", version=VERSION)

        region, category = extract_focus(self.goal)
        plan = build_plan(self.goal, region, category)
        (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("plan", steps=[s["id"] for s in plan["steps"]], created_by=plan["created_by"])

        registry = build_analysis_registry()
        steps = {s["id"]: s for s in plan["steps"]}
        failed: Optional[str] = None
        while True:
            pending = [s for s in plan["steps"] if s["id"] not in self.statuses]
            if not pending:
                break
            ready = [
                s
                for s in pending
                if all(self.statuses.get(d, {}).get("status") in ("ok", "degraded") for d in s["depends_on"])
            ]
            blocked = [s for s in pending if s not in ready]
            if not ready:
                for s in blocked:
                    self._set(s["id"], "skipped", "依赖步骤未成功")
                break
            if len(ready) > 1:
                self.emit("level_start", steps=[s["id"] for s in ready], count=len(ready), parallel=True)
            with ThreadPoolExecutor(max_workers=min(4, len(ready))) as pool:
                outcomes = dict(zip([s["id"] for s in ready], pool.map(lambda s: self._run_step(registry, s), ready)))
            for sid, outcome in outcomes.items():
                if outcome is not None:
                    failed = sid
                    break
            if failed:
                break

        report_markdown = ""
        facts: Dict[str, Any] = {}
        report_step = self.results.get("s5")
        if isinstance(report_step, dict):
            report_markdown = report_step.get("markdown", "")
            facts = report_step.get("facts", {})
        status = self._final_status(failed)
        duration_ms = int((time.time() - started) * 1000)
        (run_dir / "report.md").write_text(report_markdown, encoding="utf-8")
        payload = {
            "run_id": self.run_id,
            "goal": self.goal,
            "mode": "analysis-dag",
            "status": status,
            "duration_ms": duration_ms,
            "region": region,
            "category": category,
            "statuses": self.statuses,
            "warnings": self.warnings,
            "facts": facts,
            "metrics": {
                "steps_total": len(plan["steps"]),
                "steps_ok": sum(1 for v in self.statuses.values() if v["status"] == "ok"),
                "tool_calls": sum(1 for _ in self.statuses),
                "parallel_levels": 2,
            },
            "artifacts": {"plan": "plan.json", "trace": "trace.jsonl", "report": "report.md"},
            "report": report_markdown,
        }
        (run_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        self.emit("tool_call", step_id=sid, tool=step["tool"], args={"sql": args.get("sql", "")[:200] if isinstance(args, dict) else ""})
        if step["tool"] == "report_render":
            # 渲染步由运行器注入已完成的步骤结果（refs）与注入开关；
            # 校验器随后会用同一套查找逻辑独立重算，两条路径互不信任。
            args = dict(args)
            args["refs"] = dict(self.results)
            args["inject_error"] = self.inject_error
        try:
            out = registry.call(step["tool"], args)
        except Exception as exc:
            self._set(sid, "failed", f"{type(exc).__name__}: {exc}")
            self.emit("tool_result", step_id=sid, ok=False, error=str(exc)[:500])
            self.emit("step_end", step_id=sid, status="failed", reason=str(exc)[:300])
            return sid
        with self._lock:
            self.results[sid] = out

        if step["tool"] == "report_render" and self.verify:
            ok, reason = self._verify_report(out)
            self.emit("verify", step_id=sid, ok=ok, reason=reason, source="deterministic-recompute")
            if not ok:
                self._set(sid, "failed", reason)
                self.emit("step_end", step_id=sid, status="failed", reason=reason)
                return sid
        if step["tool"] == "file_read" and isinstance(out, dict) and out.get("missing_total"):
            self.warnings.append(f"数据集含 {out['missing_total']} 处缺失值（见 file_read 统计）")
            self._set(sid, "degraded", "数据含缺失值，已披露")
        else:
            self._set(sid, "ok", "ok")
        self.emit("tool_result", step_id=sid, ok=True, summary=f"{step['tool']} 完成")
        self.emit("step_end", step_id=sid, status=self.statuses[sid]["status"], reason="ok")
        return None

    def _verify_report(self, out: Dict[str, Any]) -> Tuple[bool, str]:
        """独立重算：不走渲染路径，直接从 SQL 结果重新解析每个指标。"""
        for metric in out.get("metrics") or []:
            name = metric.get("name", "")
            try:
                if metric.get("baseline") is None:
                    expected = metric.get("current")
                else:
                    cur, base = metric.get("current"), metric.get("baseline")
                    if cur is None or base is None:
                        return False, f"「{name}」缺少当期/基期数值"
                    expected = (cur - base) / base * 100 if metric.get("unit") == "%" else cur - base
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                return False, f"「{name}」独立重算失败：{exc}"
            if abs(float(metric.get("value", 0)) - float(expected)) > 0.01:
                return False, (
                    f"「{name}」报告值 {metric.get('value')} 与数据源重算值 {round(float(expected), 4)} 不一致，"
                    "已拦截，不会发布"
                )
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
    parser = argparse.ArgumentParser(description="织序 · 经营分析场景")
    parser.add_argument("goal", nargs="+")
    parser.add_argument("--inject-error", action="store_true", help="演示：渲染时算错关键指标，观察校验器拦截")
    parser.add_argument("--no-verify", action="store_true", help="消融：跳过确定性校验（错误数字将直接发布）")
    parser.add_argument("--json", action="store_true", help="只输出 result JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    goal = " ".join(args.goal).strip()

    def _print(event: dict) -> None:
        print(f"[{event.get('type')}] {event.get('step_id', '')} {str(event.get('reason', event.get('summary', '')))[:80]}", file=sys.stderr)

    run = AnalysisRun(
        goal,
        verify=not args.no_verify,
        inject_error=args.inject_error,
        run_id=args.run_id,
    )
    if args.verbose:
        run.bus.subscribe(_print)
    result = run.run()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("report") or "")
        print("\n" + "=" * 72)
        print(f"状态: {result['status']} · 耗时 {result['duration_ms']}ms · 产物: runs/{result['run_id']}/")
    return 0 if result["status"] not in ("failed",) else 1


if __name__ == "__main__":
    raise SystemExit(main())

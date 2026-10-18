#!/usr/bin/env python3
"""可复现评测：Mock 确定性回归 + 真实书生模型评测 + 消融/基线套件。

用法：
    .venv/bin/python eval/run_eval.py --mode mock
    .venv/bin/python eval/run_eval.py --mode live --limit 4
    .venv/bin/python eval/run_eval.py --mode live --model intern-s2 --fast-model qwen3.8-27b
    .venv/bin/python eval/run_eval.py --suite ablation --mode mock
    .venv/bin/python eval/run_eval.py --suite ablation --mode live --task T01

产物：
    eval/results-<mode>.json / .md
    eval/results-ablation.json / .md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.engine import Orchestrator, RunOptions  # noqa: E402
from core.events import EventBus  # noqa: E402

# 消融/基线变体：对应技术方案 §6.4；serial-exec 兼作 §6.3 的固定流程基线。
ABLATION_VARIANTS = [
    ("ours", {}, "完整织序：DAG 并行 + 确定性校验/验收 + 重规划"),
    ("w/o-verifier", {"verify": False}, "移除确定性校验与整体验收"),
    ("w/o-replanner", {"replan": False}, "移除失败重规划（仅剩步骤内修复）"),
    ("serial-exec", {"parallel": False}, "同层并行退化为串行流水线"),
]
ABLATION_DEFAULT_TASKS = ["T01", "T02", "T05", "T08"]
# 注入去程航班空结果：验证三级恢复的每一层各自贡献。
ABLATION_INJECT_TASK = {
    "id": "T01-inject",
    "goal": "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元，偏好早班机",
    "expect": "ok",
    "inject_failure": True,
    "notes": "注入：去程航班检索被限制为 ¥500 内，预期触发恢复链",
}


def load_tasks() -> List[dict]:
    path = Path(__file__).resolve().parent / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(
    task: dict,
    *,
    mock: bool,
    model: str | None,
    fast_model: str | None,
    limit_replans: int,
    overrides: Optional[Dict[str, Any]] = None,
) -> dict:
    bus = EventBus()
    options = RunOptions(
        goal=task["goal"],
        mode=task.get("mode", "dag"),
        mock=mock,
        model=model,
        fast_model=fast_model,
        use_memory=False,
        auto_approve=True,
        inject_failure=bool(task.get("inject_failure")),
        max_replans=limit_replans,
        **(overrides or {}),
    )
    started = time.perf_counter()
    result = Orchestrator(options, bus=bus).run()
    elapsed = time.perf_counter() - started
    metrics = result.get("metrics") or {}
    facts = result.get("facts") or {}
    status = result.get("status")
    total = facts.get("total")
    warnings = result.get("warnings") or []
    expect = task.get("expect", "ok")

    passed = True
    reasons: List[str] = []
    if expect == "ok":
        if status not in ("ok", "ok_with_warnings"):
            passed = False
            reasons.append(f"status={status}")
        if total is None:
            passed = False
            reasons.append("缺少总费用")
    elif expect == "degraded":
        if status != "degraded":
            passed = False
            reasons.append(f"status={status}")
        if total is None:
            passed = False
            reasons.append("降级但没有费用")
    elif expect == "failed":
        if status not in ("failed", "error") and total is not None:
            passed = False
            reasons.append(f"未按预期失败：status={status}, total={total}")
    if task.get("mode") == "agent" and not mock:
        if status not in ("ok", "ok_with_warnings", "degraded"):
            passed = False
            reasons.append(f"agent status={status}")

    expected_total = task.get("expected_total")
    if mock and expected_total is not None:
        if total is None or abs(float(total) - float(expected_total)) > 1:
            passed = False
            reasons.append(f"total={total} != {expected_total}")
    if not mock and expect == "ok" and total is not None:
        budget = float(facts.get("budget") or 0)
        if budget and float(total) > budget:
            passed = False
            reasons.append(f"超预算 {total}>{budget}")

    if task.get("min_warnings") is not None and len(warnings) < int(task["min_warnings"]):
        passed = False
        reasons.append(f"warnings={len(warnings)} < {task['min_warnings']}")
    if task.get("min_replans") is not None and int(metrics.get("replans", 0)) < int(task["min_replans"]):
        passed = False
        reasons.append(f"replans={metrics.get('replans', 0)} < {task['min_replans']}")
    if task.get("min_repairs") is not None:
        repairs = int(metrics.get("repairs", 0)) + int(metrics.get("optimizations", 0))
        if repairs < int(task["min_repairs"]):
            passed = False
            reasons.append(f"repairs={repairs} < {task['min_repairs']}")

    llm = metrics.get("llm") or {}
    return {
        "id": task["id"],
        "goal": task["goal"],
        "tags": task.get("tags") or [],
        "mode": task.get("mode", "dag"),
        "expect": expect,
        "status": status,
        "passed": bool(passed),
        "reasons": reasons,
        "total": total,
        "expected_total": expected_total,
        "budget": facts.get("budget"),
        "steps_total": metrics.get("steps_total", 0),
        "steps_ok": metrics.get("steps_ok", 0),
        "warnings": len(warnings),
        "warning_list": warnings[:5],
        "replans": metrics.get("replans", 0),
        "repairs": int(metrics.get("repairs", 0)) + int(metrics.get("optimizations", 0)),
        "tokens": llm.get("total_tokens", 0),
        "llm_calls": llm.get("calls", 0),
        "elapsed_s": round(elapsed, 2),
        "run_id": result.get("run_id"),
        "notes": task.get("notes", ""),
    }


def write_reports(rows: List[dict], mode: str, meta: Dict[str, Any]) -> None:
    eval_dir = Path(__file__).resolve().parent
    stamp = datetime.now().isoformat(timespec="seconds")
    payload = {"mode": mode, "generated_at": stamp, **meta, "results": rows}
    (eval_dir / f"results-{mode}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for row in rows if row["passed"])
    lines = [
        f"# WeaveOrder v0.3 评测结果（{mode}）",
        "",
        f"- 生成时间：{stamp}",
        f"- 模型：`{meta.get('model')}` / fast `{meta.get('fast_model')}`",
        f"- 任务数：{len(rows)}",
        f"- 通过：{passed}/{len(rows)}（{passed / len(rows) * 100:.1f}%）",
        f"- 总耗时：{sum(row['elapsed_s'] for row in rows):.1f}s",
        "",
        "| Task | 模式 | 期望 | 状态 | 结果 | 总费用 | 步骤 | 重规划 | 修复/优化 | 提示 | 耗时 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        result = "✅ PASS" if row["passed"] else "❌ FAIL"
        lines.append(
            f"| {row['id']} | {row['mode']} | {row['expect']} | {row['status']} | {result} | "
            f"{('¥%.0f' % row['total']) if row['total'] is not None else '—'} | "
            f"{row['steps_ok']}/{row['steps_total']} | {row['replans']} | {row['repairs']} | "
            f"{row['warnings']} | {row['elapsed_s']}s | {row['notes']} |"
        )
    failures = [row for row in rows if not row["passed"]]
    if failures:
        lines += ["", "## 失败详情", ""]
        for row in failures:
            lines.append(f"- **{row['id']}**：{'; '.join(row['reasons'])}")
            if row["warning_list"]:
                lines.append(f"  - warnings: {'；'.join(row['warning_list'])}")
    lines += [
        "",
        "## 口径",
        "",
        "- Mock 模式使用规则规划与确定性数据源，校验精确总费用。",
        "- Live 模式使用书生 TokenPlan 模型，只要求方案可交付且在预算内，不要求与 Mock 完全同价。",
        "- `repairs` = 步骤内修复 + 预算优化次数；重规划单独计数。",
    ]
    (eval_dir / f"results-{mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def run_ablation(args: argparse.Namespace) -> int:
    wanted = set(args.task) if args.task else set(ABLATION_DEFAULT_TASKS)
    tasks = [dict(t) for t in load_tasks() if t["id"] in wanted]
    tasks.append(dict(ABLATION_INJECT_TASK))
    print(f"== 消融/基线套件：{len(ABLATION_VARIANTS)} 变体 × {len(tasks)} 任务（{args.mode}）==")

    all_rows: Dict[str, List[dict]] = {}
    for name, overrides, _note in ABLATION_VARIANTS:
        rows: List[dict] = []
        for task in tasks:
            print(f"[{name}] {task['id']} …", flush=True)
            try:
                row = evaluate(
                    task,
                    mock=args.mode == "mock",
                    model=args.model,
                    fast_model=args.fast_model,
                    limit_replans=args.max_replans,
                    overrides=overrides,
                )
            except Exception as exc:
                row = {
                    "id": task["id"], "goal": task["goal"], "tags": task.get("tags") or [],
                    "mode": task.get("mode", "dag"), "expect": task.get("expect", "ok"),
                    "status": "exception", "passed": False,
                    "reasons": [f"{type(exc).__name__}: {exc}"],
                    "total": None, "expected_total": task.get("expected_total"),
                    "budget": None, "steps_total": 0, "steps_ok": 0, "steps_degraded": 0,
                    "warnings": 0, "warning_list": [], "replans": 0, "repairs": 0,
                    "optimizations": 0, "tokens": 0, "llm_calls": 0, "elapsed_s": 0,
                    "run_id": "", "notes": task.get("notes", ""),
                }
            row["variant"] = name
            rows.append(row)
            print(
                f"    {'PASS' if row['passed'] else 'FAIL'} · {row['status']} · "
                f"¥{row['total'] if row['total'] is not None else '—'} · {row['elapsed_s']}s",
                flush=True,
            )
        all_rows[name] = rows

    eval_dir = Path(__file__).resolve().parent
    stamp = datetime.now().isoformat(timespec="seconds")
    from core.config import Config

    cfg = Config()
    meta = {
        "mode": args.mode,
        "generated_at": stamp,
        "model": args.model or cfg.model,
        "fast_model": args.fast_model or cfg.fast_model,
        "version": "0.4.0",
    }
    (eval_dir / f"results-ablation-{args.mode}.json").write_text(
        json.dumps({"meta": meta, "variants": all_rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    variant_notes = dict((name, note) for name, _ov, note in ABLATION_VARIANTS)
    lines = [
        f"# WeaveOrder v0.4 消融/基线评测（{args.mode}）",
        "",
        f"- 生成时间：{stamp}",
        f"- 模型：`{meta['model']}` / fast `{meta['fast_model']}`",
        f"- 任务：{len(tasks)} 条 × {len(ABLATION_VARIANTS)} 变体",
        "",
        "| 变体 | 移除的组件 | 通过 | 平均耗时(s) | 步骤 ok/total | 重规划 | 修复/优化 | 失败任务 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in all_rows.items():
        passed = sum(1 for r in rows if r["passed"])
        failed_ids = [r["id"] for r in rows if not r["passed"]]
        lines.append(
            f"| `{name}` | {variant_notes[name]} | {passed}/{len(rows)} | "
            f"{sum(r['elapsed_s'] for r in rows) / len(rows):.1f} | "
            f"{sum(r['steps_ok'] for r in rows)}/{sum(r['steps_total'] for r in rows)} | "
            f"{sum(r['replans'] for r in rows)} | {sum(r['repairs'] for r in rows)} | "
            f"{(', '.join(failed_ids)) if failed_ids else '—'} |"
        )
    lines += ["", "## 明细", "", "| 变体 | Task | 期望 | 状态 | 结果 | 总费用 | 重规划 | 修复 | 耗时 |", "|---|---|---|---|---|---|---|---|---|"]
    for name, rows in all_rows.items():
        for row in rows:
            mark = "✅" if row["passed"] else "❌"
            lines.append(
                f"| `{name}` | {row['id']} | {row['expect']} | {row['status']} | {mark} | "
                f"{('¥%.0f' % row['total']) if row['total'] is not None else '—'} | "
                f"{row['replans']} | {row['repairs']} | {row['elapsed_s']}s |"
            )
    lines += [
        "",
        "## 与技术方案的对应",
        "",
        "- `ours` = 完整编排（§3 架构全量）。",
        "- `w/o-verifier` / `w/o-replanner` = §6.4 消融。",
        "- `serial-exec` = §6.3 固定流程基线（同层并行关闭）。",
        "- ReAct 单 Agent 基线 = `mode=agent`，见 results-live-*.md 中 T13（Agent）vs T01（DAG）。",
        "- 并行的时间收益用 live 模式计时体现（mock 工具瞬时返回，耗时差为 0）。",
        "",
        "## 口径",
        "",
        "- 通过判定与主评测一致：期望 ok/degraded/failed 分别校验。",
        "- `w/o-verifier` 下预算无解任务（T02/T08）若被判 ok 但总费用超预算，即为验收缺失的直接证据。",
        "- T01-inject 注入去程航班空结果：ours 依赖步骤内修复/重规划恢复；w/o-verifier 会把空结果采信并向下游传播。",
    ]
    (eval_dir / f"results-ablation-{args.mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def _golden_analysis(region: str, category: str) -> str:
    """从 CSV 独立重算“{region}{category} 9 月环比”，作为评测黄金值。"""
    import csv as _csv

    path = ROOT / "data" / "sales_2026Q3.csv"
    agg: Dict[tuple, float] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            key = (row["month"], row["region"], row["category"])
            agg[key] = agg.get(key, 0.0) + float(row["revenue"])
    cur = agg[("2026-09", region, category)]
    base = agg[("2026-08", region, category)]
    return f"{(cur - base) / base * 100:+.1f}%"


def run_analysis_suite(args: argparse.Namespace) -> int:
    """第二场景（经营分析）评测：黄金值独立重算 + 注入/消融用例。"""
    from analysis_agent import AnalysisRun

    path = Path(__file__).resolve().parent / "analysis_tasks.jsonl"
    tasks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t["id"] in wanted]

    rows: List[dict] = []
    for index, task in enumerate(tasks, 1):
        print(f"[{index}/{len(tasks)}] {task['id']} {task['goal'][:40]}…", flush=True)
        started = time.perf_counter()
        try:
            run = AnalysisRun(
                task["goal"],
                verify=task.get("verify", True),
                inject_error=bool(task.get("inject_error")),
                run_id=f"eval-{task['id'].lower()}",
            )
            result = run.run()
            elapsed = round(time.perf_counter() - started, 3)
            status = result.get("status")
            report = result.get("report") or ""
            reasons: List[str] = []
            expect = task.get("expect", "degraded")
            if status != expect:
                reasons.append(f"status={status} != {expect}")
            for text in task.get("assert_contains") or []:
                if text not in report:
                    reasons.append(f"报告缺少 {text}")
            if task.get("wrong_number_published"):
                golden = _golden_analysis(task["region"], task["category"])
                if golden in report:
                    reasons.append(f"消融用例不应包含正确值 {golden}")
                task["notes"] = (
                    task.get("notes", "")
                    + f"【已发布错误数字（正确值 {golden}）——w/o-verifier 的失败模式】"
                )
            elif status != "failed":
                golden = _golden_analysis(task["region"], task["category"])
                if golden not in report:
                    reasons.append(f"报告缺少黄金值 {golden}（独立重算）")
            rows.append(
                {
                    "id": task["id"],
                    "goal": task["goal"],
                    "expect": expect,
                    "status": status,
                    "passed": not reasons,
                    "reasons": reasons,
                    "elapsed_s": elapsed,
                    "notes": task.get("notes", ""),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "id": task["id"], "goal": task["goal"], "expect": task.get("expect", "degraded"),
                    "status": "exception", "passed": False,
                    "reasons": [f"{type(exc).__name__}: {exc}"], "elapsed_s": 0,
                    "notes": task.get("notes", ""),
                }
            )
        print(f"    {'PASS' if rows[-1]['passed'] else 'FAIL'} · {rows[-1]['status']} · {rows[-1]['elapsed_s']}s", flush=True)

    eval_dir = Path(__file__).resolve().parent
    stamp = datetime.now().isoformat(timespec="seconds")
    passed = sum(1 for r in rows if r["passed"])
    lines = [
        "# WeaveOrder v0.4 评测结果（analysis · 经营分析场景）",
        "",
        f"- 生成时间：{stamp}",
        f"- 任务数：{len(rows)}（含注入与消融用例）",
        f"- 通过：{passed}/{len(rows)}",
        f"- 总耗时：{sum(r['elapsed_s'] for r in rows):.2f}s（确定性，可复现）",
        "",
        "| Task | 期望 | 状态 | 结果 | 断言 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        mark = "✅ PASS" if row["passed"] else "❌ FAIL"
        asserts = "；".join(row["reasons"]) if row["reasons"] else "全部通过"
        lines.append(f"| {row['id']} | {row['expect']} | {row['status']} | {mark} | {asserts} | {row['notes']} |")
    lines += [
        "",
        "## 口径",
        "",
        "- 黄金值由评测脚本直接从 `data/sales_2026Q3.csv` 独立重算，与被测实现零共享代码。",
        "- A05/A06 为同一注入的对照：校验开 → 拦截（failed）；校验关 → 错误数字直接发布（w/o-verifier 失败模式）。",
        "- A04 数据源不覆盖西北大区：诚实失败，不编造。",
    ]
    (eval_dir / "results-analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (eval_dir / "results-analysis.json").write_text(
        json.dumps({"generated_at": stamp, "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n".join(lines))
    return 0 if passed == len(rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="WeaveOrder 评测")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--suite", choices=["main", "ablation", "analysis"], default="main")
    parser.add_argument("--model", default=None)
    parser.add_argument("--fast-model", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--task", action="append", default=None, help="只跑指定 id，可重复")
    parser.add_argument("--max-replans", type=int, default=2)
    args = parser.parse_args()

    if args.suite == "ablation":
        return run_ablation(args)
    if args.suite == "analysis":
        return run_analysis_suite(args)

    tasks = load_tasks()
    if args.task:
        wanted = set(args.task)
        tasks = [task for task in tasks if task["id"] in wanted]
    if args.mode == "mock":
        tasks = [task for task in tasks if not task.get("live_only")]
    elif args.limit:
        tasks = tasks[: args.limit]

    rows = []
    for index, task in enumerate(tasks, 1):
        print(f"[{index}/{len(tasks)}] {task['id']} {task['goal'][:42]}…", flush=True)
        try:
            row = evaluate(
                task,
                mock=args.mode == "mock",
                model=args.model,
                fast_model=args.fast_model,
                limit_replans=args.max_replans,
            )
        except Exception as exc:
            row = {
                "id": task["id"], "goal": task["goal"], "tags": task.get("tags") or [],
                "mode": task.get("mode", "dag"), "expect": task.get("expect", "ok"),
                "status": "exception", "passed": False, "reasons": [f"{type(exc).__name__}: {exc}"],
                "total": None, "expected_total": task.get("expected_total"), "budget": None,
                "steps_total": 0, "steps_ok": 0, "warnings": 0, "warning_list": [],
                "replans": 0, "repairs": 0, "tokens": 0, "llm_calls": 0, "elapsed_s": 0,
                "run_id": "", "notes": task.get("notes", ""),
            }
        rows.append(row)
        print(
            f"    {'PASS' if row['passed'] else 'FAIL'} · {row['status']} · "
            f"¥{row['total'] if row['total'] is not None else '—'} · {row['elapsed_s']}s"
        )

    from core.config import Config

    cfg = Config()
    write_reports(
        rows,
        args.mode,
        {
            "model": args.model or cfg.model,
            "fast_model": args.fast_model or cfg.fast_model,
            "version": "0.3.0",
        },
    )
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())

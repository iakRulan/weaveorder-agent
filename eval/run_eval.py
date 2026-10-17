#!/usr/bin/env python3
"""Small, reproducible evaluation for WeaveOrder.

Usage:
    .venv/bin/python eval/run_eval.py --mode mock
    env -u ALL_PROXY .venv/bin/python eval/run_eval.py --mode live --limit 2

Mock mode uses the deterministic rule planner, so exact totals are checked.
Live mode uses Intern-S2; it reports totals and constraint status, but does not
require an exact total because the model may choose a different valid option.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402


def load_tasks() -> list[dict]:
    path = Path(__file__).resolve().parent / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_total(report: str) -> float | None:
    match = re.search(r"预估合计：¥([\d.]+)", report or "")
    return float(match.group(1)) if match else None


def run_one(task: dict, mock: bool) -> dict:
    cfg = agent.Config(mock=mock)
    llm = agent.LLM(cfg)
    tools = agent.build_registry()
    orch = agent.Orchestrator(
        llm,
        tools,
        auto_approve=True,
        inject_failure=bool(task.get("inject_failure")),
    )
    started = time.perf_counter()
    report, run_dir = orch.run(task["goal"])
    elapsed = time.perf_counter() - started

    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    trace = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    steps = plan.get("steps", [])
    statuses = orch.status
    ok_steps = sum(1 for s in steps if statuses.get(s["id"], ("", ""))[0] == "ok")
    failed_steps = sum(1 for s in steps if statuses.get(s["id"], ("", ""))[0] == "failed")
    replans = sum(1 for e in trace if e["type"] == "replan")
    retries = sum(
        1 for e in trace if e["type"] == "tool_call" and int(e.get("attempt", 1)) > 1
    )
    total = parse_total(report)
    expected = task.get("expect", "ok")

    if expected == "ok":
        passed = failed_steps == 0 and ok_steps == len(steps)
        if mock and "expected_total" in task:
            passed = passed and total is not None and abs(total - task["expected_total"]) <= 1
        if not mock:
            passed = passed and total is not None
    else:
        passed = failed_steps > 0 or total is None
    if "min_warnings" in task:
        passed = passed and len(orch.warnings) >= int(task["min_warnings"])
    if "min_replans" in task:
        passed = passed and replans >= int(task["min_replans"])

    return {
        "id": task["id"],
        "goal": task["goal"],
        "expect": expected,
        "passed": bool(passed),
        "total": total,
        "expected_total": task.get("expected_total"),
        "ok_steps": ok_steps,
        "steps": len(steps),
        "failed_steps": failed_steps,
        "replans": replans,
        "retries": retries,
        "warnings": len(orch.warnings),
        "elapsed_s": round(elapsed, 2),
        "run_id": run_dir.name,
        "notes": task.get("notes", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个任务")
    parser.add_argument("--out-dir", default="eval")
    args = parser.parse_args()

    tasks = load_tasks()
    if args.limit:
        tasks = tasks[: args.limit]
    results = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task['id']} {task['goal'][:48]}...", flush=True)
        try:
            result = run_one(task, mock=args.mode == "mock")
        except Exception as exc:
            result = {
                "id": task["id"],
                "goal": task["goal"],
                "expect": task.get("expect", "ok"),
                "passed": False,
                "total": None,
                "expected_total": task.get("expected_total"),
                "ok_steps": 0,
                "steps": 0,
                "failed_steps": 0,
                "replans": 0,
                "retries": 0,
                "warnings": 0,
                "elapsed_s": 0,
                "run_id": "",
                "notes": f"exception: {type(exc).__name__}: {exc}",
            }
        results.append(result)
        print(
            f"    {'PASS' if result['passed'] else 'FAIL'} total={result['total']} "
            f"steps={result['ok_steps']}/{result['steps']} replans={result['replans']} "
            f"warnings={result['warnings']} {result['elapsed_s']}s",
            flush=True,
        )

    passed = sum(1 for r in results if r["passed"])
    avg_time = sum(r["elapsed_s"] for r in results) / max(1, len(results))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"results-{args.mode}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"# WeaveOrder 评测结果（{args.mode}）",
        "",
        f"- 任务数：{len(results)}",
        f"- 通过：{passed}/{len(results)}",
        f"- 平均耗时：{avg_time:.2f}s",
        "",
        "| Task | 结果 | 总费用 | 步骤 | 重规划 | 重试 | 冲突提示 | 耗时 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        total = f"¥{r['total']:.0f}" if r["total"] is not None else "—"
        lines.append(
            f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {total} | "
            f"{r['ok_steps']}/{r['steps']} | {r['replans']} | {r['retries']} | "
            f"{r['warnings']} | {r['elapsed_s']}s | {r['notes']} |"
        )
    (out_dir / f"results-{args.mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()

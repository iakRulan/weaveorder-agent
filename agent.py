#!/usr/bin/env python3
"""织序 WeaveOrder — 可审计、可干预、可复用的任务编排智能体。

v0.3 CLI：
    python agent.py --mock "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元"
    python agent.py --yes "帮我安排 ..."                 # 真实 LLM，自动批准（演示）
    python agent.py --yes --mode agent "帮我安排 ..."    # 原生 Function Calling Agent
    python agent.py --mock --inject-failure "..."        # 演示失败恢复

兼容层：保留 Config / LLM / Orchestrator / build_registry 等旧入口，
供 MCP server、评测脚本和外部 harness 直接 import。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    ApprovalGate,
    Config,
    EventBus,
    LLM,
    MemoryStore,
    Orchestrator,
    Plan,
    RunOptions,
    Step,
    VERSION,
)
from core.events import describe_event  # noqa: E402
from core.tools import Tool, ToolRegistry, build_registry  # noqa: E402
from core import catalog, policy  # noqa: E402,F401
from core import tools as tool_module  # noqa: E402


def _print_event(event: dict) -> None:
    print(describe_event(event), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"织序 WeaveOrder v{VERSION}")
    parser.add_argument("goal", nargs="+", help="一句话目标")
    parser.add_argument("--mock", action="store_true", help="强制使用规则规划与 mock 数据")
    parser.add_argument("--yes", "-y", action="store_true", help="自动批准高风险动作（演示用）")
    parser.add_argument(
        "--mode", choices=["dag", "agent"], default="dag", help="dag=规划执行；agent=原生工具调用"
    )
    parser.add_argument("--model", default=None, help="覆盖主模型，如 intern-s2 / qwen3.8-27b")
    parser.add_argument("--fast-model", default=None, help="覆盖结构化任务使用的轻量模型")
    parser.add_argument("--think", action="store_true", help="启用 Intern-S2 深度思考（更慢）")
    parser.add_argument("--no-memory", action="store_true", help="关闭长期记忆读写")
    parser.add_argument(
        "--planner",
        choices=["auto", "llm", "rule"],
        default="auto",
        help="兼容旧参数；rule 等价于 --mock",
    )
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="演示用：把去程航班价格上限压到 500，触发空结果与恢复",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="把事件流打印到 stderr")
    parser.add_argument("--json", action="store_true", help="只输出 result JSON")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    goal = " ".join(args.goal).strip()
    if not goal:
        parser.error("goal 不能为空")
    mock = bool(args.mock or args.planner == "rule")
    options = RunOptions(
        goal=goal,
        mode=args.mode,
        mock=mock,
        model=args.model,
        fast_model=args.fast_model,
        thinking=True if args.think else None,
        use_memory=not args.no_memory,
        auto_approve=bool(args.yes or mock),
        inject_failure=args.inject_failure,
        run_id=args.run_id,
    )
    bus = EventBus()
    if args.verbose:
        bus.subscribe(_print_event)
    gate_mode = "auto" if options.auto_approve else "interactive"
    gate = ApprovalGate(mode=gate_mode)
    orch = Orchestrator(options, bus=bus, gate=gate)
    result = orch.run()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n" + "=" * 78)
        print(result.get("report", ""))
        print("=" * 78)
        run_dir = Path("runs") / result["run_id"]
        print(f"\n状态  : {result['status']}")
        print(f"Trace : {run_dir / 'trace.jsonl'}")
        print(f"Plan  : {run_dir / 'plan.json'}")
        print(f"Result: {run_dir / 'result.json'}")
        print(f"Report: {run_dir / 'report.md'}")
    return 0 if result.get("status") not in ("error",) else 1


if __name__ == "__main__":
    raise SystemExit(main())

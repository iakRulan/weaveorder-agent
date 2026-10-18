"""把一次运行渲染成可交付的 Markdown 报告。

报告不是 trace 的复制，而是「结论 → 方案 → 依据 → 风险 → 审计」的叙事：
每个数字都能在 trace 里找到对应的工具调用。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

STATUS_LABELS = {
    "ok": "成功",
    "ok_with_warnings": "成功（有提示）",
    "degraded": "降级完成",
    "denied": "审批未通过",
    "cancelled": "已取消",
    "failed": "失败",
    "error": "异常",
    "running": "运行中",
    "skipped": "跳过",
    "pending": "待执行",
}


def _money(value: Any) -> str:
    try:
        return f"¥{float(value):,.0f}"
    except Exception:
        return "—"


def _duration(ms: Any) -> str:
    try:
        value = int(ms)
    except Exception:
        return "—"
    if value < 1000:
        return f"{value}ms"
    return f"{value / 1000:.1f}s"


def demote_headings(markdown: str, max_level: int = 5) -> str:
    lines = []
    for line in str(markdown or "").splitlines():
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            rest = line[hashes:]
            if hashes < max_level:
                line = "#" + line
        lines.append(line)
    return "\n".join(lines)


def _find_result(run: Any, tool: str) -> Optional[dict]:
    try:
        value = run._result_by_tool(tool)
    except Exception:
        value = None
    return value if isinstance(value, dict) else None


def _all_results(run: Any, tool: str) -> List[dict]:
    try:
        values = run._result_by_tool(tool, all_results=True)
    except Exception:
        values = []
    return [v for v in values if isinstance(v, dict)]


def _plan_steps(run: Any) -> List[Any]:
    plan = getattr(run, "plan", None)
    return list(plan.steps) if plan is not None else []


def _status_of(run: Any, step_id: str) -> Dict[str, Any]:
    statuses = getattr(run, "statuses", {}) or {}
    return statuses.get(step_id, {}) or {}


def _events(run: Any, types: tuple) -> List[dict]:
    bus = getattr(run, "bus", None)
    if bus is None:
        return []
    return [event for event in bus.snapshot() if event.get("type") in types]


def build_report(
    run: Any,
    narrative: str = "",
    verifier: Optional[Dict[str, Any]] = None,
    error: str = "",
) -> str:
    request = getattr(run, "request", None)
    plan = getattr(run, "plan", None)
    facts = getattr(run, "facts", {}) or {}
    warnings = list(getattr(run, "warnings", []) or [])
    review_notes = list(getattr(run, "review_notes", []) or [])
    critique = getattr(run, "critique", {}) or {}
    verifier = verifier or getattr(run, "verifier", {}) or {}
    status = getattr(run, "final_status", "running")
    status_label = STATUS_LABELS.get(status, status)
    cfg = getattr(run, "cfg", None)
    options = getattr(run, "options", None)
    mode = getattr(options, "mode", "dag")
    mode_label = "自主 Agent（Function Calling）" if mode == "agent" else "DAG 编排"
    try:
        metrics = run._metrics_snapshot()
    except Exception:
        metrics = {}
    duration = metrics.get("duration_ms") or facts.get("duration_ms")
    route = request.route if request is not None else ""
    title_bits = [route, request.purpose if request is not None else "", "差旅执行报告"]
    title = " · ".join([bit for bit in title_bits if bit])

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        f"> 运行 `{getattr(run, 'run_id', '')}` · {mode_label} · "
        f"模型 `{getattr(cfg, 'model', '') if cfg else ''}` · "
        f"耗时 {_duration(duration)} · 状态：**{status_label}**"
    )
    lines.append("")
    if error:
        lines.append(f"> ⚠️ 运行异常：{error}")
        lines.append("")

    if narrative:
        lines.append("## 摘要")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")

    draft = None
    try:
        draft = run._draft_result()
    except Exception:
        draft = None
    if isinstance(draft, dict) and draft.get("markdown"):
        lines.append("## 行程方案")
        lines.append("")
        lines.append(demote_headings(str(draft["markdown"])))
        lines.append("")
    else:
        lines.append("## 行程方案")
        lines.append("")
        lines.append("本次运行未生成完整行程草案。以下为已确认的工具结果与执行摘要。")
        lines.append("")

    # -- 费用 ------------------------------------------------------------
    cost = _find_result(run, "compute_trip_cost")
    total = facts.get("total")
    if cost or total is not None:
        lines.append("## 费用与预算")
        lines.append("")
        if isinstance(cost, dict) and cost.get("breakdown"):
            lines.append("| 项目 | 说明 | 金额 |")
            lines.append("|---|---|---|")
            for item in cost["breakdown"]:
                lines.append(
                    f"| {item.get('item', '')} | {item.get('detail', '')} | "
                    f"{_money(item.get('amount'))} |"
                )
            lines.append(f"| **合计** | 往返交通 + 住宿 | **{_money(cost.get('total'))}** |")
            lines.append("")
        if total is not None:
            lines.append(f"- 预估合计：{_money(total)}")
        budget = facts.get("budget")
        if budget is not None:
            try:
                remaining = float(budget) - float(total or 0)
                lines.append(
                    f"- 预算：{_money(budget)}；"
                    f"{'剩余 ' + _money(remaining) if remaining >= 0 else '超出 ' + _money(-remaining)}"
                )
            except Exception:
                pass
        lines.append("")

    # -- 合规 ------------------------------------------------------------
    compliance = None
    if isinstance(draft, dict):
        compliance = draft.get("compliance")
    if not isinstance(compliance, dict):
        try:
            compliance = run._compliance_result()
        except Exception:
            compliance = None
    clauses: List[dict] = []
    policy_result = _find_result(run, "check_policy")
    if isinstance(policy_result, dict):
        clauses = [c for c in (policy_result.get("clauses") or []) if isinstance(c, dict)]
    lines.append("## 合规与政策依据")
    lines.append("")
    if isinstance(compliance, dict):
        lines.append(
            f"- 合规评分：**{compliance.get('score', '—')}/100**；"
            f"阻塞项 {compliance.get('blockers', 0)} 个，提示 {compliance.get('warnings_count', 0)} 个"
        )
        for item in compliance.get("violations") or []:
            lines.append(f"- 🔴 {item.get('message', '')}")
        for item in compliance.get("warnings") or []:
            lines.append(f"- 🟡 {item.get('message', '')}")
        if not (compliance.get("violations") or compliance.get("warnings")):
            lines.append("- ✅ 未发现政策冲突")
    else:
        lines.append("- 未取得合规检查结果。")
    if clauses:
        lines.append("")
        lines.append("| 条款 | 内容 |")
        lines.append("|---|---|")
        for clause in clauses:
            lines.append(
                f"| {clause.get('id', '')} | {str(clause.get('text', '')).replace('|', '｜')} |"
            )
    lines.append("")

    # -- 执行摘要 --------------------------------------------------------
    lines.append("## 执行摘要")
    lines.append("")
    lines.append("| 步骤 | 目标 | 工具 | 风险 | 状态 | 耗时 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|")
    for step in _plan_steps(run):
        state = _status_of(run, step.id)
        step_status = state.get("status", "pending")
        lines.append(
            f"| {step.id} | {step.goal.replace('|', '｜')} | `{step.tool}` | "
            f"{step.risk} | {STATUS_LABELS.get(step_status, step_status)} | "
            f"{_duration(state.get('latency_ms'))} | "
            f"{str(state.get('reason', '')).replace('|', '｜')[:120]} |"
        )
    if not _plan_steps(run):
        lines.append("| - | 未生成计划 | - | - | - | - | - |")
    lines.append("")

    # -- 关键编排决策 ----------------------------------------------------
    decisions = _events(run, ("replan", "plan_revised", "repair", "optimization", "injection", "warning"))
    if decisions:
        lines.append("## 关键编排决策")
        lines.append("")
        for event in decisions[:16]:
            kind = event.get("type")
            if kind == "replan":
                text = f"第 {event.get('round')} 轮重规划：{event.get('reason', '')}"
            elif kind == "plan_revised":
                text = f"计划修订：{event.get('revision_reason') or event.get('message', '')}"
            elif kind == "repair":
                text = f"{event.get('step_id')} 自动修复：{'；'.join(event.get('changes') or [])}"
            elif kind == "optimization":
                text = event.get("message", "")
            elif kind == "injection":
                text = event.get("message", "")
            else:
                text = event.get("message", "")
            if text:
                lines.append(f"- `{event.get('t', 0)}ms` {text}")
        lines.append("")

    # -- 风险与冲突 ------------------------------------------------------
    lines.append("## 风险与冲突")
    lines.append("")
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- 未发现显式冲突。")
    if getattr(run, "questions", None):
        lines.append("")
        lines.append("**待确认问题**")
        lines.append("")
        for question in run.questions:
            lines.append(f"- {question}")
    lines.append("")

    # -- 审查与验收 ------------------------------------------------------
    lines.append("## 审查与验收")
    lines.append("")
    verdict = critique.get("verdict", "—")
    lines.append(f"- 计划审查：**{verdict}**")
    for issue in (critique.get("issues") or [])[:6]:
        lines.append(
            f"  - [{issue.get('severity', 'low')}] {issue.get('step', '-')} "
            f"{issue.get('message', '')}"
        )
    if verifier:
        lines.append(
            f"- 结果验收：**{'通过' if verifier.get('ok') else '有风险'}**"
            f"（{verifier.get('score', '—')}/100）"
        )
        for issue in verifier.get("issues") or []:
            lines.append(f"  - {issue}")
    if review_notes:
        lines.append(f"- 审查记录：{len(review_notes)} 条")
    if not critique and not verifier:
        lines.append("- 本次运行未启用模型审查。")
    lines.append("")

    # -- 指标 ------------------------------------------------------------
    llm = metrics.get("llm") or {}
    lines.append("## 运行指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    rows = [
        ("总耗时", _duration(metrics.get("duration_ms"))),
        ("步骤", f"{metrics.get('steps_total', 0)} 步"),
        ("成功 / 降级 / 失败", f"{metrics.get('steps_ok', 0)} / {metrics.get('steps_degraded', 0)} / {metrics.get('steps_failed', 0)}"),
        ("重规划", metrics.get("replans", 0)),
        ("步骤修复", metrics.get("repairs", 0)),
        ("预算优化", metrics.get("optimizations", 0)),
        ("工具调用", metrics.get("tool_calls", 0)),
        ("并行批次", metrics.get("parallel_rounds", 0)),
        ("模型调用", llm.get("calls", 0)),
        ("Token", f"{llm.get('prompt_tokens', 0)} in / {llm.get('completion_tokens', 0)} out"),
        ("模型平均延迟", _duration(llm.get("avg_latency_ms", 0))),
    ]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    lines.append("")

    # -- 审计 ------------------------------------------------------------
    bus = getattr(run, "bus", None)
    trace_count = bus.last_seq if bus is not None else 0
    lines.append("## 可审计性")
    lines.append("")
    lines.append(f"- 本次运行共记录 **{trace_count}** 个事件，写入 `trace.jsonl`。")
    lines.append("- 计划快照：`plan.json`；运行摘要：`result.json`；本报告：`report.md`。")
    lines.append("- 报告中的航班、酒店、费用与政策编号均可回查对应 `tool_call` / `tool_result` 事件。")
    lines.append("- 高风险动作在 `approval_required` 处暂停，只有批准后才会执行。")
    lines.append("")

    lines.append("## 假设与待确认")
    lines.append("")
    assumptions = list(getattr(plan, "assumptions", []) or []) if plan is not None else []
    if request is not None:
        assumptions = list(assumptions) + list(getattr(request, "assumptions", []) or [])
    seen = set()
    for assumption in assumptions:
        key = str(assumption).strip()
        if key and key not in seen:
            seen.add(key)
            lines.append(f"- {key}")
    if not assumptions:
        lines.append("- 无额外假设。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_由织序 WeaveOrder 生成：Plan → Critique → Execute(DAG) → Verify → Narrate → Memory。_"
    )
    return "\n".join(lines)


__all__ = ["build_report", "demote_headings", "STATUS_LABELS"]

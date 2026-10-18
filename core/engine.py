"""织序编排引擎：多智能体协同 + 并行 DAG 执行 + 审批闸门 + 可观测事件。

一次运行的完整生命周期：

    intake → plan → critique → execute(DAG) → verify → narrate → memory

失败时的恢复顺序：
    步骤内确定性修复 → LLM Replanner 重规划 → 显式失败并保留完整 trace
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import catalog, policy
from .agents import (
    CriticAgent,
    IntakeAgent,
    MemoryAgent,
    NarratorAgent,
    PlannerAgent,
    ReplannerAgent,
    VerifierAgent,
    TRAVEL_TOOLS,
)
from .config import CODE_NAME, VERSION, Config, RUNS_DIR, ensure_dirs
from .events import EventBus
from .intake import TravelRequest
from .llm import LLM, LLMError
from .memory import MemoryStore
from .plan import (
    Plan,
    Step,
    PlanError,
    ensure_approval_step,
    normalize_travel_plan,
    resolve,
    summarize_value,
    topological_levels,
    validate_plan,
)
from .report import build_report, demote_headings
from .tools import ToolRegistry, build_registry, compute_trip_cost


# ---------------------------------------------------------------------------
# 运行选项与审批
# ---------------------------------------------------------------------------


@dataclass
class RunOptions:
    goal: str
    mode: str = "dag"  # dag | agent
    mock: bool = False
    model: Optional[str] = None
    fast_model: Optional[str] = None
    thinking: Optional[bool] = None
    use_memory: bool = True
    auto_approve: bool = True
    inject_failure: bool = False
    run_id: Optional[str] = None
    max_replans: Optional[int] = None
    # 消融/基线开关（评测对比实验用；默认全开 = 完整织序编排）
    verify: bool = True    # 关闭后跳过步骤级确定性校验与整体验收
    replan: bool = True    # 关闭后失败不触发重规划
    parallel: bool = True  # 关闭后同层步骤退化为串行流水线

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "mode": self.mode,
            "mock": self.mock,
            "model": self.model,
            "fast_model": self.fast_model,
            "thinking": self.thinking,
            "use_memory": self.use_memory,
            "auto_approve": self.auto_approve,
            "inject_failure": self.inject_failure,
            "run_id": self.run_id,
            "verify": self.verify,
            "replan": self.replan,
            "parallel": self.parallel,
        }


@dataclass
class ApprovalRequest:
    id: str
    step_id: str
    action: str
    detail: str
    amount: Optional[float]
    risk: int
    created_at: str
    status: str = "pending"
    approved: bool = False
    note: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "approval_id": self.id,
            "step_id": self.step_id,
            "action": self.action,
            "detail": self.detail[:4000],
            "amount": self.amount,
            "risk": self.risk,
            "created_at": self.created_at,
            "status": self.status,
        }


class ApprovalGate:
    """真实的人工审批闸门。

    ``mode=auto`` 用于评测/CLI --yes；``interactive`` 在终端等待输入；
    ``remote`` 由 Web API 调用 ``resolve`` 放行。
    """

    def __init__(
        self,
        mode: str = "auto",
        timeout: float = 600.0,
        emit: Optional[Any] = None,
    ) -> None:
        self.mode = mode
        self.timeout = timeout
        self.emit = emit
        self._lock = threading.Lock()
        self._requests: Dict[str, ApprovalRequest] = {}
        self._events: Dict[str, threading.Event] = {}
        self._counter = 0

    def create(
        self,
        step: Optional[Step] = None,
        action: str = "",
        detail: str = "",
        amount: Optional[float] = None,
        risk: int = 2,
    ) -> ApprovalRequest:
        with self._lock:
            self._counter += 1
            request_id = f"ap-{int(time.time() * 1000)}-{self._counter}"
            request = ApprovalRequest(
                id=request_id,
                step_id=getattr(step, "id", ""),
                action=action or getattr(step, "goal", "高风险动作"),
                detail=detail or "",
                amount=amount,
                risk=risk,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._requests[request_id] = request
            self._events[request_id] = threading.Event()
        return request

    def wait(
        self,
        request: ApprovalRequest,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        if self.mode == "auto":
            request.status = "auto-approved"
            request.approved = True
            request.note = "自动审批（评测/演示模式）"
            return True
        if self.mode == "interactive":
            print("\n" + "=" * 72)
            print("[需要人工确认]")
            print(f"动作：{request.action}")
            print(f"详情：{request.detail[:1200]}")
            if request.amount is not None:
                print(f"金额：¥{request.amount:,.0f}")
            print("=" * 72)
            try:
                answer = input("批准执行？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            approved = answer in ("y", "yes", "是", "批准")
            request.approved = approved
            request.status = "approved" if approved else "rejected"
            request.note = "终端确认"
            return approved
        # remote
        event = self._events.get(request.id)
        deadline = time.time() + self.timeout
        while not event.wait(0.4):
            if cancel_event is not None and cancel_event.is_set():
                request.status = "cancelled"
                request.note = "运行已取消"
                return False
            if time.time() > deadline:
                request.status = "timeout"
                request.note = "审批超时，按拒绝处理"
                return False
        return request.approved

    def resolve(self, approval_id: str, approved: bool, note: str = "") -> bool:
        with self._lock:
            request = self._requests.get(approval_id)
            event = self._events.get(approval_id)
        if request is None or event is None:
            return False
        request.approved = bool(approved)
        request.status = "approved" if approved else "rejected"
        request.note = note
        event.set()
        return True

    def pending(self) -> List[dict]:
        with self._lock:
            return [
                request.public()
                for request in self._requests.values()
                if request.status == "pending"
            ]

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [request.public() for request in self._requests.values()]


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


class RunTrace:
    def __init__(self, bus: EventBus, path: Path) -> None:
        self.bus = bus
        self.path = path
        self._lock = threading.Lock()

    def add(self, event_type: str, **data: Any) -> dict:
        event = self.bus.emit(event_type, **data)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def events(self) -> List[dict]:
        return self.bus.snapshot()


@dataclass
class StepOutcome:
    step_id: str
    ok: bool
    status: str
    reason: str = ""
    latency_ms: int = 0
    degraded: bool = False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(
        self,
        options: RunOptions,
        *,
        bus: Optional[EventBus] = None,
        gate: Optional[ApprovalGate] = None,
        memory: Optional[MemoryStore] = None,
    ) -> None:
        self.options = options
        self.goal = options.goal.strip()
        self.cfg = self._build_config()
        self.bus = bus or EventBus()
        self.gate = gate or ApprovalGate(
            mode="auto" if self.cfg.auto_approve else "interactive",
            timeout=self.cfg.approval_timeout,
        )
        if self.gate.emit is None:
            self.gate.emit = self.bus.emit
        self.memory = memory if memory is not None else (
            MemoryStore() if self.cfg.use_memory else None
        )
        self.tools: ToolRegistry = build_registry(self.memory, self.cfg)
        self.llm = LLM(self.cfg, on_event=self._on_llm_event)
        self.trace: Optional[RunTrace] = None
        self.run_dir: Optional[Path] = None
        self.run_id = options.run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        self.started_at = time.time()
        self.started_iso = datetime.now().isoformat(timespec="seconds")
        self.cancel_event = threading.Event()
        self._lock = threading.RLock()

        self.request: Optional[TravelRequest] = None
        self.plan: Optional[Plan] = None
        self.initial_plan: Optional[Plan] = None
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.results: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self._warning_set: Set[str] = set()
        self.review_notes: List[str] = []
        self.critique: Dict[str, Any] = {}
        self.verifier: Dict[str, Any] = {}
        self.narrative: str = ""
        self.questions: List[str] = []
        self.memory_updates: List[dict] = []
        self.facts: Dict[str, Any] = {}
        self.stages: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {
            "replans": 0,
            "retries": 0,
            "tool_calls": 0,
            "parallel_rounds": 0,
            "optimizations": 0,
            "repairs": 0,
            "agent_turns": 0,
        }
        self.final_status = "running"
        self.approval_denied = False
        self.cancelled = False
        self._last_denied = False
        self._agent_history_value: List[Dict[str, Any]] = []
        self._memory_notes: List[str] = []
        self._current_stage: Optional[str] = None

    # -- setup -----------------------------------------------------------
    def _build_config(self) -> Config:
        cfg = Config(mock=self.options.mock)
        if self.options.model:
            cfg.model = self.options.model
        if self.options.fast_model:
            cfg.fast_model = self.options.fast_model
        if self.options.thinking is not None:
            cfg.thinking = self.options.thinking
        cfg.use_memory = self.options.use_memory
        cfg.auto_approve = self.options.auto_approve
        if self.options.max_replans is not None:
            cfg.max_replans = max(0, self.options.max_replans)
        if not self.options.replan:
            cfg.max_replans = 0
        return cfg

    def _on_llm_event(self, event_type: str, **data: Any) -> None:
        if self.trace is not None:
            self.trace.add(event_type, **data)

    # -- events ----------------------------------------------------------
    def emit(self, event_type: str, **data: Any) -> dict:
        assert self.trace is not None
        return self.trace.add(event_type, **data)

    def add_warning(self, text: str, source: str = "") -> None:
        text = str(text or "").strip()
        if not text:
            return
        with self._lock:
            key = re.sub(r"\s+", "", text)
            if key in self._warning_set:
                return
            self._warning_set.add(key)
            self.warnings.append(text)
        if self.trace is not None:
            self.trace.add("warning", message=text, source=source)

    def _begin_stage(self, name: str, label: str = "") -> None:
        self._current_stage = name
        self.stages.append(
            {
                "name": name,
                "label": label or name,
                "started": time.perf_counter(),
                "status": "running",
                "duration_ms": 0,
            }
        )
        self.emit("stage_start", stage=name, label=label or name)

    def _end_stage(self, name: str, status: str = "ok", detail: str = "") -> None:
        for stage in reversed(self.stages):
            if stage["name"] == name and stage["status"] == "running":
                stage["duration_ms"] = int((time.perf_counter() - stage["started"]) * 1000)
                stage["status"] = status
                stage["detail"] = detail[:300]
                self.emit(
                    "stage_end",
                    stage=name,
                    status=status,
                    duration_ms=stage["duration_ms"],
                    detail=detail[:300],
                )
                break
        self._current_stage = None

    # -- main ------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        ensure_dirs()
        self.run_dir = RUNS_DIR / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace = RunTrace(self.bus, self.run_dir / "trace.jsonl")
        self.emit(
            "run_start",
            run_id=self.run_id,
            goal=self.goal,
            mode=self.options.mode,
            model=self.cfg.model,
            fast_model=self.cfg.fast_model,
            mock=self.cfg.mock,
            version=VERSION,
            options=self.options.to_dict(),
        )
        narrative = ""
        verifier: Dict[str, Any] = {}
        report = ""
        facts: Dict[str, Any] = {}
        try:
            request, intake_meta = self._intake_stage()
            self.request = request
            self.questions = list(request.questions)

            memory_context = self._memory_context()

            if self.options.mode == "agent":
                narrative, facts, verifier = self._run_agent_mode(request, memory_context)
                if self.plan is None:
                    self.plan = self._agent_plan_from_results(request)
            else:
                self._plan_stage(request, memory_context)
                self._critique_stage(request)
                self._execute_dag(request)
                if self.options.verify:
                    self._verify_stage(request)
                else:
                    self.verifier = {}
                    self.emit("verifier_skipped", reason="消融：验收阶段已禁用")
                verifier = self.verifier
                narrative, facts = self._narrate_stage(request)

            self.facts = facts
            self.verifier = verifier
            status = self._final_status()
            self.final_status = status
            self._memory_stage(request, facts, status)
            report = build_report(self, narrative=narrative, verifier=verifier)
            self._write_artifacts(report)
            duration = int((time.time() - self.started_at) * 1000)
            payload = self._result_payload(status, report, narrative, verifier, facts, duration)
            self._write_result(payload)
            self.emit(
                "run_end",
                run_id=self.run_id,
                status=status,
                summary=self._run_summary(status, facts),
                metrics=payload["metrics"],
                warnings=self.warnings,
                questions=self.questions,
                artifacts=payload["artifacts"],
                memory_updates=self.memory_updates,
            )
            self._flush_trace()
            return payload
        except Exception as exc:  # pragma: no cover - 防御性兜底
            self.final_status = "error"
            detail = f"{type(exc).__name__}: {exc}"
            self.emit("run_error", error=detail, traceback=traceback.format_exc()[-4000:])
            report = build_report(
                self,
                narrative=f"## 结论\n\n- 运行异常终止：{detail}\n",
                verifier={},
                error=detail,
            )
            self._write_artifacts(report)
            duration = int((time.time() - self.started_at) * 1000)
            payload = self._result_payload("error", report, "", {}, {}, duration, error=detail)
            self._write_result(payload)
            self.emit("run_end", run_id=self.run_id, status="error", summary=detail)
            self._flush_trace()
            return payload
        finally:
            self.bus.close()

    # -- stages ----------------------------------------------------------
    def _intake_stage(self) -> Tuple[TravelRequest, Any]:
        self._begin_stage("intake", "解析目标")
        agent = IntakeAgent(self.llm, self.cfg)
        request, meta = agent.run(self.goal)
        self.emit(
            "intake",
            request=request.to_dict(),
            meta=meta.to_dict(),
            questions=request.questions,
            assumptions=request.assumptions,
            interpretation=self._interpret(request),
        )
        self._end_stage("intake", "ok", request.route)
        return request, meta

    @staticmethod
    def _interpret(request: TravelRequest) -> str:
        prefs = "、".join(request.preferences) if request.preferences else "无特别偏好"
        return (
            f"{request.route}，{request.depart} 至 {request.return_date}，"
            f"{request.nights} 晚，预算 ¥{request.budget}，{prefs}"
        )

    def _memory_context(self) -> str:
        if self.memory is None:
            self.emit("memory_context", enabled=False, context="")
            return ""
        # 从本次目标里先抽取高置信度偏好（不调用模型）。
        for item in self.memory.infer_from_goal(self.goal):
            self.memory.set_preference(
                item["key"], item["value"], evidence=item.get("evidence", ""), source="rule"
            )
        context = self.memory.context_text()
        self.emit(
            "memory_context",
            enabled=True,
            context=context,
            preferences=self.memory.preferences(),
            episodes=self.memory.recent_episodes(limit=3),
        )
        return context

    def _plan_stage(self, request: TravelRequest, memory_context: str) -> None:
        self._begin_stage("plan", "DAG 规划")
        planner = PlannerAgent(self.llm, self.cfg, self.tools)
        plan, meta = planner.plan(
            self.goal,
            request,
            memory_context=memory_context,
            inject_failure=self.options.inject_failure,
        )
        self.plan = plan
        self.initial_plan = plan
        normalized = normalize_travel_plan(plan, request)
        if normalized:
            self.emit("plan_normalized", changes=normalized, message="；".join(normalized))
        errors = validate_plan(plan, self.tools, max_steps=self.cfg.max_steps)
        if errors:
            self.emit("plan_invalid", errors=errors)
            self._end_stage("plan", "fallback", "；".join(errors[:3]))
        else:
            self._end_stage("plan", "ok", f"{len(plan.steps)} 步 · {plan.created_by}")
        (self.run_dir / "plan.initial.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.run_dir / "plan.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.emit(
            "plan",
            plan=plan.to_dict(),
            meta=meta.to_dict(),
            fallback=meta.fallback,
            levels=[[s.id for s in level] for level in self._safe_levels()],
        )
        if self.options.inject_failure:
            for step in plan.steps:
                if step.tool == "search_flights":
                    old = step.args.get("max_price")
                    step.args["max_price"] = 500
                    self.emit(
                        "injection",
                        step_id=step.id,
                        kind="flight_empty_result",
                        message="演示注入：去程航班价格上限压到 ¥500，预期触发空结果与重规划",
                        previous=old,
                    )
                    break

    def _critique_stage(self, request: TravelRequest) -> None:
        self._begin_stage("critique", "计划审查")
        critic = CriticAgent(self.llm, self.cfg, self.tools)
        assert self.plan is not None
        result, meta = critic.review(self.goal, request, self.plan)
        self.critique = result
        for issue in result.get("issues") or []:
            note = f"{issue.get('step', '-')}: {issue.get('message', '')}"
            self.review_notes.append(note)
            self.emit(
                "critique_issue",
                step=issue.get("step"),
                severity=issue.get("severity", "low"),
                message=issue.get("message", ""),
            )
        # 自动修复审查发现的结构性问题。
        if any("审批" in str(i.get("message", "")) for i in result.get("issues") or []):
            if self.plan and not any(s.tool == "request_approval" for s in self.plan.steps):
                ensure_approval_step(self.plan)
                self.emit("plan_patch", reason="Critic 发现缺少审批步骤，已自动补充")
        self._end_stage("critique", "ok", f"{result.get('verdict', 'accept')} · {len(self.review_notes)} 条意见")
        self.emit(
            "critique",
            critique=result,
            meta=meta.to_dict(),
            risk_notes=result.get("risk_notes") or [],
            suggestions=result.get("suggestions") or [],
        )

    def _execute_dag(self, request: TravelRequest) -> None:
        self._begin_stage("execute", "并行执行 DAG")
        assert self.plan is not None
        replans = 0
        while True:
            if self.cancel_event.is_set():
                self.cancelled = True
                self._skip_remaining("运行已取消")
                break
            pending = [s for s in self.plan.steps if s.id not in self.statuses]
            if not pending:
                break
            ready: List[Step] = []
            blocked: List[Step] = []
            for step in pending:
                deps = [self.statuses.get(dep, {}) for dep in step.depends_on]
                if any(dep.get("status") in ("failed", "skipped") for dep in deps):
                    blocked.append(step)
                elif all(dep.get("status") in ("ok", "degraded") for dep in deps):
                    ready.append(step)
            if blocked:
                for step in blocked:
                    self._set_status(step, "skipped", "依赖步骤未成功")
                    self.emit(
                        "step_end", step_id=step.id, status="skipped", reason="依赖步骤未成功"
                    )
                continue
            if not ready:
                for step in pending:
                    self._set_status(step, "skipped", "依赖链无法继续")
                    self.emit("step_end", step_id=step.id, status="skipped", reason="依赖链中断")
                break
            outcomes = self._run_level(ready, request)
            failed = next((o for o in outcomes if not o.ok), None)
            if failed is None:
                continue
            if failed.status == "skipped":
                # 人工拒绝/取消：不重规划，尊重人的决定。
                self.approval_denied = self.approval_denied or self._last_denied
                self._skip_remaining("人工未批准，停止后续动作")
                break
            if replans >= self.cfg.max_replans:
                self.emit("replan_exhausted", reason=failure_reason(failed), max_replans=replans)
                self._skip_remaining("重规划次数已用尽")
                break
            if self._replan(request, failed, replans + 1):
                replans += 1
                self.metrics["replans"] = replans
                continue
            self._skip_remaining("重规划未产生可执行计划")
            break
        self.metrics["steps_total"] = len(self.plan.steps) if self.plan else 0
        self._end_stage(
            "execute",
            "ok" if not self._has_failed() else "partial",
            f"{self._count_status('ok')} 成功 · {self._count_status('failed')} 失败",
        )

    def _run_level(self, steps: List[Step], request: TravelRequest) -> List[StepOutcome]:
        if len(steps) == 1:
            return [self._run_step(steps[0], request)]
        if not self.options.parallel:
            # 消融/基线：同层退化为串行流水线，供“并行 vs 串行”对比。
            self.emit("level_start", steps=[s.id for s in steps], count=len(steps), parallel=False)
            return [self._run_step(step, request) for step in steps]
        self.metrics["parallel_rounds"] += 1
        self.emit("level_start", steps=[s.id for s in steps], count=len(steps), parallel=True)
        started = time.perf_counter()
        outcomes: List[StepOutcome] = []
        with ThreadPoolExecutor(max_workers=min(4, len(steps))) as executor:
            futures = {executor.submit(self._run_step, step, request): step for step in steps}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:  # pragma: no cover
                    self._set_status(step, "failed", f"执行异常：{exc}")
                    self.emit(
                        "step_end", step_id=step.id, status="failed", reason=f"执行异常：{exc}"
                    )
                    outcomes.append(
                        StepOutcome(step.id, False, "failed", f"执行异常：{exc}")
                    )
        self.emit(
            "level_end",
            steps=[s.id for s in steps],
            duration_ms=int((time.perf_counter() - started) * 1000),
            saved_ms=max(
                0,
                sum(o.latency_ms for o in outcomes)
                - int((time.perf_counter() - started) * 1000),
            ),
        )
        return outcomes

    def _run_step(self, step: Step, request: TravelRequest) -> StepOutcome:
        started = time.perf_counter()
        self.emit(
            "step_start",
            step_id=step.id,
            goal=step.goal,
            tool=step.tool,
            risk=step.risk,
            depends_on=step.depends_on,
        )
        if self.cancel_event.is_set():
            self._set_status(step, "skipped", "运行已取消")
            self.emit("step_end", step_id=step.id, status="skipped", reason="运行已取消")
            return StepOutcome(step.id, True, "skipped", "运行已取消")

        try:
            args = resolve(step.args, self.results)
        except KeyError as exc:
            self._set_status(step, "failed", f"参数引用失败：{exc}")
            self.emit("step_end", step_id=step.id, status="failed", reason=str(exc))
            return StepOutcome(step.id, False, "failed", str(exc))

        if step.risk >= 2:
            approval = self.gate.create(
                step,
                action=step.goal or step.tool,
                detail=self._approval_detail(step, args),
                amount=_to_float(args.get("amount")),
                risk=step.risk,
            )
            self.emit("approval_required", **approval.public())
            approved = self.gate.wait(approval, self.cancel_event)
            self.emit(
                "approval_result",
                approval_id=approval.id,
                step_id=step.id,
                approved=approved,
                status=approval.status,
                note=approval.note,
            )
            if not approved:
                self._last_denied = approval.status in ("rejected", "timeout")
                self.approval_denied = True
                reason = "审批超时" if approval.status == "timeout" else "人工未批准"
                self._set_status(step, "skipped", reason)
                self.emit("step_end", step_id=step.id, status="skipped", reason=reason)
                return StepOutcome(step.id, True, "skipped", reason)

        out: Any = None
        last_error = ""
        for attempt in (1, 2):
            attempt_started = time.perf_counter()
            self.metrics["tool_calls"] += 1
            self.emit(
                "tool_call",
                step_id=step.id,
                tool=step.tool,
                args=_safe_args(args),
                attempt=attempt,
            )
            try:
                out = self.tools.call(step.tool, args)
            except Exception as exc:
                latency = int((time.perf_counter() - attempt_started) * 1000)
                last_error = f"{type(exc).__name__}: {exc}"
                self.emit(
                    "tool_result",
                    step_id=step.id,
                    tool=step.tool,
                    ok=False,
                    latency_ms=latency,
                    attempt=attempt,
                    error=last_error[:1000],
                )
                # 只读工具自动重试一次；写操作不重试，避免重复副作用。
                if attempt == 1 and step.risk == 0:
                    self.metrics["retries"] += 1
                    time.sleep(0.25)
                    continue
                break
            latency = int((time.perf_counter() - attempt_started) * 1000)
            self.emit(
                "tool_result",
                step_id=step.id,
                tool=step.tool,
                ok=True,
                latency_ms=latency,
                attempt=attempt,
                summary=summarize_value(out, 900),
            )
            break

        if out is None:
            reason = f"执行失败：{last_error or '工具返回空'}"
            self._set_status(step, "failed", reason, latency_ms=0)
            self.emit("step_end", step_id=step.id, status="failed", reason=reason)
            return StepOutcome(step.id, False, "failed", reason)

        if self.options.verify:
            ok, reason, extra = self._verify(step, out, request)
        else:  # 消融：跳过确定性校验，工具输出直接采信（不触发步骤内修复）
            ok, reason, extra = True, "校验已禁用", {"warnings": [], "degraded": False}
        self.emit(
            "verify",
            step_id=step.id,
            ok=ok,
            reason=reason,
            source="deterministic" if self.options.verify else "disabled",
            details=extra,
        )
        if not ok:
            repaired, out2, reason2, extra2, notes = self._auto_repair(
                step, args, out, reason, request
            )
            if repaired:
                self.metrics["repairs"] = self.metrics.get("repairs", 0) + 1
                self.emit(
                    "repair",
                    step_id=step.id,
                    changes=notes,
                    before=reason,
                    after=reason2,
                )
                out = out2 if out2 is not None else out
                ok2, reason2_new, extra2_new = self._verify(step, out, request)
                degraded_flag = bool(
                    extra.get("degraded")
                    or (extra2 or {}).get("degraded")
                    or (extra2_new or {}).get("degraded")
                )
                extra = {**extra, **(extra2 or {}), **(extra2_new or {})}
                extra["degraded"] = degraded_flag
                if degraded_flag and not ok2:
                    # 预算无解等情况：保留最接近预算的方案，标记降级并交给审批。
                    ok, reason = True, reason2_new
                else:
                    ok, reason = ok2, reason2_new
                self.emit(
                    "verify",
                    step_id=step.id,
                    ok=ok,
                    reason=reason,
                    source="deterministic",
                    after_repair=True,
                    degraded=bool(extra.get("degraded")),
                )
            else:
                self.emit("repair_failed", step_id=step.id, reason=reason)

        for warning in extra.get("warnings") or []:
            self.add_warning(warning, source=step.id)
        degraded = bool(extra.get("degraded"))
        latency = int((time.perf_counter() - started) * 1000)

        with self._lock:
            self.results[step.id] = out
        if not ok:
            self.add_warning(reason, source=step.id)
            self._set_status(step, "failed", reason, latency_ms=latency)
            self.emit(
                "step_end",
                step_id=step.id,
                status="failed",
                reason=reason,
                latency_ms=latency,
                summary=summarize_value(out, 300),
            )
            return StepOutcome(step.id, False, "failed", reason, latency)

        status = "degraded" if degraded else "ok"
        self._set_status(step, status, reason, latency_ms=latency)
        self.emit(
            "step_end",
            step_id=step.id,
            status=status,
            reason=reason,
            latency_ms=latency,
            summary=summarize_value(out, 500),
        )
        return StepOutcome(step.id, True, status, reason, latency, degraded)

    # -- verification ----------------------------------------------------
    def _verify(self, step: Step, out: Any, request: TravelRequest) -> Tuple[bool, str, Dict[str, Any]]:
        extra: Dict[str, Any] = {"warnings": [], "degraded": False}
        if out is None:
            return False, "工具返回空", extra
        tool = step.tool
        if tool == "search_flights":
            if not out.get("options"):
                coverage = out.get("coverage") or {}
                if coverage.get("supported") is False:
                    return (
                        False,
                        f"数据源未覆盖 {out.get('origin')} → {out.get('destination')}，"
                        "不会编造航班",
                        extra,
                    )
                return False, "没有满足当前条件的航班", extra
            best = out.get("best") or out.get("cheapest")
            if isinstance(best, dict):
                check = policy.compliance(
                    outbound=best if out.get("origin") == request.origin else None,
                    inbound=best if out.get("origin") != request.origin else None,
                )
                for item in (check.get("warnings") or []) + (check.get("violations") or []):
                    extra["warnings"].append(
                        f"{step.id} {out.get('origin')}→{out.get('destination')}：{item['message']}"
                    )
        elif tool == "search_hotels":
            if not out.get("options"):
                return False, "没有满足当前条件的酒店", extra
            best = out.get("best") or out.get("cheapest")
            if isinstance(best, dict):
                check = policy.compliance(hotel=best, city=out.get("city", ""), nights=out.get("nights", 1))
                for item in (check.get("warnings") or []) + (check.get("violations") or []):
                    extra["warnings"].append(f"{step.id} 酒店：{item['message']}")
        elif tool == "check_policy":
            if not out.get("clauses"):
                return False, "没有检索到政策条款", extra
        elif tool == "compute_trip_cost":
            try:
                value = float(out.get("total", 0))
            except Exception:
                return False, "费用计算结果不是数字", extra
            expected = (
                float(out.get("outbound_price", 0))
                + float(out.get("return_price", 0))
                + float(out.get("hotel_total", 0))
                + float(out.get("local_transport", 0))
            )
            if abs(value - expected) > 0.01:
                return False, f"费用合计 {value:.0f} 与明细不一致（应为 {expected:.0f}）", extra
            if value > request.budget:
                return False, f"总费用 ¥{value:.0f} 超过预算 ¥{request.budget}", extra
        elif tool == "draft_itinerary":
            markdown = str(out.get("markdown", ""))
            for section in ("交通", "住宿", "费用", "合规检查"):
                if section not in markdown:
                    return False, f"行程草案缺少「{section}」段", extra
            compliance = out.get("compliance") or {}
            if not isinstance(compliance, dict):
                compliance = {}
            for item in (compliance.get("violations") or []) + (compliance.get("warnings") or []):
                extra["warnings"].append(str(item.get("message", "")))
            if compliance.get("violations"):
                extra["degraded"] = True
            facts = out.get("facts") or {}
            for cost_id, result in self.results.items():
                if isinstance(result, dict) and "total" in result and cost_id in {
                    s.id for s in (self.plan.steps if self.plan else []) if s.tool == "compute_trip_cost"
                }:
                    try:
                        if facts.get("total") is not None and abs(
                            float(facts["total"]) - float(result["total"])
                        ) > 0.01:
                            return (
                                False,
                                f"行程单总额 {facts['total']} 与费用工具 {result['total']} 不一致",
                                extra,
                            )
                    except Exception:
                        pass
                    break
        elif tool == "request_approval":
            if not out.get("approved"):
                return False, "审批未通过", extra
        return True, "ok", extra

    def _auto_repair(
        self,
        step: Step,
        args: Dict[str, Any],
        out: Any,
        reason: str,
        request: TravelRequest,
    ) -> Tuple[bool, Any, str, Dict[str, Any], List[str]]:
        notes: List[str] = []
        extra: Dict[str, Any] = {"warnings": [], "degraded": False}
        if step.tool in ("search_flights", "search_hotels"):
            relaxed = dict(args)
            for key in ("max_price", "depart_after", "depart_before", "direct_only", "min_star", "max_distance_km"):
                relaxed.pop(key, None)
            if step.tool == "search_flights":
                relaxed["cabin"] = "economy"
            if relaxed == args:
                return False, out, reason, extra, notes
            self.emit(
                "repair_attempt",
                step_id=step.id,
                tool=step.tool,
                changes={k: v for k, v in relaxed.items() if args.get(k) != v},
                cause=reason,
            )
            try:
                new_out = self.tools.call(step.tool, relaxed)
            except Exception as exc:
                notes.append(f"放宽条件后仍失败：{exc}")
                return False, out, reason, extra, notes
            if isinstance(new_out, dict) and new_out.get("options"):
                notes.append("移除价格/时段等过滤条件后重新检索")
                extra["warnings"].append(
                    f"{step.id} 原检索条件过严，已自动放宽条件；结果可能偏离原始偏好"
                )
                return True, new_out, "修复后通过", extra, notes
            return False, out, reason, extra, notes

        if step.tool == "compute_trip_cost":
            optimized = self._optimize_budget(request, step, out)
            if optimized is not True:
                return False, out, reason, extra, notes
            cost = self.results.get(step.id)
            ok, new_reason, new_extra = self._verify(step, cost, request)
            notes.append("预算优化器重新组合往返航班与酒店")
            extra["warnings"].extend(new_extra.get("warnings") or [])
            extra["degraded"] = bool(new_extra.get("degraded"))
            if not ok and float(cost.get("total", 0)) > request.budget:
                # 没有更便宜的可行组合：保留最接近预算的方案，显式降级而不是静默失败。
                extra["degraded"] = True
                extra["warnings"].append(
                    f"已尝试最便宜组合仍超预算 ¥{float(cost.get('total', 0)) - request.budget:.0f}，"
                    "方案降级并提交人工审批"
                )
                return True, cost, "降级：超预算，需人工审批", extra, notes
            return ok, cost, new_reason, extra, notes

        if step.tool == "draft_itinerary":
            rebuilt = self._rebuild_draft(step, request, args)
            if rebuilt is not None:
                notes.append("用已确认的航班、酒店与费用重新生成行程单")
                return True, rebuilt, "重建后通过", extra, notes
        return False, out, reason, extra, notes

    def _optimize_budget(
        self, request: TravelRequest, cost_step: Step, old_cost: Any
    ) -> bool:
        plan = self.plan
        if plan is None:
            return False
        flight_steps = [s for s in plan.steps if s.tool == "search_flights"]
        hotel_step = next((s for s in plan.steps if s.tool == "search_hotels"), None)
        if len(flight_steps) < 2 or hotel_step is None:
            return False
        out_step = next(
            (s for s in flight_steps if self._step_results(s.id).get("origin") == request.origin),
            flight_steps[0],
        )
        in_step = next(
            (s for s in flight_steps if s.id != out_step.id),
            flight_steps[-1],
        )
        out_res = self._step_results(out_step.id)
        in_res = self._step_results(in_step.id)
        hotel_res = self._step_results(hotel_step.id)
        out_options = [o for o in (out_res.get("options") or []) if isinstance(o, dict)]
        in_options = [o for o in (in_res.get("options") or []) if isinstance(o, dict)]
        hotel_options = [o for o in (hotel_res.get("options") or []) if isinstance(o, dict)]
        if not out_options or not in_options or not hotel_options:
            return False
        nights = request.nights
        combos = []
        limit = policy.hotel_limit(request.destination)
        for outbound in out_options[:6]:
            for inbound in in_options[:6]:
                for hotel in hotel_options[:6]:
                    total = (
                        float(outbound.get("price", 0))
                        + float(inbound.get("price", 0))
                        + float(hotel.get("price", 0)) * nights
                    )
                    penalty = 0.0
                    for flight in (outbound, inbound):
                        hour = _hour(flight.get("depart", "12:00"))
                        if hour < 7 or hour >= 21:
                            penalty += 300
                        if flight.get("refundable") is False:
                            penalty += 100
                        if flight.get("stops"):
                            penalty += 400
                    distance = float(hotel.get("distance_km", 0) or 0)
                    if distance > 3:
                        penalty += 200 + (distance - 3) * 40
                    if float(hotel.get("price", 0)) > limit:
                        penalty += 400
                    if hotel.get("refundable") is False:
                        penalty += 100
                    if int(hotel.get("star", 3)) < 3:
                        penalty += 120
                    combos.append(
                        {
                            "outbound": outbound,
                            "inbound": inbound,
                            "hotel": hotel,
                            "total": round(total, 2),
                            "quality": round(total + penalty, 2),
                        }
                    )
        if not combos:
            return False
        under = [c for c in combos if c["total"] <= request.budget]
        chosen = min(under, key=lambda c: c["quality"]) if under else min(combos, key=lambda c: c["total"])

        # 更新被引用步骤的结果，让后续 draft_itinerary 使用新选择。
        self._patch_search_result(out_step, out_res, chosen["outbound"], "outbound")
        self._patch_search_result(in_step, in_res, chosen["inbound"], "inbound")
        self._patch_search_result(hotel_step, hotel_res, chosen["hotel"], "hotel")

        new_cost = compute_trip_cost(
            outbound_price=float(chosen["outbound"]["price"]),
            return_price=float(chosen["inbound"]["price"]),
            hotel_price=float(chosen["hotel"]["price"]),
            nights=nights,
            budget=request.budget,
        )
        with self._lock:
            self.results[cost_step.id] = new_cost
        self.metrics["optimizations"] += 1
        try:
            old_total = float((old_cost or {}).get("total", 0))
        except Exception:
            old_total = 0.0
        compliance_check = policy.compliance(
            outbound=chosen["outbound"],
            inbound=chosen["inbound"],
            hotel=chosen["hotel"],
            total_cost=new_cost["total"],
            budget=request.budget,
            city=request.destination,
            nights=nights,
        )
        for item in (compliance_check.get("warnings") or []) + (
            compliance_check.get("violations") or []
        ):
            self.add_warning(item["message"], source="budget-optimizer")
        self.emit(
            "optimization",
            step_id=cost_step.id,
            before=old_total,
            after=new_cost["total"],
            delta=round(new_cost["total"] - old_total, 2),
            under_budget=bool(under),
            selection={
                "outbound": chosen["outbound"].get("flight_no"),
                "inbound": chosen["inbound"].get("flight_no"),
                "hotel": chosen["hotel"].get("name"),
                "hotel_price": chosen["hotel"].get("price"),
                "distance_km": chosen["hotel"].get("distance_km"),
                "quality": chosen["quality"],
            },
            compliance=compliance_check,
            message=(
                f"预算优化：¥{old_total:,.0f} → ¥{new_cost['total']:,.0f}"
                + ("（预算内）" if under else "（仍超预算，改为降级 + 审批）")
            ),
        )
        return True

    def _patch_search_result(
        self, step: Step, result: Dict[str, Any], selected: Dict[str, Any], kind: str
    ) -> None:
        if not isinstance(result, dict) or not isinstance(selected, dict):
            return
        patched = dict(result)
        patched["best"] = selected
        options = [o for o in (result.get("options") or []) if isinstance(o, dict)]
        if options and all(o.get("price") is not None for o in options):
            patched["cheapest"] = min(options, key=lambda o: float(o.get("price", 0)))
        patched["selected_by"] = "budget-optimizer"
        patched["original_best"] = result.get("best")
        with self._lock:
            self.results[step.id] = patched
        self.emit(
            "step_patched",
            step_id=step.id,
            tool=step.tool,
            selection=selected.get("flight_no") or selected.get("name"),
            price=selected.get("price"),
            kind=kind,
        )

    def _rebuild_draft(
        self, step: Step, request: TravelRequest, args: Dict[str, Any]
    ) -> Optional[dict]:
        plan = self.plan
        if plan is None:
            return None
        flights = [s for s in plan.steps if s.tool == "search_flights"]
        hotel_step = next((s for s in plan.steps if s.tool == "search_hotels"), None)
        policy_step = next((s for s in plan.steps if s.tool == "check_policy"), None)
        cost_step = next((s for s in plan.steps if s.tool == "compute_trip_cost"), None)
        out_step = next(
            (s for s in flights if self._step_results(s.id).get("origin") == request.origin),
            flights[0] if flights else None,
        )
        in_step = next((s for s in flights if out_step and s.id != out_step.id), None)
        data = {
            "goal": self.goal,
            "outbound": (self._step_results(out_step.id).get("best") if out_step else None),
            "inbound": (self._step_results(in_step.id).get("best") if in_step else None),
            "hotel": (self._step_results(hotel_step.id).get("best") if hotel_step else None),
            "policy": self._step_results(policy_step.id) if policy_step else None,
            "cost": self._step_results(cost_step.id) if cost_step else None,
            "budget": request.budget,
            "nights": request.nights,
        }
        try:
            rebuilt = self.tools.call("draft_itinerary", data)
        except Exception as exc:
            self.emit("repair_failed", step_id=step.id, reason=f"重建行程单失败：{exc}")
            return None
        return rebuilt

    def _replan(self, request: TravelRequest, failed: StepOutcome, round_index: int) -> bool:
        assert self.plan is not None
        step = self.plan.by_id(failed.step_id)
        if step is None:
            return False
        with self._lock:
            completed = {
                sid: state.get("status", "")
                for sid, state in self.statuses.items()
                if state.get("status") in ("ok", "degraded")
            }
        self._begin_stage(f"replan-{round_index}", "失败重规划")
        self.emit(
            "replan",
            round=round_index,
            step_id=failed.step_id,
            reason=failed.reason,
            completed=list(completed),
            message=f"{failed.step_id} 失败：{failed.reason}；请求 LLM 重规划",
        )
        agent = ReplannerAgent(self.llm, self.cfg, self.tools)
        revised, meta = agent.replan(
            goal=self.goal,
            request=request,
            plan=self.plan,
            failed_step=failed.step_id,
            reason=failed.reason,
            results=self.results,
            completed=completed,
            warnings=self.warnings,
            round_index=round_index,
        )
        if revised is None:
            self.emit(
                "replan_failed",
                step_id=failed.step_id,
                reason=meta.note or "模型未返回可执行计划",
            )
            self._end_stage(f"replan-{round_index}", "failed", meta.note)
            return False
        # 保留已完成步骤的原始定义与结果，避免重规划篡改已发生的事实。
        original = {s.id: s for s in self.plan.steps}
        new_steps: List[Step] = []
        for step_item in revised.steps:
            if step_item.id in completed and step_item.id in original:
                new_steps.append(original[step_item.id])
            else:
                new_steps.append(step_item)
        existing = {s.id for s in new_steps}
        for step_item in self.plan.steps:
            if step_item.id in completed and step_item.id not in existing:
                new_steps.insert(0, step_item)
                existing.add(step_item.id)
        new_steps = ensure_approval_step(Plan(goal=revised.goal, steps=new_steps)).steps
        candidate = Plan(
            goal=revised.goal or self.plan.goal,
            steps=new_steps,
            assumptions=revised.assumptions or self.plan.assumptions,
            strategy="dag",
            created_by="replanner",
            notes=revised.notes,
        )
        normalized = normalize_travel_plan(candidate, request)
        if normalized:
            self.emit("plan_normalized", changes=normalized, message="；".join(normalized))
        errors = validate_plan(candidate, self.tools, max_steps=self.cfg.max_steps)
        if errors:
            self.emit("replan_failed", reason="校验失败：" + "；".join(errors[:3]))
            self._end_stage(f"replan-{round_index}", "failed", "；".join(errors[:3]))
            return False
        # 清掉失败/被移除步骤的状态与结果，保留已完成步骤。
        valid_ids = {s.id for s in candidate.steps}
        with self._lock:
            for sid in list(self.statuses):
                if sid not in valid_ids and sid not in completed:
                    self.statuses.pop(sid, None)
                    self.results.pop(sid, None)
            for sid in list(self.statuses):
                if sid not in completed and self.statuses[sid].get("status") in ("failed", "skipped"):
                    self.statuses.pop(sid, None)
                    self.results.pop(sid, None)
        self.plan = candidate
        (self.run_dir / f"plan.replan-{round_index}.json").write_text(
            json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.emit(
            "plan_revised",
            round=round_index,
            plan=candidate.to_dict(),
            revision_reason=revised.notes,
            added=[s.id for s in candidate.steps if s.id not in original],
            removed=[sid for sid in original if sid not in valid_ids],
            message=f"重规划完成：{len(candidate.steps)} 步，继续执行未完成部分",
        )
        self._end_stage(f"replan-{round_index}", "ok", revised.notes or "已修订计划")
        return True

    def _skip_remaining(self, reason: str) -> None:
        if self.plan is None:
            return
        for step in self.plan.steps:
            if step.id not in self.statuses:
                self._set_status(step, "skipped", reason)
                self.emit("step_end", step_id=step.id, status="skipped", reason=reason)

    def _verify_stage(self, request: TravelRequest) -> None:
        draft = self._draft_result()
        if not isinstance(draft, dict):
            self.verifier = {}
            return
        self._begin_stage("verify", "结果验收")
        agent = VerifierAgent(self.llm, self.cfg)
        result, meta = agent.review(self.goal, request, draft, self.warnings)
        self.verifier = result
        if result.get("ok") is False:
            for issue in result.get("issues") or []:
                self.add_warning(f"验收：{issue}", source="verifier")
        else:
            for issue in result.get("issues") or []:
                self.review_notes.append(f"验收建议：{issue}")
        self._end_stage("verify", "ok", f"score {result.get('score')}")
        self.emit("verifier", verifier=result, meta=meta.to_dict())

    def _narrate_stage(self, request: TravelRequest) -> Tuple[str, Dict[str, Any]]:
        self._begin_stage("narrate", "生成结论")
        facts = self._collect_facts(request)
        metrics = self._metrics_snapshot()
        emitted = {"chars": 0, "buffer": ""}

        def on_token(delta: str) -> None:
            emitted["buffer"] += delta
            emitted["chars"] += len(delta)
            if len(emitted["buffer"]) >= 40:
                self.emit("narrator_delta", delta=emitted["buffer"])
                emitted["buffer"] = ""

        agent = NarratorAgent(self.llm, self.cfg)
        narrative, meta = agent.narrate(
            goal=self.goal,
            request=request,
            facts=facts,
            warnings=self.warnings,
            metrics=metrics,
            on_token=on_token if self.llm.available else None,
        )
        if emitted["buffer"]:
            self.emit("narrator_delta", delta=emitted["buffer"])
        self.narrative = narrative
        self._end_stage("narrate", "ok" if not meta.fallback else "fallback", f"{len(narrative)} 字")
        self.emit("narrative", markdown=narrative, meta=meta.to_dict())
        return narrative, facts

    # -- agent mode ------------------------------------------------------
    def _run_agent_mode(
        self, request: TravelRequest, memory_context: str
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        self._begin_stage("agent", "自主 Agent（Function Calling）")
        tools = self.tools.openai_tools()
        system = _agent_system_prompt(self.tools, request, memory_context)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"目标：{self.goal}\n"
                    f"已解析约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                    "请通过工具完成差旅编排，最后给出 Markdown 结论。"
                ),
            },
        ]
        tool_history: List[Dict[str, Any]] = []
        final_text = ""
        errors = 0
        max_turns = 8
        for turn in range(1, max_turns + 1):
            if self.cancel_event.is_set():
                self.cancelled = True
                break
            self.metrics["agent_turns"] = turn
            self.emit("agent_turn", turn=turn, max_turns=max_turns)
            try:
                result = self.llm.chat(
                    messages,
                    role="agent",
                    temperature=0.2,
                    max_tokens=1600,
                    thinking=False,
                    tools=tools,
                    tool_choice="auto",
                    allow_tool_calls=True,
                    retries=1,
                )
            except Exception as exc:
                self.emit("agent_error", error=f"{type(exc).__name__}: {exc}")
                break
            if result.tool_calls:
                assistant_tool_calls = []
                for call in result.tool_calls:
                    name = call.get("name") or ""
                    raw_args = call.get("arguments") or "{}"
                    assistant_tool_calls.append(
                        {
                            "id": call.get("id") or f"call_{turn}_{len(assistant_tool_calls)}",
                            "type": "function",
                            "function": {"name": name, "arguments": raw_args},
                        }
                    )
                # OpenAI 协议：先追加 assistant(tool_calls)，再追加 tool 结果。
                messages.append(
                    {
                        "role": "assistant",
                        "content": result.text or "",
                        "tool_calls": assistant_tool_calls,
                    }
                )
                for call, assistant_call in zip(result.tool_calls, assistant_tool_calls):
                    name = call.get("name") or ""
                    raw_args = call.get("arguments") or "{}"
                    call_id = assistant_call["id"]
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except Exception:
                        args = {}
                    self.emit(
                        "agent_tool_call",
                        turn=turn,
                        tool=name,
                        args=args,
                        call_id=call_id,
                    )
                    if not self.tools.has(name):
                        content = json.dumps(
                            {"error": f"未知工具 {name}", "available": sorted(self.tools._tools)},
                            ensure_ascii=False,
                        )
                        self.emit("agent_tool_result", tool=name, ok=False, summary=content)
                        tool_history.append(
                            {"tool": name, "args": args, "ok": False, "error": "未知工具"}
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": call_id, "content": content}
                        )
                        continue
                    tool = self.tools.get(name)
                    if tool.risk >= 2:
                        approval = self.gate.create(
                            None,
                            action=f"Agent 请求：{name}",
                            detail=json.dumps(args, ensure_ascii=False)[:2000],
                            amount=_to_float(args.get("amount")),
                            risk=tool.risk,
                        )
                        self.emit("approval_required", **approval.public())
                        approved = self.gate.wait(approval, self.cancel_event)
                        self.emit(
                            "approval_result",
                            approval_id=approval.id,
                            approved=approved,
                            status=approval.status,
                            note=approval.note,
                        )
                        if not approved:
                            self.approval_denied = True
                            content = json.dumps(
                                {"error": "人工未批准该动作", "status": approval.status},
                                ensure_ascii=False,
                            )
                            messages.append(
                                {"role": "tool", "tool_call_id": call_id, "content": content}
                            )
                            tool_history.append(
                                {"tool": name, "args": args, "ok": False, "error": "人工未批准"}
                            )
                            continue
                    self.metrics["tool_calls"] += 1
                    started = time.perf_counter()
                    try:
                        output = self.tools.call(name, args)
                        ok = True
                        content = json.dumps(output, ensure_ascii=False, default=str)[:12000]
                        tool_history.append(
                            {"tool": name, "args": args, "ok": True, "output": output}
                        )
                    except Exception as exc:
                        ok = False
                        errors += 1
                        output = None
                        content = json.dumps(
                            {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                        )
                        tool_history.append(
                            {"tool": name, "args": args, "ok": False, "error": str(exc)}
                        )
                    latency = int((time.perf_counter() - started) * 1000)
                    self.emit(
                        "agent_tool_result",
                        tool=name,
                        ok=ok,
                        latency_ms=latency,
                        summary=content[:900],
                    )
                    if name == "search_flights" and ok and isinstance(output, dict):
                        best = output.get("best") or output.get("cheapest")
                        if isinstance(best, dict):
                            check = policy.compliance(
                                outbound=best
                                if output.get("origin") == request.origin
                                else None,
                                inbound=best
                                if output.get("origin") != request.origin
                                else None,
                            )
                            for item in (check.get("warnings") or []) + (
                                check.get("violations") or []
                            ):
                                self.add_warning(item["message"], source="agent")
                    if name == "search_hotels" and ok and isinstance(output, dict):
                        best = output.get("best") or output.get("cheapest")
                        if isinstance(best, dict):
                            check = policy.compliance(
                                hotel=best,
                                city=output.get("city", ""),
                                nights=output.get("nights", 1),
                            )
                            for item in (check.get("warnings") or []) + (
                                check.get("violations") or []
                            ):
                                self.add_warning(item["message"], source="agent")
                    if name == "compute_trip_cost" and ok and isinstance(output, dict):
                        # Agent 模式没有 DAG 的确定性预算闸门，这里补一道守卫：
                        # 超预算必须显式进入 warnings（状态降为 ok_with_warnings），不得静默。
                        total = output.get("total")
                        if (
                            isinstance(total, (int, float))
                            and request.budget
                            and float(total) > float(request.budget)
                        ):
                            self.add_warning(
                                f"Agent 方案总费用 ¥{float(total):,.0f} 超过预算 ¥{float(request.budget):,.0f}，"
                                "方案未经预算闸门放行，请人工确认后再预订",
                                source="agent",
                            )
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": content}
                    )
                continue
            final_text = (result.text or "").strip()
            if final_text:
                break
        if not final_text:
            final_text = (
                "## 结论\n\n- Agent 未在限定轮次内完成，以下为已验证的工具结果，"
                "请人工确认后再预订。\n"
            )
        self._remember_agent_history(tool_history)
        self.plan = self._agent_plan_from_results(request)
        agent_results = self._agent_results(tool_history)
        # 如果 Agent 忘了生成行程草案，用已验证的工具结果补一份，保证报告可交付。
        if not isinstance(agent_results.get("draft_itinerary"), dict):
            draft = self._build_agent_draft(request, agent_results)
            if isinstance(draft, dict):
                draft_step = Step(
                    id=f"a{len(self.plan.steps) + 1}",
                    goal="补充生成行程草案",
                    tool="draft_itinerary",
                    args={},
                    depends_on=[self.plan.steps[-1].id] if self.plan.steps else [],
                    success="包含交通/住宿/费用",
                    risk=0,
                )
                self.plan.steps.append(draft_step)
                self.results[draft_step.id] = draft
                self.statuses[draft_step.id] = {
                    "status": "ok",
                    "reason": "由引擎补齐",
                    "tool": "draft_itinerary",
                    "goal": draft_step.goal,
                }
        if "request_approval" not in {row["tool"] for row in tool_history}:
            self.add_warning("Agent 未执行人工审批步骤，方案尚未获得预订授权", source="agent")
        facts = self._collect_facts(request)
        facts["agent_turns"] = self.metrics["agent_turns"]
        facts["agent_tools"] = [
            {"tool": row["tool"], "ok": row["ok"], "args": row.get("args", {})}
            for row in tool_history
        ]
        self.facts = facts
        verifier = {
            "ok": not self.warnings,
            "score": max(0, 100 - 8 * len(self.warnings)),
            "issues": self.warnings[:6],
            "strengths": ["工具调用轨迹完整", "原生 Function Calling"],
        }
        self.verifier = verifier
        self.emit("agent_final", markdown=final_text, turns=self.metrics["agent_turns"])
        self._end_stage(
            "agent",
            "ok" if not errors else "partial",
            f"{self.metrics['agent_turns']} 轮 · {len(tool_history)} 次工具调用",
        )
        return final_text, facts, verifier

    def _build_agent_draft(
        self, request: TravelRequest, agent_results: Dict[str, Any]
    ) -> Optional[dict]:
        flights = []
        for row in self._agent_history:
            if row.get("tool") == "search_flights" and row.get("ok"):
                flights.append(row.get("output"))
        outbound = inbound = None
        for result in flights:
            if not isinstance(result, dict):
                continue
            best = result.get("best") or result.get("cheapest")
            if result.get("origin") == request.origin:
                outbound = best
            elif result.get("origin") == request.destination:
                inbound = best
        hotel_result = agent_results.get("search_hotels") or {}
        hotel = hotel_result.get("best") or hotel_result.get("cheapest")
        cost = agent_results.get("compute_trip_cost")
        if not isinstance(cost, dict) and outbound and inbound and hotel:
            cost = compute_trip_cost(
                outbound_price=float(outbound.get("price", 0)),
                return_price=float(inbound.get("price", 0)),
                hotel_price=float(hotel.get("price", 0)),
                nights=request.nights,
                budget=request.budget,
            )
        try:
            return self.tools.call(
                "draft_itinerary",
                {
                    "goal": self.goal,
                    "outbound": outbound,
                    "inbound": inbound,
                    "hotel": hotel,
                    "policy": agent_results.get("check_policy"),
                    "cost": cost,
                    "budget": request.budget,
                    "nights": request.nights,
                },
            )
        except Exception as exc:
            self.emit("agent_error", error=f"补齐行程草案失败：{exc}")
            return None

    def _agent_results(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for row in history:
            if row.get("ok") and isinstance(row.get("output"), dict):
                results[row["tool"]] = row["output"]  # 保留每个工具最近一次结果
        return results

    def _agent_plan_from_results(self, request: TravelRequest) -> Plan:
        """把 Agent 模式的工具轨迹映射成可展示的计划视图。"""

        steps: List[Step] = []
        succeeded = {row["tool"] for row in self._agent_history if row.get("ok")}
        can_recover = {"search_flights", "search_hotels", "compute_trip_cost", "draft_itinerary"} <= succeeded
        for row in self._agent_history:
            tool = row["tool"]
            step_id = f"a{len(steps) + 1}"
            steps.append(
                Step(
                    id=step_id,
                    goal=f"Agent · {tool}",
                    tool=tool,
                    args=row.get("args") or {},
                    depends_on=[steps[-1].id] if steps else [],
                    success="ok" if row.get("ok") else "error",
                    risk=self.tools.get(tool).risk if self.tools.has(tool) else 0,
                )
            )
            if row.get("ok"):
                status = "ok"
                self.results[step_id] = row.get("output")
            elif can_recover:
                status = "degraded"
            else:
                status = "failed"
            self.statuses[step_id] = {
                "status": status,
                "reason": "Agent 调用" if row.get("ok") else row.get("error", "调用失败"),
                "tool": tool,
                "goal": f"Agent · {tool}",
            }
        if not steps:
            steps = [
                Step(
                    "a1",
                    "Agent 直接回答（未调用工具）",
                    "request_approval",
                    {"action": "无工具调用"},
                    [],
                    "",
                    0,
                )
            ]
        return Plan(goal=self.goal, steps=steps, strategy="agent", created_by="agent")

    # -- results ---------------------------------------------------------
    def _collect_facts(self, request: TravelRequest) -> Dict[str, Any]:
        facts: Dict[str, Any] = {}
        draft = self._draft_result()
        if isinstance(draft, dict) and isinstance(draft.get("facts"), dict):
            facts.update(draft["facts"])
        # 补充/覆盖来自工具结果的权威数字。
        cost = self._result_by_tool("compute_trip_cost")
        if isinstance(cost, dict):
            facts.setdefault("total", cost.get("total"))
            facts["breakdown"] = cost.get("breakdown")
            facts.setdefault("budget", cost.get("budget") or request.budget)
        flights = self._result_by_tool("search_flights", all_results=True)
        if isinstance(flights, list):
            for result in flights:
                if not isinstance(result, dict):
                    continue
                best = result.get("best") or result.get("cheapest")
                if not isinstance(best, dict):
                    continue
                if result.get("origin") == request.origin:
                    facts.setdefault("outbound_flight", best.get("flight_no"))
                    facts.setdefault("outbound_price", best.get("price"))
                    facts.setdefault("outbound_depart", best.get("depart"))
                elif result.get("origin") == request.destination:
                    facts.setdefault("inbound_flight", best.get("flight_no"))
                    facts.setdefault("inbound_price", best.get("price"))
                    facts.setdefault("inbound_depart", best.get("depart"))
        hotel = self._result_by_tool("search_hotels")
        if isinstance(hotel, dict):
            best = hotel.get("best") or hotel.get("cheapest")
            if isinstance(best, dict):
                facts.setdefault("hotel", best.get("name"))
                facts.setdefault("hotel_price", best.get("price"))
                facts.setdefault("hotel_distance_km", best.get("distance_km"))
        facts.setdefault("origin", request.origin)
        facts.setdefault("destination", request.destination)
        facts.setdefault("depart", request.depart)
        facts.setdefault("return_date", request.return_date)
        facts.setdefault("nights", request.nights)
        facts.setdefault("budget", request.budget)
        facts.setdefault("purpose", request.purpose)
        facts.setdefault("preferences", request.preferences)
        compliance = self._compliance_result()
        if isinstance(compliance, dict):
            facts["compliance_score"] = compliance.get("score")
            facts["compliance_ok"] = compliance.get("ok")
            facts["violations"] = [i.get("message") for i in compliance.get("violations") or []]
            facts["policy_warnings"] = [i.get("message") for i in compliance.get("warnings") or []]
        facts["warnings"] = list(self.warnings)
        facts["replans"] = self.metrics.get("replans", 0)
        try:
            facts["approvals"] = self.gate.snapshot()
        except Exception:
            facts["approvals"] = []
        try:
            facts["status"] = self._final_status()
        except Exception:
            facts["status"] = ""
        return facts

    def _compliance_result(self) -> Optional[dict]:
        draft = self._draft_result()
        if isinstance(draft, dict) and isinstance(draft.get("compliance"), dict):
            return draft["compliance"]
        cost = self._result_by_tool("compute_trip_cost")
        flights = self._result_by_tool("search_flights", all_results=True) or []
        hotel = self._result_by_tool("search_hotels")
        if not isinstance(cost, dict):
            return None
        outbound = inbound = None
        if isinstance(flights, list):
            for result in flights:
                if not isinstance(result, dict):
                    continue
                best = result.get("best") or result.get("cheapest")
                if result.get("origin") == (self.request.origin if self.request else ""):
                    outbound = best
                else:
                    inbound = best
        best_hotel = (hotel or {}).get("best") if isinstance(hotel, dict) else None
        return policy.compliance(
            outbound=outbound,
            inbound=inbound,
            hotel=best_hotel,
            total_cost=cost.get("total"),
            budget=cost.get("budget"),
            city=(self.request.destination if self.request else ""),
            nights=(self.request.nights if self.request else 1),
        )

    def _draft_result(self) -> Optional[dict]:
        if self.plan is None:
            return None
        for step in reversed(self.plan.steps):
            if step.tool == "draft_itinerary":
                value = self.results.get(step.id)
                if isinstance(value, dict):
                    return value
        for value in self.results.values():
            if isinstance(value, dict) and "markdown" in value and "facts" in value:
                return value
        return None

    def _result_by_tool(self, tool: str, all_results: bool = False) -> Any:
        if self.plan is None:
            return [] if all_results else None
        found = []
        for step in self.plan.steps:
            if step.tool == tool and step.id in self.results:
                found.append(self.results[step.id])
        if all_results:
            return found
        return found[-1] if found else None

    def _step_results(self, step_id: str) -> Dict[str, Any]:
        value = self.results.get(step_id)
        return value if isinstance(value, dict) else {}

    def _set_status(self, step: Step, status: str, reason: str = "", latency_ms: int = 0) -> None:
        with self._lock:
            self.statuses[step.id] = {
                "status": status,
                "reason": str(reason)[:500],
                "tool": step.tool,
                "goal": step.goal,
                "latency_ms": latency_ms,
            }

    def _safe_levels(self) -> List[List[Step]]:
        if not self.plan:
            return []
        try:
            return topological_levels(self.plan.steps)
        except PlanError:
            return [self.plan.steps]

    def _has_failed(self) -> bool:
        return any(state.get("status") == "failed" for state in self.statuses.values())

    def _count_status(self, status: str) -> int:
        return sum(1 for state in self.statuses.values() if state.get("status") == status)

    def _final_status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.approval_denied:
            return "denied"
        if self._has_failed():
            return "failed"
        if any(state.get("status") == "degraded" for state in self.statuses.values()):
            return "degraded"
        if self.warnings:
            return "ok_with_warnings"
        return "ok"

    def _run_summary(self, status: str, facts: Dict[str, Any]) -> str:
        total = facts.get("total")
        total_text = f"¥{float(total):,.0f}" if isinstance(total, (int, float)) else "费用待定"
        return (
            f"{self.request.route if self.request else ''} · {total_text} · "
            f"{self._count_status('ok')}/{len(self.statuses)} 步成功 · 状态 {status}"
        )

    def _approval_detail(self, step: Step, args: Dict[str, Any]) -> str:
        detail = args.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
        if isinstance(detail, dict):
            return json.dumps(detail, ensure_ascii=False)
        draft = self._draft_result()
        if isinstance(draft, dict) and draft.get("summary"):
            return str(draft["summary"])
        return json.dumps(_safe_args(args), ensure_ascii=False)[:3000]

    # -- metrics / payload ----------------------------------------------
    def _metrics_snapshot(self) -> Dict[str, Any]:
        llm_stats = self.llm.stats.snapshot()
        duration = int((time.time() - self.started_at) * 1000)
        statuses = list(self.statuses.values())
        return {
            "duration_ms": duration,
            "steps_total": len(self.plan.steps) if self.plan else 0,
            "steps_ok": sum(1 for s in statuses if s.get("status") == "ok"),
            "steps_degraded": sum(1 for s in statuses if s.get("status") == "degraded"),
            "steps_failed": sum(1 for s in statuses if s.get("status") == "failed"),
            "steps_skipped": sum(1 for s in statuses if s.get("status") == "skipped"),
            "replans": self.metrics.get("replans", 0),
            "retries": self.metrics.get("retries", 0),
            "tool_calls": self.metrics.get("tool_calls", 0),
            "parallel_rounds": self.metrics.get("parallel_rounds", 0),
            "optimizations": self.metrics.get("optimizations", 0),
            "repairs": self.metrics.get("repairs", 0),
            "agent_turns": self.metrics.get("agent_turns", 0),
            "warnings": len(self.warnings),
            "llm": llm_stats,
            "stages": [
                {
                    "name": stage["name"],
                    "label": stage.get("label", stage["name"]),
                    "status": stage.get("status"),
                    "duration_ms": stage.get("duration_ms", 0),
                }
                for stage in self.stages
            ],
        }

    def _result_payload(
        self,
        status: str,
        report: str,
        narrative: str,
        verifier: Dict[str, Any],
        facts: Dict[str, Any],
        duration: int,
        error: str = "",
    ) -> Dict[str, Any]:
        metrics = self._metrics_snapshot()
        metrics["duration_ms"] = duration
        plan_dict = self.plan.to_dict() if self.plan else {}
        payload = {
            "run_id": self.run_id,
            "goal": self.goal,
            "mode": self.options.mode,
            "status": status,
            "error": error,
            "mock": self.cfg.mock,
            "model": self.cfg.model,
            "fast_model": self.cfg.fast_model,
            "version": VERSION,
            "started_at": self.started_iso,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_ms": duration,
            "request": self.request.to_dict() if self.request else {},
            "questions": self.questions,
            "plan": plan_dict,
            "initial_plan": self.initial_plan.to_dict() if self.initial_plan else plan_dict,
            "statuses": self.statuses,
            "warnings": self.warnings,
            "review_notes": self.review_notes,
            "critique": self.critique,
            "verifier": verifier or self.verifier,
            "facts": facts or self.facts,
            "narrative": narrative or self.narrative,
            "metrics": metrics,
            "artifacts": {
                "plan": "plan.json",
                "trace": "trace.jsonl",
                "report": "report.md",
                "result": "result.json",
            },
            "step_results": {
                sid: summarize_value(value, 1500)
                for sid, value in list(self.results.items())[:16]
            },
            "memory_updates": self.memory_updates,
            "trace_count": self.bus.last_seq,
            "report": report,
        }
        return payload

    def _write_artifacts(self, report: str) -> None:
        assert self.run_dir is not None
        if self.plan is not None:
            (self.run_dir / "plan.json").write_text(
                json.dumps(self.plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (self.run_dir / "report.md").write_text(report, encoding="utf-8")

    def _write_result(self, payload: Dict[str, Any]) -> None:
        assert self.run_dir is not None
        (self.run_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        self._flush_trace()

    def _flush_trace(self) -> None:
        if self.run_dir is None:
            return
        (self.run_dir / "trace.jsonl").write_text(
            "\n".join(
                json.dumps(event, ensure_ascii=False, default=str)
                for event in self.bus.snapshot()
            )
            + "\n",
            encoding="utf-8",
        )

    def _memory_stage(
        self, request: TravelRequest, facts: Dict[str, Any], status: str
    ) -> None:
        if self.memory is None:
            self._begin_stage("memory", "记忆沉淀")
            self._end_stage("memory", "skipped", "本次运行未启用长期记忆")
            return
        self._begin_stage("memory", "记忆沉淀")
        total = facts.get("total")
        episode = {
            "run_id": self.run_id,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "goal": self.goal,
            "origin": request.origin,
            "destination": request.destination,
            "depart": request.depart,
            "return_date": request.return_date,
            "budget": request.budget,
            "total": total,
            "status": status,
            "mode": self.options.mode,
            "warnings": self.warnings[:5],
            "replans": self.metrics.get("replans", 0),
            "duration_ms": int((time.time() - self.started_at) * 1000),
            "outbound_flight": facts.get("outbound_flight"),
            "inbound_flight": facts.get("inbound_flight"),
            "hotel": facts.get("hotel"),
            "compliance_score": facts.get("compliance_score"),
        }
        self.memory.record_episode(episode)
        saved: List[dict] = []
        # 规则抽取（无需模型）
        for item in self.memory.infer_from_goal(self.goal):
            if self.memory.set_preference(
                item["key"], item["value"], evidence=item.get("evidence", ""), source="rule"
            ):
                saved.append({"key": item["key"], "value": item["value"], "source": "rule"})
        # 模型抽取（失败不影响主流程）
        agent = MemoryAgent(self.llm, self.cfg)
        items, meta = agent.extract(
            goal=self.goal, request=request, facts=facts, warnings=self.warnings
        )
        for item in items:
            try:
                confidence = float(item.get("confidence", 0))
            except Exception:
                confidence = 0
            if confidence < 0.7:
                continue
            key = str(item.get("key", "")).strip()
            value = str(item.get("value", "")).strip()
            if self.memory.set_preference(
                key,
                value,
                evidence=str(item.get("evidence", "")),
                source="llm",
                confidence=confidence,
            ):
                saved.append(
                    {"key": key, "value": value, "source": "llm", "confidence": confidence}
                )
        self.memory_updates = saved
        context = self.memory.context_text()
        self._end_stage("memory", "ok", f"保存 {len(saved)} 条偏好")
        self.emit(
            "memory_update",
            saved=saved,
            meta=meta.to_dict(),
            context=context,
            profile=self.memory.preferences(),
        )

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- compatibility ---------------------------------------------------
    @property
    def _agent_history(self) -> List[Dict[str, Any]]:
        # 由 _run_agent_mode 写入；用 getattr 避免 dataclass 之外的状态污染。
        return getattr(self, "_agent_history_value", [])

    def _remember_agent_history(self, history: List[Dict[str, Any]]) -> None:
        self._agent_history_value = history


def failure_reason(outcome: StepOutcome) -> str:
    return f"{outcome.step_id} 失败：{outcome.reason}"


def _hour(hhmm: str) -> int:
    try:
        return int(str(hhmm).split(":")[0])
    except Exception:
        return 12


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_args(args: Dict[str, Any]) -> Dict[str, Any]:
    safe = {}
    for key, value in (args or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value[:500] if isinstance(value, str) else value
        elif isinstance(value, dict):
            safe[key] = {k: (str(v)[:200] if not isinstance(v, (int, float, bool)) else v) for k, v in list(value.items())[:12]}
        else:
            safe[key] = str(value)[:300]
    return safe


def _agent_system_prompt(tools: ToolRegistry, request: TravelRequest, memory: str) -> str:
    from core.skills import skill_context

    return f"""你是「织序 WeaveOrder」的自主差旅 Agent。你可以通过 function calling 调用工具完成任务。
# 工作方式
- 先查政策，再搜索去程与返程航班，再搜酒店，用计算工具算总费用，生成行程草案，最后请求人工审批。
- 所有价格、航班号、酒店名必须来自工具返回，禁止编造。
- 预算 {request.budget} 元；路线 {request.origin} → {request.destination}；
  日期 {request.depart} 至 {request.return_date}；偏好：{', '.join(request.preferences) or '无'}。
- 长期记忆：{memory or '（无）'}
# 技能
{skill_context(max_chars=800) or '（无已安装技能）'}
- 如果工具返回空结果或超预算，调整参数重试，并在结论里说明取舍。
- 预订类动作必须调用 request_approval，不得直接声称已下单。
# 工具
{tools.describe_compact()}
# 最终答复
用 Markdown，包含：## 结论、## 关键决策、## 风险与建议。不要输出工具 JSON。"""


__all__ = ["RunOptions", "Orchestrator", "ApprovalGate", "ApprovalRequest", "StepOutcome"]

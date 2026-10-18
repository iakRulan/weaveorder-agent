"""多智能体角色：Intake / Planner / Critic / Verifier / Replanner / Narrator / Memory。

每个角色只做一件事，输入输出都是结构化数据，便于单独评测与替换模型。
真正的「协同」发生在 Orchestrator：规划 → 审查 → 并行执行 → 校验 →
失败重规划 → 叙事总结 → 记忆沉淀。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import catalog, policy
from .config import Config
from .intake import TravelRequest, _default_questions, llm_intake, rule_intake
from .llm import LLM, LLMError, parse_json_text
from .memory import MemoryStore
from .plan import (
    Plan,
    PlanError,
    Step,
    ensure_approval_step,
    merge_plan,
    plan_from_dict,
    summarize_value,
    validate_plan,
)

TRAVEL_TOOLS = [
    "check_policy",
    "search_flights",
    "search_hotels",
    "compute_trip_cost",
    "draft_itinerary",
    "policy_compliance",
    "request_approval",
]


@dataclass
class AgentMeta:
    agent: str
    model: str = ""
    latency_ms: int = 0
    fallback: bool = False
    note: str = ""
    raw: str = ""
    tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "fallback": self.fallback,
            "note": self.note,
            "tokens": self.tokens,
        }


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


class IntakeAgent:
    def __init__(self, llm: LLM, cfg: Config) -> None:
        self.llm = llm
        self.cfg = cfg

    def run(self, goal: str) -> Tuple[TravelRequest, AgentMeta]:
        started = time.perf_counter()
        request = rule_intake(goal)
        meta = AgentMeta(agent="intake", model="rule", latency_ms=0)
        if self.llm.available:
            refined = llm_intake(self.llm, goal, request)
            request = refined
            if request.source == "llm+rule":
                meta.model = self.cfg.model_for("intake")
                meta.fallback = False
            else:
                meta.fallback = True
                meta.note = "模型解析失败，使用规则解析"
        else:
            meta.fallback = True
            meta.note = "mock 模式：规则解析"
        if not request.questions:
            request.questions = _default_questions(request)
        meta.latency_ms = int((time.perf_counter() - started) * 1000)
        return request, meta


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """你是「织序 WeaveOrder」的任务规划器，把企业差旅目标拆成可执行的任务 DAG。
你只输出一个 JSON 对象，顶层必须有 steps 数组，不要输出解释。

# 可用工具
{tools}

# 引用规则
- 引用前序步骤结果必须写成 $s2.best、$s2.best.price、$s5.total、$s6.summary 这种形式。
- 禁止写 $2.price、$s2.outbound.price 或自造字段；只能引用工具真实返回的字段。
- 只依赖真正需要的前序步骤；互不依赖的步骤可以并行。

# 步骤对象（字段名必须完全一致）
{{"id":"s1","goal":"检索差旅政策","tool":"check_policy","args":{{"query":"机票 酒店 预算 审批"}},"depends_on":[],"success":"返回至少 1 条政策","risk":0}}

# 硬性规则
1. 必须且只规划 7 步：政策检索、去程航班、返程航班、酒店、费用合计、行程草案、人工审批。
2. id 依次为 s1、s2、s3、s4、s5、s6、s7；tool 必须来自上面的工具列表。
3. 严格使用用户已解析的出发城市、目的地、日期与预算；不要改成机场三字码，不要改日期。
4. 费用必须用 compute_trip_cost 计算，禁止心算；参数引用去程/返程/酒店的 price。
5. 行程草案用 draft_itinerary，policy 引用 $s1，cost 引用 $s5，并传入 budget 与 nights。
6. 审批步骤 tool=request_approval，detail 引用 $s6.summary，amount 引用 $s5.total，risk=2。
7. 顶层字段只允许 goal、assumptions、steps；禁止输出 dag、action、params、step、output_ref 等字段。
8. max_price 只在用户明确给出价格上限时填写；不要凭空限制。
"""

PLANNER_USER = """用户目标：{goal}
已解析约束：{request}
长期记忆：{memory}
数据覆盖城市：{cities}
{extra}
请输出 steps JSON。"""


def _adapt_plan_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """兼容模型可能使用的 dag/action/params 变体，尽量把合法信息救回来。"""

    raw_steps = data.get("steps") or data.get("dag") or data.get("plan") or data.get("tasks")
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id", item.get("step_id", item.get("step", index + 1)))
        if isinstance(raw_id, int) or str(raw_id).isdigit():
            step_id = f"s{raw_id}"
        else:
            step_id = str(raw_id).strip()
        tool = item.get("tool") or item.get("action") or item.get("name") or ""
        args = item.get("args") or item.get("params") or item.get("input") or {}
        if not isinstance(args, dict):
            args = {}
        deps = item.get("depends_on") or item.get("deps") or item.get("after") or []
        if not isinstance(deps, list):
            deps = [deps]
        deps = [f"s{d}" if str(d).isdigit() else str(d) for d in deps]
        # 从 args 的 $sN 引用中自动补依赖。
        for ref in _ref_roots(args):
            if ref not in deps and ref != step_id:
                deps.append(ref)
        steps.append(
            {
                "id": step_id,
                "goal": item.get("goal") or item.get("description") or str(tool),
                "tool": tool,
                "args": args,
                "depends_on": deps,
                "success": item.get("success") or item.get("expected") or "",
                "risk": item.get("risk", 0),
                "optional": bool(item.get("optional", False)),
            }
        )
    return {
        "goal": data.get("goal") or "",
        "assumptions": data.get("assumptions") or [],
        "steps": steps,
    }


def _ref_roots(value: Any) -> List[str]:
    import re

    roots: List[str] = []
    if isinstance(value, str):
        for match in re.finditer(r"\$([A-Za-z][A-Za-z0-9_]*)", value):
            if match.group(1) not in roots:
                roots.append(match.group(1))
    elif isinstance(value, dict):
        for item in value.values():
            roots.extend(_ref_roots(item))
    elif isinstance(value, list):
        for item in value:
            roots.extend(_ref_roots(item))
    return roots


def rule_plan(goal: str, request: TravelRequest, inject_failure: bool = False) -> Plan:
    outbound_args: Dict[str, Any] = {
        "origin": request.origin,
        "destination": request.destination,
        "date": request.depart,
    }
    if "直飞" in request.preferences:
        outbound_args["direct_only"] = True
    if inject_failure:
        # 演示：压到不可能成交的价格，触发「空结果 → 重规划」。
        outbound_args["max_price"] = 500
    steps = [
        Step(
            "s1",
            "检索差旅政策",
            "check_policy",
            {"query": "机票 酒店 预算 审批 红眼"},
            [],
            "返回至少 1 条政策条款",
            0,
        ),
        Step(
            "s2",
            "搜索去程航班",
            "search_flights",
            outbound_args,
            [],
            "返回至少 1 个去程航班选项",
            0,
        ),
        Step(
            "s3",
            "搜索返程航班",
            "search_flights",
            {
                "origin": request.destination,
                "destination": request.origin,
                "date": request.return_date,
            },
            [],
            "返回至少 1 个返程航班选项",
            0,
        ),
        Step(
            "s4",
            "搜索酒店",
            "search_hotels",
            {
                "city": request.destination,
                "checkin": request.depart,
                "checkout": request.return_date,
            },
            [],
            "返回至少 1 个酒店选项",
            0,
        ),
        Step(
            "s5",
            "计算往返总费用",
            "compute_trip_cost",
            {
                "outbound_price": "$s2.best.price",
                "return_price": "$s3.best.price",
                "hotel_price": "$s4.best.price",
                "nights": request.nights,
                "budget": request.budget,
            },
            ["s2", "s3", "s4"],
            f"总费用不超过 {request.budget} 元",
            0,
        ),
        Step(
            "s6",
            "生成行程草案",
            "draft_itinerary",
            {
                "goal": goal,
                "outbound": "$s2.best",
                "inbound": "$s3.best",
                "hotel": "$s4.best",
                "policy": "$s1",
                "cost": "$s5",
                "budget": request.budget,
                "nights": request.nights,
            },
            ["s1", "s2", "s3", "s4", "s5"],
            "包含交通、住宿、费用、政策依据与合规检查",
            0,
        ),
        Step(
            "s7",
            "提交预订审批",
            "request_approval",
            {
                "action": "预订往返航班与酒店",
                "detail": "$s6.summary",
                "amount": "$s5.total",
            },
            ["s6"],
            "获得人工确认",
            2,
        ),
    ]
    assumptions = [
        f"{request.origin} → {request.destination}",
        f"{request.depart} 至 {request.return_date}（{request.nights} 晚）",
        f"预算 {request.budget} 元",
    ] + list(request.preferences)
    return Plan(
        goal=goal,
        steps=steps,
        assumptions=assumptions,
        strategy="dag",
        created_by="rule",
    )


class PlannerAgent:
    def __init__(self, llm: LLM, cfg: Config, tools: Any) -> None:
        self.llm = llm
        self.cfg = cfg
        self.tools = tools

    def plan(
        self,
        goal: str,
        request: TravelRequest,
        memory_context: str = "",
        inject_failure: bool = False,
    ) -> Tuple[Plan, AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="planner", model=self.cfg.model_for("planner"))
        if not self.llm.available:
            plan = rule_plan(goal, request, inject_failure=inject_failure)
            meta.fallback = True
            meta.model = "rule"
            meta.note = "mock 模式：规则规划"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return plan, meta

        extra = ""
        if inject_failure:
            extra = "演示约束：去程航班搜索把 max_price 设为 500，用于验证失败后的恢复能力。"
        messages = [
            {
                "role": "system",
                "content": PLANNER_SYSTEM.format(
                    tools=self.tools.describe_compact(TRAVEL_TOOLS)
                ),
            },
            {
                "role": "user",
                "content": PLANNER_USER.format(
                    goal=goal,
                    request=json.dumps(request.to_dict(), ensure_ascii=False),
                    memory=memory_context or "（无历史记忆）",
                    cities="、".join(catalog.route_cities()),
                    extra=extra,
                ),
            },
        ]
        try:
            raw = self.llm.chat(
                messages,
                role="planner",
                temperature=0.1,
                max_tokens=2200,
                json_mode=True,
                thinking=False,
            )
            meta.raw = raw.text[:12000]
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            data = _adapt_plan_payload(parse_json_text(raw.text))
            plan = plan_from_dict(data, strategy="dag", created_by="llm")
            errors = validate_plan(plan, self.tools, max_steps=self.cfg.max_steps)
            if errors:
                plan = self._repair(plan, errors, meta)
            if plan is None:
                raise LLMError("计划校验失败")
            if inject_failure:
                for step in plan.steps:
                    if step.tool == "search_flights":
                        step.args["max_price"] = 500
                        break
            plan = ensure_approval_step(plan)
            if not plan.assumptions:
                plan.assumptions = [
                    f"{request.origin} → {request.destination}",
                    f"{request.depart} 至 {request.return_date}（{request.nights} 晚）",
                    f"预算 {request.budget} 元",
                ]
            return plan, meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"LLM 规划失败：{type(exc).__name__}: {exc}"[:300]
            plan = rule_plan(goal, request, inject_failure=inject_failure)
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return plan, meta

    def _repair(self, plan: Plan, errors: List[str], meta: AgentMeta) -> Optional[Plan]:
        try:
            raw = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是 JSON 计划修复器。根据校验错误修正计划，只输出严格 JSON。"
                            "顶层字段只允许 goal、assumptions、steps；每个 step 必须有 "
                            "id/tool/args/depends_on/success/risk。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"校验错误：{json.dumps(errors, ensure_ascii=False)}\n"
                            f"原计划：{json.dumps(plan.to_dict(), ensure_ascii=False)[:9000]}\n"
                            "请输出修复后的完整计划 JSON。"
                        ),
                    },
                ],
                role="planner:repair",
                temperature=0,
                max_tokens=2200,
                json_mode=True,
                thinking=False,
                retries=0,
            )
            data = _adapt_plan_payload(parse_json_text(raw.text))
            repaired = plan_from_dict(data, strategy="dag", created_by="llm-repair")
            remaining = validate_plan(repaired, self.tools, max_steps=self.cfg.max_steps)
            if remaining:
                return None
            meta.note = "计划经一次结构修复"
            return repaired
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Critic
# ---------------------------------------------------------------------------

CRITIC_SYSTEM = """你是「织序 WeaveOrder」的计划审查员。审查 Planner 给出的差旅任务 DAG，只输出 JSON：
{"verdict":"accept"或"revise","issues":[{"step":"s1","severity":"high|medium|low","message":"..."}],
 "suggestions":["..."],"risk_notes":["..."]}
审查重点：
1. 是否覆盖：政策、去程、返程、酒店、费用、行程草案、人工审批；
2. 引用是否合规（$sN 存在、字段是工具真实返回的）；
3. 费用是否用工具计算、审批是否 risk=2；
4. 是否有明显浪费、重复或与用户约束冲突的步骤。
注意：规划阶段不应假设未执行步骤的结果；缺少客户具体区域最多标 medium，不要因此要求新增步骤。
只有“必须修改才能执行”的问题才用 verdict=revise，例如引用不存在、往返方向写反、缺少关键步骤、
risk 等级错误；信息缺失、客户区域未知、无法用工具参数表达的问题请放入 risk_notes，不要因此 revise。
没有实质问题时 verdict=accept，issues 为空。不要输出计划本身，不要输出解释。"""


class CriticAgent:
    def __init__(self, llm: LLM, cfg: Config, tools: Any) -> None:
        self.llm = llm
        self.cfg = cfg
        self.tools = tools

    def review(
        self, goal: str, request: TravelRequest, plan: Plan
    ) -> Tuple[Dict[str, Any], AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="critic", model=self.cfg.model_for("critic"))
        if not self.llm.available:
            result = self.local_review(plan)
            meta.fallback = True
            meta.model = "rule"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return result, meta
        try:
            raw = self.llm.chat(
                [
                    {"role": "system", "content": CRITIC_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"用户目标：{goal}\n"
                            f"约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                            f"政策阈值：{json.dumps(policy.RULES, ensure_ascii=False)}\n"
                            f"计划：{json.dumps(plan.to_dict(), ensure_ascii=False)[:10000]}"
                        ),
                    },
                ],
                role="critic",
                temperature=0,
                max_tokens=900,
                json_mode=True,
                thinking=False,
            )
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            data = parse_json_text(raw.text)
            issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
            verdict = str(data.get("verdict", "")).lower()
            if verdict not in ("accept", "revise"):
                verdict = "revise" if any(
                    str(i.get("severity", "")).lower() == "high" for i in issues
                ) else "accept"
            return {
                "verdict": verdict,
                "issues": issues[:8],
                "suggestions": [str(s) for s in (data.get("suggestions") or [])][:5],
                "risk_notes": [str(s) for s in (data.get("risk_notes") or [])][:5],
            }, meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"Critic 失败，使用规则审查：{type(exc).__name__}"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return self.local_review(plan), meta

    @staticmethod
    def local_review(plan: Plan) -> Dict[str, Any]:
        tools_used = {step.tool for step in plan.steps}
        required = {
            "check_policy": "缺少政策检索",
            "search_flights": "缺少航班搜索",
            "search_hotels": "缺少酒店搜索",
            "compute_trip_cost": "缺少费用计算",
            "draft_itinerary": "缺少行程草案",
            "request_approval": "缺少人工审批",
        }
        issues: List[Dict[str, str]] = []
        for tool, message in required.items():
            if tool not in tools_used:
                issues.append({"step": "-", "severity": "high", "message": message})
        flight_steps = [s for s in plan.steps if s.tool == "search_flights"]
        if len(flight_steps) < 2:
            issues.append(
                {"step": "-", "severity": "medium", "message": "往返只规划了一段航班"}
            )
        ids = {step.id for step in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in ids:
                    issues.append(
                        {"step": step.id, "severity": "high", "message": f"依赖不存在的 {dep}"}
                    )
        return {
            "verdict": "revise" if any(i["severity"] == "high" for i in issues) else "accept",
            "issues": issues,
            "suggestions": [],
            "risk_notes": [],
        }


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

VERIFIER_SYSTEM = """你是「织序 WeaveOrder」的结果验收员。根据用户目标与约束，检查最终行程是否可交付。
只输出 JSON：{"ok":true/false,"score":0-100,"issues":["..."],"strengths":["..."]}
检查点：日期与路线是否一致、往返是否齐全、酒店是否覆盖住宿、费用是否含往返与住宿、
是否明确标注超预算/政策冲突、是否有待确认事项。
重要：航班号、起降时刻、酒店名称/地址/距离都来自工具返回，是已知事实；不要因为用户没提供
客户具体地址就判定“编造”，除非草案声称了工具未返回的信息。重点检查待确认事项是否覆盖
客户区域、价格时效与政策例外。不要编造未提供的信息。"""


class VerifierAgent:
    def __init__(self, llm: LLM, cfg: Config) -> None:
        self.llm = llm
        self.cfg = cfg

    def review(
        self,
        goal: str,
        request: TravelRequest,
        draft: Dict[str, Any],
        warnings: List[str],
    ) -> Tuple[Dict[str, Any], AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="verifier", model=self.cfg.model_for("verifier"))
        if not self.llm.available or not isinstance(draft, dict):
            meta.fallback = True
            meta.model = "rule"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return {
                "ok": bool(draft),
                "score": 80 if draft else 0,
                "issues": [],
                "strengths": ["确定性校验已通过"],
            }, meta
        try:
            raw = self.llm.chat(
                [
                    {"role": "system", "content": VERIFIER_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"目标：{goal}\n"
                            f"约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                            f"风险提示：{json.dumps(warnings, ensure_ascii=False)}\n"
                            f"行程草案：{str(draft.get('markdown', ''))[:5000]}"
                        ),
                    },
                ],
                role="verifier",
                temperature=0,
                max_tokens=800,
                json_mode=True,
                thinking=False,
            )
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            data = parse_json_text(raw.text)
            return {
                "ok": bool(data.get("ok", True)),
                "score": int(data.get("score", 80) or 80),
                "issues": [str(i) for i in (data.get("issues") or [])][:6],
                "strengths": [str(i) for i in (data.get("strengths") or [])][:6],
            }, meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"Verifier 失败：{type(exc).__name__}"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return {"ok": True, "score": 80, "issues": [], "strengths": []}, meta


# ---------------------------------------------------------------------------
# Replanner
# ---------------------------------------------------------------------------

REPLANNER_SYSTEM = """你是「织序 WeaveOrder」的重规划器。执行中某一步失败或预算超支，你需要在保留已完成步骤的前提下修复计划。
只输出 JSON 对象：{"goal":"...","assumptions":["..."],"revision_reason":"...","steps":[...]}
规则：
1. 已完成且成功的步骤保持原 id、tool、args 不变，放进 steps；
2. 失败步骤可以修改参数、换日期、降低价格上限，或替换为新的步骤；
3. 新增步骤 id 用 sR1、sR2…，tool 必须来自工具列表；
4. 保持原有引用格式（$s2.best.price 等），不要引用不存在的步骤；
5. 必须保留最终的人工审批步骤（request_approval，risk=2）；
6. 禁止用估算数字替代工具结果：compute_trip_cost 的价格必须引用 $sN，不能写 800 之类的常量；
   如果数据源覆盖不到，保留失败并在 revision_reason 中说明，不要伪造一个“可交付”的方案；
7. 总步骤不超过 10；只输出 JSON。"""


class ReplannerAgent:
    def __init__(self, llm: LLM, cfg: Config, tools: Any) -> None:
        self.llm = llm
        self.cfg = cfg
        self.tools = tools

    def replan(
        self,
        *,
        goal: str,
        request: TravelRequest,
        plan: Plan,
        failed_step: str,
        reason: str,
        results: Dict[str, Any],
        completed: Dict[str, str],
        warnings: List[str],
        round_index: int,
    ) -> Tuple[Optional[Plan], AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="replanner", model=self.cfg.model_for("replanner"))
        if not self.llm.available:
            meta.fallback = True
            meta.model = "rule"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return None, meta
        compact_results = {
            step_id: summarize_value(value, 350)
            for step_id, value in results.items()
            if step_id in completed
        }
        try:
            raw = self.llm.chat(
                [
                    {"role": "system", "content": REPLANNER_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"用户目标：{goal}\n"
                            f"约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                            f"失败步骤：{failed_step}\n失败原因：{reason}\n"
                            f"第 {round_index} 轮重规划。已完成：{json.dumps(completed, ensure_ascii=False)}\n"
                            f"已完成结果摘要：{json.dumps(compact_results, ensure_ascii=False)[:5000]}\n"
                            f"现有风险提示：{json.dumps(warnings, ensure_ascii=False)}\n"
                            f"当前计划：{json.dumps(plan.to_dict(), ensure_ascii=False)[:8000]}\n"
                            f"可用工具：\n{self.tools.describe_compact(TRAVEL_TOOLS)}"
                        ),
                    },
                ],
                role="replanner",
                temperature=0.1,
                max_tokens=2200,
                json_mode=True,
                thinking=False,
                retries=1,
            )
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            data = _adapt_plan_payload(parse_json_text(raw.text))
            revised = plan_from_dict(data, strategy="dag", created_by="replanner")
            revised.notes = str(parse_json_text(raw.text).get("revision_reason", ""))[:500]
            # 把已完成步骤补回计划，保证报告与引用完整。
            existing = {step.id for step in revised.steps}
            completed_steps = [step for step in plan.steps if step.id in completed and step.id not in existing]
            if completed_steps:
                revised.steps = completed_steps + revised.steps
            revised = ensure_approval_step(revised)
            errors = validate_plan(revised, self.tools, max_steps=self.cfg.max_steps)
            if errors:
                meta.fallback = True
                meta.note = "重规划结果未通过校验：" + "；".join(errors[:3])
                return None, meta
            return revised, meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"重规划失败：{type(exc).__name__}: {exc}"[:300]
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return None, meta


# ---------------------------------------------------------------------------
# Narrator
# ---------------------------------------------------------------------------

NARRATOR_SYSTEM = """你是「织序 WeaveOrder」的报告撰写员。根据给定事实，为差旅方案写一段可直接放进报告的摘要。
要求：
1. 只使用给定事实中的数字、航班号、酒店名和政策编号，不得编造；
2. 中文，Markdown，严格包含三个二级标题：## 结论、## 关键决策、## 风险与建议；
3. 每节 2–4 条，短句，总字数 220–380 字；
4. 明确说明是否在预算内，以及审批记录（facts.approvals）：自动批准、人工批准、拒绝或尚未审批；
   如果审批记录为空，必须写“尚未完成人工审批”，绝不能写“无需审批”；
5. 不得推断未给出的信息（同行人数、舱位、客户具体地址、天气等）；
6. 不要输出 approval_id、run_id 等内部 ID，也不要把 risk 数字解释成风险等级；
7. 不要输出标题以外的客套话。"""


class NarratorAgent:
    def __init__(self, llm: LLM, cfg: Config) -> None:
        self.llm = llm
        self.cfg = cfg

    def narrate(
        self,
        *,
        goal: str,
        request: TravelRequest,
        facts: Dict[str, Any],
        warnings: List[str],
        metrics: Dict[str, Any],
        on_token: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="narrator", model=self.cfg.model_for("narrator"))
        if not self.llm.available:
            meta.fallback = True
            meta.model = "rule"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return self.template(goal, request, facts, warnings), meta
        try:
            raw = self.llm.chat(
                [
                    {"role": "system", "content": NARRATOR_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"用户目标：{goal}\n"
                            f"约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                            f"事实：{json.dumps(facts, ensure_ascii=False)[:5000]}\n"
                            f"风险：{json.dumps(warnings, ensure_ascii=False)}\n"
                            f"运行指标：{json.dumps(metrics, ensure_ascii=False)[:1200]}"
                        ),
                    },
                ],
                role="narrator",
                temperature=0.3,
                max_tokens=900,
                thinking=False,
                stream_cb=on_token,
            )
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            text = raw.text.strip()
            if "## 结论" not in text:
                text = self.template(goal, request, facts, warnings)
                meta.fallback = True
                meta.note = "模型输出缺少结构化标题，使用模板"
            return text, meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"Narrator 失败：{type(exc).__name__}"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return self.template(goal, request, facts, warnings), meta

    @staticmethod
    def template(
        goal: str,
        request: TravelRequest,
        facts: Dict[str, Any],
        warnings: List[str],
    ) -> str:
        total = facts.get("total")
        budget = facts.get("budget") or request.budget
        status = "在预算内" if total is not None and float(total) <= float(budget) else "超出预算"
        lines = [
            "## 结论",
            f"- 已为「{request.route}」生成 {request.depart} 至 {request.return_date} 的往返差旅方案，覆盖 {request.nights} 晚住宿。",
            f"- 预估总费用 ¥{float(total):,.0f}，预算 ¥{float(budget):,.0f}，{status}。" if total is not None else "- 费用待确认。",
            "",
            "## 关键决策",
            f"- 去程：{(facts.get('outbound_flight') or '待定')}；返程：{(facts.get('inbound_flight') or '待定')}。",
            f"- 酒店：{(facts.get('hotel') or '待定')}；合规评分 {facts.get('compliance_score', '—')}/100。",
            "- 费用由 compute_trip_cost 计算，所有结论可回查到 trace.jsonl。",
        ]
        approvals = facts.get("approvals") or []
        if approvals:
            labels = []
            for item in approvals[:3]:
                status_map = {
                    "approved": "已人工批准",
                    "auto-approved": "演示模式自动批准",
                    "rejected": "已拒绝",
                    "timeout": "审批超时",
                    "pending": "等待审批",
                    "cancelled": "已取消",
                }
                labels.append(
                    f"{item.get('action', '高风险动作')}（{status_map.get(item.get('status'), item.get('status'))}）"
                )
            lines.append(f"- 审批记录：{'；'.join(labels)}。")
        else:
            lines.append("- 审批记录：尚未完成人工审批，预订前必须确认。")
        lines += [
            "",
            "## 风险与建议",
        ]
        if warnings:
            lines.extend(f"- {warning}" for warning in warnings[:4])
        else:
            lines.append("- 未发现政策冲突，仍需在预订前完成人工确认。")
        lines.append("- 建议在出行前 24 小时复核航班动态与酒店取消政策。")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory writer
# ---------------------------------------------------------------------------

MEMORY_SYSTEM = """你是「织序 WeaveOrder」的记忆整理器。从本次差旅中抽取可长期复用的偏好。
只输出 JSON：{"preferences":[{"key":"hotel_brand","value":"亚朵","confidence":0.9,"evidence":"用户原话或选择行为"}],"summary":"一句话"}
允许的 key：home_city, hotel_brand, hotel_price_max, airline_pref, seat_pref, flight_time_pref, budget_habit, avoid_red_eye, arrival_time, trip_purpose, local_transport。
seat_pref 只能填 靠窗/过道/前排/后排；航班时段偏好请用 flight_time_pref（早班机/午班机/晚班机/避免红眼），不要混用。
规则：只抽取用户明确表达或反复选择体现的偏好；不要记录手机号、证件号等敏感信息；
没有可复用偏好时返回空数组；confidence 低于 0.7 的不要输出。"""


class MemoryAgent:
    def __init__(self, llm: LLM, cfg: Config) -> None:
        self.llm = llm
        self.cfg = cfg

    def extract(
        self,
        *,
        goal: str,
        request: TravelRequest,
        facts: Dict[str, Any],
        warnings: List[str],
    ) -> Tuple[List[Dict[str, Any]], AgentMeta]:
        started = time.perf_counter()
        meta = AgentMeta(agent="memory", model=self.cfg.model_for("memory"))
        if not self.llm.available:
            meta.fallback = True
            meta.model = "rule"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return [], meta
        try:
            raw = self.llm.chat(
                [
                    {"role": "system", "content": MEMORY_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"用户目标：{goal}\n"
                            f"约束：{json.dumps(request.to_dict(), ensure_ascii=False)}\n"
                            f"本次选择：{json.dumps(facts, ensure_ascii=False)[:2500]}\n"
                            f"风险：{json.dumps(warnings, ensure_ascii=False)}"
                        ),
                    },
                ],
                role="memory",
                temperature=0,
                max_tokens=600,
                json_mode=True,
                thinking=False,
            )
            meta.latency_ms = raw.latency_ms
            meta.tokens = int(raw.usage.get("completion_tokens", 0))
            data = parse_json_text(raw.text)
            items = [item for item in (data.get("preferences") or []) if isinstance(item, dict)]
            return items[:6], meta
        except Exception as exc:
            meta.fallback = True
            meta.note = f"记忆抽取失败：{type(exc).__name__}"
            meta.latency_ms = int((time.perf_counter() - started) * 1000)
            return [], meta


__all__ = [
    "IntakeAgent",
    "PlannerAgent",
    "CriticAgent",
    "VerifierAgent",
    "ReplannerAgent",
    "NarratorAgent",
    "MemoryAgent",
    "AgentMeta",
    "rule_plan",
    "TRAVEL_TOOLS",
]

#!/usr/bin/env python3
"""织序 WeaveOrder — 可审计的任务编排智能体（最小可运行原型）。

用法:
    python agent.py --mock "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元"
    python agent.py --inject-failure --mock "..."
    python agent.py "..."            # 有 INTERN_API_KEY 时走 LLM 规划
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 配置与模型
# ---------------------------------------------------------------------------


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.getenv("INTERN_API_KEY", "").strip())
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "INTERN_BASE_URL", "https://discovery-api.intern-ai.org.cn/v1"
        ).strip()
    )
    model: str = field(default_factory=lambda: os.getenv("INTERN_MODEL", "intern-s2").strip())
    max_tokens: int = field(
        default_factory=lambda: int(os.getenv("INTERN_MAX_TOKENS", "4096"))
    )
    mock: bool = False

    def __post_init__(self) -> None:
        if not self.api_key:
            self.mock = True


class LLM:
    """书生 Intern-S2 的 OpenAI 兼容客户端；无 Key 时退回规则模式。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        if not cfg.mock:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
            except Exception as exc:  # pragma: no cover - depends on local env
                print(f"[warn] 初始化模型客户端失败：{exc}；改用 mock 模式", file=sys.stderr)
                self.cfg.mock = True

    def chat(self, messages: list[dict], temperature: float = 0.2) -> str:
        if self.cfg.mock or self.client is None:
            return ""
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=temperature,
            max_tokens=self.cfg.max_tokens,
        )
        content = resp.choices[0].message.content or ""
        if not content.strip():
            raise RuntimeError(
                "模型返回空 content（可能被 max_tokens 截断）；"
                "请提高 INTERN_MAX_TOKENS 或检查模型是否支持"
            )
        return content


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未知工具：{name}")
        return self._tools[name]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def schemas(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def call(self, name: str, args: dict) -> Any:
        tool = self.get(name)
        required = tool.parameters.get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            raise ValueError(f"{name} 缺少必填参数：{missing}")
        # 容忍模型多给的字段，避免因为一个无关字段整步失败。
        sig = inspect.signature(tool.fn)
        kwargs = {k: v for k, v in args.items() if k in sig.parameters}
        return tool.fn(**kwargs)


def load_json(name: str) -> Any:
    with open(DATA_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def search_flights(
    origin: str, destination: str, date: str, max_price: int | None = None
) -> dict:
    flights = load_json("flights.json")
    rows = [
        f
        for f in flights
        if f["origin"] == origin and f["destination"] == destination and f["date"] == date
    ]
    if max_price is not None:
        cap = int(max_price)
        rows = [f for f in rows if f["price"] <= cap]
    rows.sort(key=lambda f: f["price"])

    def score(f: dict) -> float:
        # 7–10 点优先，避免红眼；价格差在 500 元内时优先早班。
        hour = int(f["depart"].split(":")[0])
        return f["price"] + (0 if 7 <= hour <= 10 else 500)

    return {
        "origin": origin,
        "destination": destination,
        "date": date,
        "options": rows,
        "cheapest": rows[0] if rows else None,
        "best": min(rows, key=score) if rows else None,
    }


def search_hotels(
    city: str, checkin: str, checkout: str, max_price: int | None = None
) -> dict:
    hotels = load_json("hotels.json")
    rows = [h for h in hotels if h["city"] == city]
    if max_price is not None:
        cap = int(max_price)
        rows = [h for h in rows if h["price"] <= cap]
    rows = [{**h, "checkin": checkin, "checkout": checkout} for h in rows]
    rows.sort(key=lambda h: h["price"])

    def score(h: dict) -> float:
        return h["price"] + float(h.get("distance_km", 0)) * 100

    return {
        "city": city,
        "checkin": checkin,
        "checkout": checkout,
        "options": rows,
        "cheapest": rows[0] if rows else None,
        "best": min(rows, key=score) if rows else None,
    }


def check_policy(query: str = "差旅") -> dict:
    text = (DATA_DIR / "policy.md").read_text(encoding="utf-8")
    clauses = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    clauses = [c for c in clauses if not c.lstrip().startswith("#")]
    terms = [t for t in re.split(r"\s+", query) if t]
    hits = [c for c in clauses if any(t in c for t in terms)]
    if not hits:
        hits = clauses
    return {"query": query, "clauses": hits[:6], "source": "data/policy.md"}


def calculator(expression: str) -> dict:
    # 只允许算术字符，避免把工具变成任意代码执行入口。
    if not re.fullmatch(r"[0-9+\-*/().\s]+", expression):
        raise ValueError("calculator 只支持算术表达式")
    value = eval(expression, {"__builtins__": {}}, {})  # noqa: S307 - 字符集已限制
    # 同时给出 result/total 别名，降低模型引用错字段的概率。
    return {"expression": expression, "value": value, "result": value, "total": value}


def compute_trip_cost(flight_price: float, hotel_price: float, nights: int) -> dict:
    """差旅费用合计 = 机票 + 酒店单价 × 晚数；由工具算，避免模型心算错误。"""
    nights = int(nights)
    flight_price = float(flight_price)
    hotel_price = float(hotel_price)
    hotel_total = hotel_price * nights
    total = flight_price + hotel_total
    return {
        "flight_price": flight_price,
        "hotel_price": hotel_price,
        "nights": nights,
        "hotel_total": hotel_total,
        "total": total,
        "value": total,
        "result": total,
    }


def draft_itinerary(
    goal: str,
    flight: dict | None = None,
    hotel: dict | None = None,
    policy: dict | None = None,
    total_cost: float | None = None,
) -> dict:
    lines = ["# 行程草案", "", f"**目标**：{goal}", ""]
    if isinstance(flight, dict) and flight:
        lines += [
            "## 交通",
            f"- {flight.get('airline', '')} {flight.get('flight_no', '')}  "
            f"{flight.get('origin', '')} → {flight.get('destination', '')}  "
            f"{flight.get('date', '')} {flight.get('depart', '')}-{flight.get('arrive', '')}  "
            f"¥{flight.get('price', '')}",
            "",
        ]
    if isinstance(hotel, dict) and hotel:
        lines += [
            "## 住宿",
            f"- {hotel.get('name', '')}  {hotel.get('star', '')} 星  "
            f"{hotel.get('checkin', '')} 至 {hotel.get('checkout', '')}  "
            f"¥{hotel.get('price', '')}/晚",
            "",
        ]
    if isinstance(policy, dict) and policy.get("clauses"):
        clauses = [
            "；".join(line.lstrip("- ").strip() for line in c.splitlines() if line.strip())
            for c in policy["clauses"][:3]
        ]
        lines += ["## 政策依据", *[f"- {c}" for c in clauses], ""]
    if total_cost is not None:
        lines += ["## 费用", f"- 预估合计：¥{total_cost}", ""]
    lines += ["## 待确认", "- 请确认航班/酒店选择与预算，确认后再进入预订。", ""]
    summary_parts: list[str] = []
    if isinstance(flight, dict) and flight:
        summary_parts.append(
            f"{flight.get('airline', '')}{flight.get('flight_no', '')} ¥{flight.get('price', '')}"
        )
    if isinstance(hotel, dict) and hotel:
        summary_parts.append(f"{hotel.get('name', '')} ¥{hotel.get('price', '')}/晚")
    if total_cost is not None:
        summary_parts.append(f"合计 ¥{total_cost}")
    return {
        "markdown": "\n".join(lines),
        "summary": "；".join(summary_parts),
        "sections": ["交通", "住宿", "政策依据", "费用", "待确认"],
    }


def request_approval(action: str, detail: str = "") -> dict:
    return {"approved": True, "action": action, "detail": detail[:2000]}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="check_policy",
            description="检索公司差旅政策条款，返回与查询相关的政策原文。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            fn=check_policy,
        )
    )
    reg.register(
        Tool(
            name="search_flights",
            description="按出发地、目的地、日期搜索航班，返回 options（按价格升序）、cheapest（最低价）和 best（综合价格与出发时间）。",
            parameters={
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "date": {"type": "string"},
                    "max_price": {"type": "integer"},
                },
                "required": ["origin", "destination", "date"],
            },
            fn=search_flights,
        )
    )
    reg.register(
        Tool(
            name="search_hotels",
            description="按城市和入住/离店日期搜索酒店，返回 options（按价格升序）、cheapest（最低价）和 best（综合价格与距离）。",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "checkin": {"type": "string"},
                    "checkout": {"type": "string"},
                    "max_price": {"type": "integer"},
                },
                "required": ["city", "checkin", "checkout"],
            },
            fn=search_hotels,
        )
    )
    reg.register(
        Tool(
            name="calculator",
            description="计算算术表达式，用于费用合计与预算校验。",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            fn=calculator,
        )
    )
    reg.register(
        Tool(
            name="compute_trip_cost",
            description="计算差旅总费用 = 机票价格 + 酒店单价 × 晚数，返回 hotel_total、total、value。优先用它做费用合计。",
            parameters={
                "type": "object",
                "properties": {
                    "flight_price": {"type": "number"},
                    "hotel_price": {"type": "number"},
                    "nights": {"type": "integer"},
                },
                "required": ["flight_price", "hotel_price", "nights"],
            },
            fn=compute_trip_cost,
        )
    )
    reg.register(
        Tool(
            name="draft_itinerary",
            description="把航班、酒店、政策依据和费用组装成可审阅的行程草案。",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "flight": {"type": "object"},
                    "hotel": {"type": "object"},
                    "policy": {"type": "object"},
                    "total_cost": {"type": "number"},
                },
                "required": ["goal"],
            },
            fn=draft_itinerary,
        )
    )
    reg.register(
        Tool(
            name="request_approval",
            description="在预订、付款等高风险动作前请求人工确认。",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string"}, "detail": {"type": "string"}},
                "required": ["action"],
            },
            fn=request_approval,
        )
    )
    return reg


# ---------------------------------------------------------------------------
# 计划与请求解析
# ---------------------------------------------------------------------------


@dataclass
class Step:
    id: str
    goal: str
    tool: str
    args: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    success: str = ""
    risk: int = 0


@dataclass
class Plan:
    goal: str
    steps: list[Step]
    assumptions: list[str] = field(default_factory=list)


@dataclass
class TravelRequest:
    origin: str = "上海"
    destination: str = "深圳"
    depart: str = "2026-10-28"
    return_date: str = "2026-10-30"
    budget: int = 3000
    preference: str = ""

    @property
    def nights(self) -> int:
        d1 = date.fromisoformat(self.depart)
        d2 = date.fromisoformat(self.return_date)
        return max(1, (d2 - d1).days)


def parse_travel_request(text: str) -> TravelRequest:
    req = TravelRequest()

    route = re.search(
        r"([\u4e00-\u9fa5]{2,6}?)\s*(?:到|至|去|飞|→|-)\s*([\u4e00-\u9fa5]{2,6}?)(?=[，,。\s的]|$)",
        text,
    )
    if route:
        req.origin = route.group(1).removeprefix("从")
        req.destination = route.group(2)

    full_dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
    short_dates = re.findall(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])", text)
    md_dates = re.findall(r"(\d{1,2})月(\d{1,2})日", text)
    dates = list(
        dict.fromkeys(
            full_dates
            + [f"2026-{int(m):02d}-{int(d):02d}" for m, d in short_dates]
            + [f"2026-{int(m):02d}-{int(d):02d}" for m, d in md_dates]
        )
    )
    if dates:
        req.depart = dates[0]
        req.return_date = (
            dates[1]
            if len(dates) > 1
            else date.fromordinal(date.fromisoformat(req.depart).toordinal() + 2).isoformat()
        )

    budget = re.search(r"预算\s*(\d+)", text)
    if not budget:
        budget = re.search(r"(\d+)\s*(?:元|块|以内)", text)
    if budget:
        req.budget = int(budget.group(1))

    if "早班" in text:
        req.preference = "偏好早班机"
    elif "中午" in text:
        req.preference = "偏好午班机"
    return req


def build_travel_plan(goal: str, req: TravelRequest, inject_failure: bool = False) -> Plan:
    flight_args: dict[str, Any] = {
        "origin": req.origin,
        "destination": req.destination,
        "date": req.depart,
    }
    if inject_failure:
        # 演示用：人为把价格上限压到 800，触发“空结果 → 重规划”。
        flight_args["max_price"] = 800

    assumptions = [
        f"{req.origin} → {req.destination}",
        f"{req.depart} 至 {req.return_date}（{req.nights} 晚）",
        f"预算 {req.budget} 元",
    ]
    if req.preference:
        assumptions.append(req.preference)

    steps = [
        Step(
            "s1",
            "检索差旅政策",
            "check_policy",
            {"query": "差旅 机票 酒店 标准 审批"},
            [],
            "返回至少 1 条政策条款",
            0,
        ),
        Step(
            "s2",
            "搜索去程航班",
            "search_flights",
            flight_args,
            [],
            "返回至少 1 个航班选项",
            0,
        ),
        Step(
            "s3",
            "搜索酒店",
            "search_hotels",
            {"city": req.destination, "checkin": req.depart, "checkout": req.return_date},
            [],
            "返回至少 1 个酒店选项",
            0,
        ),
        Step(
            "s4",
            "计算总费用",
            "compute_trip_cost",
            {
                "flight_price": "$s2.best.price",
                "hotel_price": "$s3.best.price",
                "nights": req.nights,
            },
            ["s2", "s3"],
            f"总费用不超过 {req.budget} 元",
            0,
        ),
        Step(
            "s5",
            "生成行程草案",
            "draft_itinerary",
            {
                "goal": goal,
                "flight": "$s2.best",
                "hotel": "$s3.best",
                "policy": "$s1",
                "total_cost": "$s4.total",
            },
            ["s1", "s2", "s3", "s4"],
            "包含交通、住宿、费用、政策依据",
            0,
        ),
        Step(
            "s6",
            "提交预订审批",
            "request_approval",
            {"action": "预订航班与酒店", "detail": "$s5.markdown"},
            ["s5"],
            "获得人工确认",
            2,
        ),
    ]
    return Plan(goal=goal, steps=steps, assumptions=assumptions)


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型输出中没有 JSON")
    return json.loads(text[start : end + 1])


def plan_from_dict(data: dict) -> Plan:
    steps = [
        Step(
            id=str(s["id"]),
            goal=str(s.get("goal", "")),
            tool=str(s["tool"]),
            args=s.get("args", {}) or {},
            depends_on=list(s.get("depends_on", []) or []),
            success=str(s.get("success", "")),
            risk=int(s.get("risk", 0) or 0),
        )
        for s in data["steps"]
    ]
    for step in steps:
        # 预订/审批类步骤无论模型怎么写，都必须走人工确认。
        if step.tool == "request_approval" and step.risk < 2:
            step.risk = 2
    return Plan(
        goal=str(data.get("goal", "")),
        steps=steps,
        assumptions=list(data.get("assumptions", []) or []),
    )


def validate_plan(plan: Plan, tools: ToolRegistry) -> None:
    if not plan.steps:
        raise ValueError("计划为空")
    if len(plan.steps) > 8:
        raise ValueError("计划超过 8 步")
    ids: set[str] = set()
    for step in plan.steps:
        if not step.id or step.id in ids:
            raise ValueError(f"步骤 id 重复或为空：{step.id}")
        ids.add(step.id)
        if step.tool not in tools:
            raise ValueError(f"未知工具：{step.tool}")
        required = tools.get(step.tool).parameters.get("required", [])
        missing = [k for k in required if k not in step.args]
        if missing:
            raise ValueError(f"{step.id} 缺少必填参数：{missing}")
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in ids:
                raise ValueError(f"{step.id} 依赖不存在的步骤：{dep}")
        if step.id in step.depends_on:
            raise ValueError(f"{step.id} 依赖自身")
    topological_order(plan.steps)  # 有环会抛错


def topological_order(steps: list[Step]) -> list[Step]:
    ids = {s.id for s in steps}
    indeg = {s.id: 0 for s in steps}
    children: dict[str, list[str]] = {s.id: [] for s in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep in ids:
                indeg[step.id] += 1
                children[dep].append(step.id)
    ready = [s.id for s in steps if indeg[s.id] == 0]
    order: list[Step] = []
    by_id = {s.id: s for s in steps}
    while ready:
        sid = ready.pop(0)
        order.append(by_id[sid])
        for child in children[sid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    if len(order) != len(steps):
        raise ValueError("计划存在环")
    return order


# ---------------------------------------------------------------------------
# 引用解析、Trace、编排器
# ---------------------------------------------------------------------------

FULL_REF = re.compile(r"^\$([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)$")
REF_TOKEN = re.compile(r"\$([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")


def lookup(path: str, results: dict) -> Any:
    cur: Any = results
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"引用不存在：${path}（缺少 {part}）")
            cur = cur[part]
        elif isinstance(cur, list):
            cur = cur[int(part)]
        else:
            raise KeyError(f"引用无法继续解析：${path}（在 {part} 处）")
    return cur


def resolve(value: Any, results: dict) -> Any:
    if isinstance(value, str):
        full = FULL_REF.match(value)
        if full:
            return lookup(full.group(1), results)
        return REF_TOKEN.sub(lambda m: str(lookup(m.group(1), results)), value)
    if isinstance(value, dict):
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    return value


def summarize(value: Any, limit: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit] + ("..." if len(text) > limit else "")


class Trace:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / "trace.jsonl"
        self.events: list[dict] = []

    def add(self, event_type: str, **data: Any) -> None:
        event = {"ts": datetime.now().isoformat(timespec="seconds"), "type": event_type, **data}
        self.events.append(event)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


class Orchestrator:
    def __init__(
        self,
        llm: LLM,
        tools: ToolRegistry,
        planner: str = "auto",
        auto_approve: bool = False,
        inject_failure: bool = False,
    ):
        self.llm = llm
        self.tools = tools
        self.planner = planner
        self.auto_approve = auto_approve
        self.inject_failure = inject_failure
        self.status: dict[str, tuple[str, str]] = {}
        self.repaired: set[str] = set()
        self.warnings: list[str] = []
        self.trace: Trace | None = None

    def run(self, goal: str) -> tuple[str, Path]:
        run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        run_dir.mkdir(parents=True, exist_ok=True)
        self.trace = Trace(run_dir)
        req = parse_travel_request(goal)
        self.trace.add("intake", goal=goal, parsed=asdict(req))

        plan = self.make_plan(goal, req)
        (run_dir / "plan.json").write_text(
            json.dumps(asdict(plan), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.trace.add(
            "plan",
            steps=[{"id": s.id, "tool": s.tool, "depends_on": s.depends_on} for s in plan.steps],
            assumptions=plan.assumptions,
        )

        results: dict[str, Any] = {}
        for step in topological_order(plan.steps):
            if not self.approve(step, results):
                self.status[step.id] = ("skipped", "人工未批准")
                self.trace.add("step_end", step_id=step.id, status="skipped")
                continue

            try:
                out = self.execute(step, results)
            except Exception as exc:
                out = None
                ok, reason = False, f"执行失败：{exc}"
            else:
                ok, reason = self.verify(step, out, req, results)
            self.trace.add("verify", step_id=step.id, ok=ok, reason=reason)

            if not ok and self.try_repair(step, results, req):
                out = results.get(step.id)
                ok, reason = self.verify(step, out, req, results)
                self.trace.add("verify", step_id=step.id, ok=ok, reason=reason, after_repair=True)

            if not ok:
                self.status[step.id] = ("failed", reason)
                self.trace.add("step_end", step_id=step.id, status="failed", reason=reason)
                break

            self.note_choice_warnings(step, out, req)
            results[step.id] = out
            self.status[step.id] = ("ok", reason)
            self.trace.add("step_end", step_id=step.id, status="ok")

        report = self.finalize(goal, plan, results)
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        return report, run_dir

    # -- planning ---------------------------------------------------------

    def make_plan(self, goal: str, req: TravelRequest) -> Plan:
        if self.planner == "rule" or self.llm.cfg.mock:
            return build_travel_plan(goal, req, inject_failure=self.inject_failure)
        try:
            plan = self.plan_with_llm(goal, req)
            validate_plan(plan, self.tools)
            return plan
        except Exception as exc:
            self.trace.add("plan_fallback", reason=str(exc))
            return build_travel_plan(goal, req, inject_failure=self.inject_failure)

    def plan_with_llm(self, goal: str, req: TravelRequest) -> Plan:
        system = (
            "你是 WeaveOrder 的任务规划器。把用户目标拆成最多 8 个带依赖关系的步骤。\n"
            "只输出 JSON，不要输出解释。格式：\n"
            '{"goal":"...","assumptions":["..."],"steps":[{"id":"s1","goal":"...",'
            '"tool":"工具名","args":{},"depends_on":[],"success":"...","risk":0}]}\n'
            "规则：tool 必须是给定工具之一；args 必须满足工具的 required；"
            '可以用 "$s1.path" 引用前序步骤结果；risk 0=只读，1=低风险写入，2=预订/付款/发送。\n'
            "本原型只覆盖去程航班、酒店、政策、费用和审批；不要规划返程航班搜索或未注册工具。\n"
            "费用合计优先用 compute_trip_cost（flight_price, hotel_price, nights），不要用 calculator 心算。\n"
            "compute_trip_cost 返回 total；draft_itinerary 返回 markdown 和 summary；审批步骤可引用 $sN.summary。"
        )
        user = (
            f"用户目标：{goal}\n"
            f"已解析：origin={req.origin}, destination={req.destination}, "
            f"depart={req.depart}, return={req.return_date}, budget={req.budget}, "
            f"nights={req.nights}\n"
            f"可用工具：{json.dumps(self.tools.schemas(), ensure_ascii=False)}"
        )
        raw = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        assert self.trace is not None
        self.trace.add("llm_plan_raw", content=raw[:12000])
        return plan_from_dict(extract_json(raw))

    # -- execution --------------------------------------------------------

    def approve(self, step: Step, results: dict) -> bool:
        if step.risk < 2 or self.auto_approve:
            return True
        try:
            args = resolve(step.args, results)
        except Exception:
            args = step.args
        print("\n[需要人工确认]")
        print(f"动作：{step.goal}")
        print(f"工具：{step.tool}")
        print(f"参数：{json.dumps(args, ensure_ascii=False, default=str)[:1200]}")
        try:
            answer = input("批准执行？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes", "是")
        assert self.trace is not None
        self.trace.add("approval", step_id=step.id, approved=approved)
        return approved

    def execute(self, step: Step, results: dict) -> Any:
        args = resolve(step.args, results)
        last_exc: Exception | None = None
        for attempt in (1, 2):
            start = time.perf_counter()
            assert self.trace is not None
            self.trace.add(
                "tool_call", step_id=step.id, tool=step.tool, args=args, attempt=attempt
            )
            try:
                out = self.tools.call(step.tool, args)
                latency = int((time.perf_counter() - start) * 1000)
                self.trace.add(
                    "tool_result",
                    step_id=step.id,
                    ok=True,
                    latency_ms=latency,
                    summary=summarize(out),
                )
                return out
            except Exception as exc:
                latency = int((time.perf_counter() - start) * 1000)
                self.trace.add(
                    "tool_result",
                    step_id=step.id,
                    ok=False,
                    latency_ms=latency,
                    error=str(exc),
                )
                last_exc = exc
                time.sleep(0.3)
        assert last_exc is not None
        raise last_exc

    def verify(
        self, step: Step, out: Any, req: TravelRequest, results: dict
    ) -> tuple[bool, str]:
        if out is None:
            return False, "工具返回空"
        if step.tool == "search_flights":
            if not out.get("options"):
                return False, "没有满足条件的航班"
        elif step.tool == "search_hotels":
            if not out.get("options"):
                return False, "没有满足条件的酒店"
        elif step.tool == "check_policy":
            if not out.get("clauses"):
                return False, "没有检索到政策条款"
        elif step.tool in ("calculator", "compute_trip_cost"):
            value = float(out.get("total", out.get("value", 0)))
            if value > req.budget:
                return False, f"总费用 {value:.0f} 元超过预算 {req.budget} 元"
            if "费用" in step.goal or "总" in step.goal:
                flight_res = results.get("s2") if isinstance(results.get("s2"), dict) else {}
                hotel_res = results.get("s3") if isinstance(results.get("s3"), dict) else {}
                flight = flight_res.get("best") or flight_res.get("cheapest")
                hotel = hotel_res.get("best") or hotel_res.get("cheapest")
                if flight and hotel:
                    expected = flight["price"] + hotel["price"] * req.nights
                    if abs(value - expected) > 0.01:
                        return (
                            False,
                            f"费用 {value:.0f} 与明细不一致（应为 {expected:.0f}）",
                        )
        elif step.tool == "draft_itinerary":
            markdown = out.get("markdown", "")
            for section in ("交通", "住宿", "费用"):
                if section not in markdown:
                    return False, f"行程草案缺少「{section}」段"
        return True, "ok"

    def note_choice_warnings(self, step: Step, out: Any, req: TravelRequest) -> None:
        """把“能选但不完全合规”的选项显式暴露出来，而不是静默接受。"""
        if step.tool == "search_flights":
            best = out.get("best") or out.get("cheapest")
            if not best:
                return
            hour = int(best["depart"].split(":")[0])
            if hour < 7 or hour >= 21:
                self.warnings.append(
                    f"航班 {best['flight_no']}（{best['depart']}）属于红眼/深夜航班，"
                    "与“避免红眼”政策冲突，需要人工确认或申请例外"
                )
        elif step.tool == "search_hotels":
            best = out.get("best") or out.get("cheapest")
            if not best:
                return
            limit = 550 if req.destination in ("深圳", "广州") else 600
            if best["price"] > limit:
                self.warnings.append(
                    f"酒店 {best['name']} 价格 ¥{best['price']}/晚，"
                    f"超过 {req.destination} 的 {limit} 元标准"
                )
            if float(best.get("distance_km", 0)) > 3:
                self.warnings.append(
                    f"酒店 {best['name']} 距客户地点 {best['distance_km']} km，超过 3 km 建议范围"
                )

    def try_repair(self, step: Step, results: dict, req: TravelRequest) -> bool:
        """一次确定性修复：空航班放宽价格上限；超预算压缩航班/酒店。"""
        assert self.trace is not None
        if step.id == "s2" and "s2" not in self.repaired:
            self.repaired.add("s2")
            self.trace.add("replan", reason="航班结果为空，放宽价格上限后重试")
            try:
                out = self.tools.call(
                    "search_flights",
                    {
                        "origin": req.origin,
                        "destination": req.destination,
                        "date": req.depart,
                        "max_price": int(req.budget * 0.7),
                    },
                )
            except Exception as exc:
                self.trace.add("replan_failed", reason=str(exc))
                return False
            if out.get("options"):
                results["s2"] = out
                return True
        if step.id == "s4" and "s4" not in self.repaired:
            self.repaired.add("s4")
            self.trace.add("replan", reason="总费用超预算，压缩航班/酒店价格上限")
            try:
                flights = self.tools.call(
                    "search_flights",
                    {
                        "origin": req.origin,
                        "destination": req.destination,
                        "date": req.depart,
                        "max_price": int(req.budget * 0.45),
                    },
                )
                hotels = self.tools.call(
                    "search_hotels",
                    {
                        "city": req.destination,
                        "checkin": req.depart,
                        "checkout": req.return_date,
                        "max_price": int(req.budget * 0.2),
                    },
                )
            except Exception as exc:
                self.trace.add("replan_failed", reason=str(exc))
                return False
            if not flights.get("options") or not hotels.get("options"):
                self.trace.add("replan_failed", reason="压缩价格后仍无可用选项")
                return False
            results["s2"] = flights
            results["s3"] = hotels
            # 修复后重新评估“能选但不完全合规”的选项，避免把红眼航班/远距离酒店静默带进报告。
            self.warnings.clear()
            self.note_choice_warnings(
                Step("s2", "搜索去程航班", "search_flights"), flights, req
            )
            self.note_choice_warnings(
                Step("s3", "搜索酒店", "search_hotels"), hotels, req
            )
            chosen_flight = flights.get("best") or flights["cheapest"]
            chosen_hotel = hotels.get("best") or hotels["cheapest"]
            results["s4"] = self.tools.call(
                "compute_trip_cost",
                {
                    "flight_price": chosen_flight["price"],
                    "hotel_price": chosen_hotel["price"],
                    "nights": req.nights,
                },
            )
            return True
        return False

    # -- final report -----------------------------------------------------

    def finalize(self, goal: str, plan: Plan, results: dict) -> str:
        draft = results.get("s5")
        if isinstance(draft, dict) and draft.get("markdown"):
            body = draft["markdown"]
        else:
            body = f"# 行程草案（未完成）\n\n**目标**：{goal}\n\n执行未完成，请查看执行摘要与 Trace。\n"

        lines = [body, "", "## 假设", *[f"- {a}" for a in plan.assumptions], ""]
        if self.warnings:
            lines += ["## 风险与冲突", *[f"- {w}" for w in self.warnings], ""]
        lines += ["## 执行摘要", "", "| 步骤 | 工具 | 状态 | 说明 |", "|---|---|---|---|"]
        for step in plan.steps:
            status, reason = self.status.get(step.id, ("skipped", "未执行"))
            safe_reason = str(reason).replace("|", "\\|")[:100]
            lines.append(f"| {step.id} {step.goal} | {step.tool} | {status} | {safe_reason} |")
        lines += [
            "",
            "## 可审计性",
            "- 每一步的工具调用、参数、返回摘要和校验结论都写入 `trace.jsonl`。",
            "- 预订类动作标记为 `risk=2`，执行前必须人工确认。",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="织序 WeaveOrder 最小原型")
    parser.add_argument("goal", nargs="+", help="一句话目标")
    parser.add_argument("--mock", action="store_true", help="强制使用规则规划与 mock 数据")
    parser.add_argument("--yes", action="store_true", help="自动批准高风险动作（演示用）")
    parser.add_argument(
        "--planner", choices=["auto", "llm", "rule"], default="auto", help="规划器选择"
    )
    parser.add_argument(
        "--inject-failure",
        action="store_true",
        help="演示用：把航班价格上限压到 800，触发空结果→重规划",
    )
    args = parser.parse_args()

    goal = " ".join(args.goal)
    cfg = Config(mock=args.mock)
    llm = LLM(cfg)
    tools = build_registry()
    orch = Orchestrator(
        llm,
        tools,
        planner=args.planner,
        auto_approve=args.yes,
        inject_failure=args.inject_failure,
    )
    report, run_dir = orch.run(goal)
    print("\n" + "=" * 72)
    print(report)
    print("=" * 72)
    print(f"\nTrace: {run_dir / 'trace.jsonl'}")
    print(f"Plan : {run_dir / 'plan.json'}")
    print(f"Report: {run_dir / 'report.md'}")


if __name__ == "__main__":
    main()

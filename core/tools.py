"""类型化工具层：声明式 JSON Schema + 参数校验/纠偏 + 超时 + 风险等级。

工具是智能体与真实世界之间唯一的写入口。因此这里做三件事：
1. 模型给的参数先校验、再纠偏（``"2"`` → ``2``），不合法的调用直接失败；
2. 每个工具声明风险等级，``risk >= 2`` 的动作必须经过审批闸门；
3. 输出保持结构化，便于结果引用（``$s2.best.price``）和报告回链。
"""

from __future__ import annotations

import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import catalog, policy as policy_mod
from .config import Config


class ToolError(ValueError):
    pass


class ToolTimeout(ToolError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Any]
    risk: int = 0
    category: str = "general"
    timeout_s: Optional[float] = None
    returns: str = ""
    examples: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "category": self.category,
            "returns": self.returns,
            "examples": list(self.examples),
        }

    def openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        self.cfg = cfg or Config()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="weave-tool")
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0

    # -- registry --------------------------------------------------------
    def register(self, tool: Tool) -> None:
        with self._lock:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(f"未知工具：{name}；可用：{', '.join(sorted(self._tools))}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def required(self, name: str) -> List[str]:
        return list(self.get(name).parameters.get("required") or [])

    def schemas(self) -> List[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def openai_tools(self) -> List[dict]:
        return [tool.openai_schema() for tool in self._tools.values()]

    def describe_compact(self, only: Optional[Iterable[str]] = None) -> str:
        allowed = set(only) if only else None
        lines = []
        for tool in self._tools.values():
            if allowed is not None and tool.name not in allowed:
                continue
            props = tool.parameters.get("properties") or {}
            required = set(tool.parameters.get("required") or [])
            args = ", ".join(
                f"{name}{'' if name in required else '?'}:{meta.get('type', 'any')}"
                for name, meta in props.items()
            )
            lines.append(f"- {tool.name}({args}) — {tool.description}")
        return "\n".join(lines)

    # -- invoke ----------------------------------------------------------
    def validate_args(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.get(name)
        if args is None:
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception as exc:
                raise ToolError(f"{name} 参数不是合法 JSON 对象：{exc}") from exc
        if not isinstance(args, dict):
            raise ToolError(f"{name} 参数必须是对象")
        schema = tool.parameters or {}
        properties: Dict[str, dict] = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        cleaned: Dict[str, Any] = {}
        for key, value in args.items():
            if key in properties:
                cleaned[key] = self._coerce(name, key, value, properties[key])
        for key, meta in properties.items():
            if key not in cleaned and "default" in meta:
                cleaned[key] = meta["default"]
        missing = [
            key
            for key in required
            if key not in cleaned or cleaned[key] in (None, "")
        ]
        if missing:
            raise ToolError(f"{name} 缺少必填参数：{missing}")
        return cleaned

    def _coerce(self, tool: str, key: str, value: Any, meta: dict) -> Any:
        target = meta.get("type")
        if value is None:
            return None
        coerced = value
        try:
            if target == "integer":
                if isinstance(value, bool):
                    raise ValueError
                if isinstance(value, str):
                    value = value.strip().replace(",", "")
                coerced = int(float(value))
            elif target == "number":
                if isinstance(value, bool):
                    raise ValueError
                if isinstance(value, str):
                    value = value.strip().replace(",", "").replace("¥", "").replace("元", "")
                coerced = float(value)
            elif target == "boolean":
                if isinstance(value, str):
                    coerced = value.strip().lower() in ("1", "true", "yes", "y", "是")
                else:
                    coerced = bool(value)
            elif target == "object":
                if isinstance(value, str):
                    coerced = json.loads(value)
                elif not isinstance(value, dict):
                    raise ValueError("需要对象")
                else:
                    coerced = value
            elif target == "array":
                if isinstance(value, str):
                    text = value.strip()
                    coerced = json.loads(text) if text.startswith("[") else [
                        item.strip() for item in text.split(",") if item.strip()
                    ]
                elif not isinstance(value, list):
                    coerced = [value]
                else:
                    coerced = value
            elif target == "string":
                coerced = (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
        except Exception as exc:
            raise ToolError(f"{tool}.{key} 参数类型应为 {target}，收到 {value!r}：{exc}") from exc
        enum = meta.get("enum")
        if enum:
            aliases = {
                "经济舱": "economy",
                "普通舱": "economy",
                "公务舱": "business",
                "商务舱": "business",
                "头等舱": "first",
                "飞机经济舱": "economy",
            }
            if isinstance(coerced, str):
                coerced = aliases.get(coerced.strip(), coerced)
                coerced = aliases.get(coerced, coerced)
            if coerced not in enum:
                raise ToolError(f"{tool}.{key} 必须是 {enum} 之一，收到 {coerced!r}")
        return coerced

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        tool = self.get(name)
        prepared = self.validate_args(name, args or {})
        self.calls += 1
        timeout = tool.timeout_s or self.cfg.tool_timeout
        future = self._executor.submit(tool.fn, **prepared)
        try:
            result = future.result(timeout=timeout)
        except FutureTimeout as exc:
            self.errors += 1
            raise ToolTimeout(f"{name} 超时（>{timeout:.0f}s）") from exc
        except ToolError:
            self.errors += 1
            raise
        except Exception as exc:
            self.errors += 1
            raise ToolError(f"{name} 执行失败：{type(exc).__name__}: {exc}") from exc
        return result


# ---------------------------------------------------------------------------
# 业务工具实现
# ---------------------------------------------------------------------------


def check_policy(
    query: str = "差旅 机票 酒店 标准 审批",
    top_k: int = 4,
    category: Optional[str] = None,
) -> dict:
    return policy_mod.search(query=query, top_k=max(1, min(int(top_k), 8)), category=category)


def search_flights(
    origin: str,
    destination: str,
    date: str,
    max_price: Optional[int] = None,
    depart_after: Optional[str] = None,
    depart_before: Optional[str] = None,
    direct_only: bool = False,
    cabin: str = "economy",
) -> dict:
    return catalog.search_flights(
        origin=origin,
        destination=destination,
        date=date,
        max_price=max_price,
        depart_after=depart_after,
        depart_before=depart_before,
        direct_only=bool(direct_only),
        cabin=cabin or "economy",
    )


def search_hotels(
    city: str,
    checkin: str,
    checkout: str,
    max_price: Optional[int] = None,
    min_star: Optional[int] = None,
    max_distance_km: Optional[float] = None,
) -> dict:
    return catalog.search_hotels(
        city=city,
        checkin=checkin,
        checkout=checkout,
        max_price=max_price,
        min_star=min_star,
        max_distance_km=max_distance_km,
    )


def compute_trip_cost(
    outbound_price: float,
    return_price: float,
    hotel_price: float,
    nights: int,
    local_transport: float = 0,
    budget: Optional[float] = None,
) -> dict:
    outbound = float(outbound_price)
    inbound = float(return_price)
    hotel_night = float(hotel_price)
    nights = max(1, int(nights))
    local = float(local_transport or 0)
    for label, value in (("去程机票", outbound), ("返程机票", inbound), ("酒店单价", hotel_night), ("市内交通", local)):
        if value < 0 or math.isnan(value):
            raise ToolError(f"{label}不能为负数：{value}")
    hotel_total = round(hotel_night * nights, 2)
    total = round(outbound + inbound + hotel_total + local, 2)
    breakdown = [
        {"item": "去程机票", "detail": "经济舱", "amount": outbound},
        {"item": "返程机票", "detail": "经济舱", "amount": inbound},
        {"item": "酒店住宿", "detail": f"¥{hotel_night:.0f} × {nights} 晚", "amount": hotel_total},
    ]
    if local:
        breakdown.append({"item": "市内交通", "detail": "补贴上限内", "amount": local})
    result: Dict[str, Any] = {
        "outbound_price": outbound,
        "return_price": inbound,
        "flight_total": round(outbound + inbound, 2),
        "hotel_price": hotel_night,
        "hotel_total": hotel_total,
        "nights": nights,
        "local_transport": local,
        "breakdown": breakdown,
        "total": total,
        "value": total,
        "result": total,
        "currency": "CNY",
    }
    if budget is not None:
        cap = float(budget)
        result.update(
            {
                "budget": cap,
                "remaining": round(cap - total, 2),
                "over_budget": total > cap,
                "budget_used_pct": round(total / cap * 100, 1) if cap else None,
            }
        )
    return result


def _fmt_money(value: Any) -> str:
    try:
        return f"¥{float(value):,.0f}"
    except Exception:
        return "—"


def _flight_row(leg: str, flight: Optional[dict]) -> str:
    if not isinstance(flight, dict) or not flight:
        return f"| {leg} | 待定 | - | - | - |"
    arrive = flight.get("arrive", "")
    if flight.get("arrive_next_day"):
        arrive = f"{arrive} (+1)"
    return (
        f"| {leg} | {flight.get('date', '')} | {flight.get('airline', '')} "
        f"{flight.get('flight_no', '')} | {flight.get('depart', '')}–{arrive} | "
        f"{_fmt_money(flight.get('price'))} |"
    )


def _policy_citations(policy_data: Optional[dict], limit: int = 4) -> List[str]:
    if not isinstance(policy_data, dict):
        return []
    citations = []
    for clause in (policy_data.get("clauses") or [])[:limit]:
        if isinstance(clause, dict):
            citations.append(f"[{clause.get('id', '?')}] {clause.get('text', '')}")
    return citations


def draft_itinerary(
    goal: str,
    outbound: Optional[dict] = None,
    inbound: Optional[dict] = None,
    hotel: Optional[dict] = None,
    policy: Optional[dict] = None,
    cost: Optional[dict] = None,
    budget: Optional[float] = None,
    nights: Optional[int] = None,
) -> dict:
    """把选中的航班、酒店、费用与政策组装成可审阅的行程单。"""

    outbound = outbound if isinstance(outbound, dict) else None
    inbound = inbound if isinstance(inbound, dict) else None
    hotel = hotel if isinstance(hotel, dict) else None
    cost = cost if isinstance(cost, dict) else None
    city = ""
    if hotel:
        city = str(hotel.get("city", ""))
    if not city and outbound:
        city = str(outbound.get("destination", ""))
    nights_value = int(
        nights
        or (hotel or {}).get("nights")
        or (cost or {}).get("nights")
        or 1
    )
    total = None
    if cost:
        try:
            total = float(cost.get("total", cost.get("value")))  # type: ignore[arg-type]
        except Exception:
            total = None
    if total is None and outbound and inbound and hotel:
        total = (
            float(outbound.get("price", 0))
            + float(inbound.get("price", 0))
            + float(hotel.get("price", 0)) * nights_value
        )
    if budget is None and isinstance(cost, dict):
        try:
            budget = float(cost["budget"]) if cost.get("budget") is not None else None
        except Exception:
            budget = None

    compliance_result = policy_mod.compliance(
        outbound=outbound,
        inbound=inbound,
        hotel=hotel,
        total_cost=total,
        budget=budget,
        city=city,
        nights=nights_value,
    )

    origin = (outbound or {}).get("origin", "")
    destination = (outbound or {}).get("destination", "") or city
    depart = (outbound or {}).get("date", "")
    ret = (inbound or {}).get("date", "")
    purpose = "商务出行"
    for label in ("客户拜访", "商务会议", "内部培训", "面试招聘", "团队建设", "行业调研"):
        if label in goal:
            purpose = label
            break
    title = f"{destination or '目的地'}{purpose} · 差旅方案"

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    meta = []
    if depart:
        meta.append(f"{depart} → {ret or '待定'}")
    if nights_value:
        meta.append(f"{nights_value} 晚")
    if budget:
        meta.append(f"预算 {_fmt_money(budget)}")
    if meta:
        lines.append("> " + " · ".join(meta))
        lines.append("")
    lines.append("## 行程总览")
    lines.append("")
    lines.append(f"- **目标**：{goal}")
    if origin or destination:
        lines.append(f"- **路线**：{origin or '待定'} → {destination or '待定'}（往返）")
    if hotel:
        lines.append(
            f"- **入住**：{hotel.get('name', '待定')}，{hotel.get('checkin', '')} 至 "
            f"{hotel.get('checkout', '')}，{nights_value} 晚"
        )
    lines.append(f"- **合规评分**：{compliance_result['score']}/100")
    lines.append("")
    lines.append("## 交通")
    lines.append("")
    lines.append("| 航段 | 日期 | 航班 | 时间 | 价格 |")
    lines.append("|---|---|---|---|---|")
    lines.append(_flight_row("去程", outbound))
    lines.append(_flight_row("返程", inbound))
    lines.append("")
    if hotel:
        lines.append("## 住宿")
        lines.append("")
        lines.append(f"- **{hotel.get('name', '')}**（{hotel.get('star', '')} 星）")
        lines.append(f"- 地址：{hotel.get('address', '')}；距客户地点 {hotel.get('distance_km', '?')} km")
        lines.append(
            f"- 价格：{_fmt_money(hotel.get('price'))}/晚 × {nights_value} 晚 = "
            f"{_fmt_money(float(hotel.get('price', 0)) * nights_value)}"
        )
        lines.append(f"- 可免费取消：{'是' if hotel.get('refundable', True) else '否'}；含早：{'是' if hotel.get('breakfast') else '否'}")
        lines.append("")
    if cost:
        lines.append("## 费用明细")
        lines.append("")
        lines.append("| 项目 | 说明 | 金额 |")
        lines.append("|---|---|---|")
        for item in cost.get("breakdown") or []:
            lines.append(
                f"| {item.get('item', '')} | {item.get('detail', '')} | {_fmt_money(item.get('amount'))} |"
            )
        lines.append(f"| **合计** | 往返交通 + 住宿 | **{_fmt_money(total)}** |")
        lines.append("")
        if budget:
            remaining = float(budget) - float(total or 0)
            status = "超出预算" if remaining < 0 else "预算内"
            lines.append(
                f"- 预算 {_fmt_money(budget)}，{status}；剩余 {_fmt_money(remaining)}"
            )
            lines.append("")
    citations = _policy_citations(policy)
    if citations:
        lines.append("## 政策依据")
        lines.append("")
        for citation in citations:
            lines.append(f"- {citation}")
        lines.append("")
    lines.append("## 合规检查")
    lines.append("")
    lines.append(f"- 结论：{'通过' if compliance_result['ok'] else '存在阻塞项'}（评分 {compliance_result['score']}/100）")
    if compliance_result["violations"]:
        for item in compliance_result["violations"]:
            lines.append(f"- 🔴 {item['message']}")
    if compliance_result["warnings"]:
        for item in compliance_result["warnings"]:
            lines.append(f"- 🟡 {item['message']}")
    if not compliance_result["violations"] and not compliance_result["warnings"]:
        lines.append("- ✅ 未发现政策冲突")
    lines.append("")
    lines.append("## 待确认")
    lines.append("")
    lines.append("- 请确认航班、酒店与预算；确认后才会进入预订环节。")
    if hotel:
        lines.append("- 客户具体办公地点未提供时，酒店距离为数据源参考值，建议确认后复核。")
    lines.append("- 航班与酒店价格来自 mock 数据源，预订前以实际渠道为准。")
    if compliance_result["violations"] or compliance_result["warnings"]:
        lines.append("- 上述风险项需要在审批时说明，或由审批人选择例外。")
    lines.append("")

    summary_parts = []
    if outbound:
        summary_parts.append(
            f"去程 {outbound.get('airline', '')}{outbound.get('flight_no', '')} "
            f"{outbound.get('date', '')} {outbound.get('depart', '')} {_fmt_money(outbound.get('price'))}"
        )
    if inbound:
        summary_parts.append(
            f"返程 {inbound.get('airline', '')}{inbound.get('flight_no', '')} "
            f"{inbound.get('date', '')} {inbound.get('depart', '')} {_fmt_money(inbound.get('price'))}"
        )
    if hotel:
        summary_parts.append(
            f"{hotel.get('name', '')} {_fmt_money(hotel.get('price'))}/晚 × {nights_value} 晚"
        )
    if total is not None:
        summary_parts.append(f"合计 {_fmt_money(total)}")
    return {
        "markdown": "\n".join(lines),
        "summary": "；".join(summary_parts),
        "title": title,
        "sections": ["行程总览", "交通", "住宿", "费用明细", "政策依据", "合规检查", "待确认"],
        "compliance": compliance_result,
        "facts": {
            "origin": origin,
            "destination": destination,
            "depart": depart,
            "return_date": ret,
            "nights": nights_value,
            "total": total,
            "budget": budget,
            "outbound_flight": (outbound or {}).get("flight_no"),
            "inbound_flight": (inbound or {}).get("flight_no"),
            "hotel": (hotel or {}).get("name"),
            "hotel_price": (hotel or {}).get("price"),
            "compliance_score": compliance_result["score"],
            "violations": [item["message"] for item in compliance_result["violations"]],
            "warnings": [item["message"] for item in compliance_result["warnings"]],
        },
    }


def policy_compliance(
    outbound_price: float,
    return_price: float,
    hotel_price: float,
    nights: int,
    city: str = "深圳",
    total_cost: Optional[float] = None,
    budget: Optional[float] = None,
    depart_time: str = "08:00",
    distance_km: float = 1.0,
) -> dict:
    """按价格/时段快速做一次政策检查（Agent 模式常用）。"""

    outbound = {"price": outbound_price, "depart": depart_time, "destination": city}
    inbound = {"price": return_price, "depart": depart_time}
    hotel = {"price": hotel_price, "distance_km": distance_km, "city": city}
    total = total_cost
    if total is None:
        total = outbound_price + return_price + hotel_price * max(1, int(nights))
    return policy_mod.compliance(
        outbound=outbound,
        inbound=inbound,
        hotel=hotel,
        total_cost=total,
        budget=budget,
        city=city,
        nights=max(1, int(nights)),
    )


def request_approval(action: str, detail: str = "", amount: Optional[float] = None) -> dict:
    return {
        "approved": True,
        "status": "approved",
        "action": action,
        "amount": amount,
        "detail": (detail or "")[:4000],
        "note": "由织序的审批闸门放行；工具本身不会自动下单或付款。",
    }


def make_memory_tools(memory: Any) -> List[Tool]:
    """记忆工具通过闭包绑定到本次运行的 MemoryStore。"""

    def get_travel_profile() -> dict:
        if memory is None:
            return {"profile": {}, "episodes": []}
        profile = memory.profile()
        return {
            "profile": profile,
            "episodes": memory.recent_episodes(limit=3),
            "context": memory.context_text(),
        }

    def save_travel_preference(key: str, value: str, evidence: str = "") -> dict:
        if memory is None:
            return {"saved": False, "reason": "记忆未启用"}
        saved = memory.set_preference(key, value, evidence=evidence, source="tool")
        return {"saved": bool(saved), "key": key, "value": value, "profile": memory.profile()}

    return [
        Tool(
            name="get_travel_profile",
            description="读取长期记忆中的差旅偏好与最近的历史行程（酒店品牌、预算习惯、常飞路线）。",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            fn=get_travel_profile,
            risk=0,
            category="memory",
            returns="profile / episodes / context",
        ),
        Tool(
            name="save_travel_preference",
            description="把用户明确表达的长期偏好写入记忆，例如酒店品牌、预算习惯、座位偏好。",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "偏好键，如 hotel_brand / budget_habit"},
                    "value": {"type": "string", "description": "偏好值"},
                    "evidence": {"type": "string", "description": "用户原话依据"},
                },
                "required": ["key", "value"],
            },
            fn=save_travel_preference,
            risk=1,
            category="memory",
            returns="{saved, profile}",
        ),
    ]


def build_registry(memory: Any = None, cfg: Optional[Config] = None) -> ToolRegistry:
    reg = ToolRegistry(cfg)
    reg.register(
        Tool(
            name="check_policy",
            description="检索公司差旅政策条款，返回带编号的条款、规则阈值与引用。规划前先调用。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，如：机票 酒店 预算 审批"},
                    "top_k": {"type": "integer", "default": 4, "description": "返回条款数（1-8）"},
                    "category": {"type": "string", "description": "可选：机票 / 酒店 / 审批 / 交通与补贴"},
                },
                "required": ["query"],
            },
            fn=check_policy,
            risk=0,
            category="policy",
            returns="{clauses:[{id,text,citation}], rules}",
            examples=['check_policy(query="机票 酒店 预算 审批")'],
        )
    )
    reg.register(
        Tool(
            name="search_flights",
            description="按出发地、目的地、日期搜索单程航班；返回 options（价格升序）、cheapest、best（综合价格/时段/退改）。不覆盖的城市返回空列表，不会编造。",
            parameters={
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "max_price": {"type": "integer"},
                    "depart_after": {"type": "string", "description": "HH:MM，可只保留晚于该时间的航班"},
                    "depart_before": {"type": "string", "description": "HH:MM"},
                    "direct_only": {"type": "boolean", "default": False},
                    "cabin": {"type": "string", "default": "economy", "enum": ["economy", "business", "first"]},
                },
                "required": ["origin", "destination", "date"],
            },
            fn=search_flights,
            risk=0,
            category="travel",
            returns="{options, cheapest, best, coverage}",
            examples=[
                'search_flights(origin="上海", destination="深圳", date="2026-10-28", max_price=1500)'
            ],
        )
    )
    reg.register(
        Tool(
            name="search_hotels",
            description="按城市、入住/离店日期搜索酒店；返回 options（价格升序）、cheapest、best（综合价格/距离/退改），并计算 nights 与总价。",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "checkin": {"type": "string", "description": "YYYY-MM-DD"},
                    "checkout": {"type": "string", "description": "YYYY-MM-DD"},
                    "max_price": {"type": "integer", "description": "每晚价格上限"},
                    "min_star": {"type": "integer"},
                    "max_distance_km": {"type": "number"},
                },
                "required": ["city", "checkin", "checkout"],
            },
            fn=search_hotels,
            risk=0,
            category="travel",
            returns="{options, cheapest, best, nights}",
            examples=[
                'search_hotels(city="深圳", checkin="2026-10-28", checkout="2026-10-30", max_price=520)'
            ],
        )
    )
    reg.register(
        Tool(
            name="compute_trip_cost",
            description="计算差旅总费用 = 去程机票 + 返程机票 + 酒店单价 × 晚数 + 市内交通；返回分项明细、total 与预算差额。不要用心算。",
            parameters={
                "type": "object",
                "properties": {
                    "outbound_price": {"type": "number"},
                    "return_price": {"type": "number"},
                    "hotel_price": {"type": "number", "description": "每晚价格"},
                    "nights": {"type": "integer"},
                    "local_transport": {"type": "number", "default": 0},
                    "budget": {"type": "number", "description": "可选，用于计算剩余预算"},
                },
                "required": ["outbound_price", "return_price", "hotel_price", "nights"],
            },
            fn=compute_trip_cost,
            risk=0,
            category="travel",
            returns="{breakdown, total, remaining, over_budget}",
            examples=[
                "compute_trip_cost(outbound_price=980, return_price=930, hotel_price=480, nights=2, budget=3000)"
            ],
        )
    )
    reg.register(
        Tool(
            name="draft_itinerary",
            description="把去程/返程航班、酒店、政策依据、费用组装成带合规检查的行程单；返回 markdown、summary 与 compliance。",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "outbound": {"type": "object", "description": "去程航班对象，通常引用 $s2.best"},
                    "inbound": {"type": "object", "description": "返程航班对象，通常引用 $s3.best"},
                    "hotel": {"type": "object", "description": "酒店对象，通常引用 $s4.best"},
                    "policy": {"type": "object", "description": "政策检索结果，通常引用 $s1"},
                    "cost": {"type": "object", "description": "费用结果，通常引用 $s5"},
                    "budget": {"type": "number"},
                    "nights": {"type": "integer"},
                },
                "required": ["goal"],
            },
            fn=draft_itinerary,
            risk=0,
            category="travel",
            returns="{markdown, summary, compliance, facts}",
            examples=['draft_itinerary(goal="上海到深圳客户拜访", outbound="$s2.best", inbound="$s3.best", hotel="$s4.best", policy="$s1", cost="$s5", budget=3000)'],
        )
    )
    reg.register(
        Tool(
            name="policy_compliance",
            description="按价格、时段、住宿天数、预算做确定性合规检查，返回 blockers 与 warnings。提交审批前可复核。",
            parameters={
                "type": "object",
                "properties": {
                    "outbound_price": {"type": "number"},
                    "return_price": {"type": "number"},
                    "hotel_price": {"type": "number"},
                    "nights": {"type": "integer"},
                    "city": {"type": "string"},
                    "total_cost": {"type": "number"},
                    "budget": {"type": "number"},
                    "depart_time": {"type": "string"},
                    "distance_km": {"type": "number", "default": 1.0},
                },
                "required": ["outbound_price", "return_price", "hotel_price", "nights"],
            },
            fn=policy_compliance,
            risk=0,
            category="policy",
            returns="{ok, score, violations, warnings}",
        )
    )
    reg.register(
        Tool(
            name="request_approval",
            description="在预订、付款等高风险动作前提交人工确认。执行引擎会先暂停并等待审批人，通过后本工具返回 approved=true。",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "detail": {"type": "string"},
                    "amount": {"type": "number"},
                },
                "required": ["action"],
            },
            fn=request_approval,
            risk=2,
            category="governance",
            returns="{approved, action, amount}",
            examples=['request_approval(action="预订航班与酒店", detail="$s6.summary", amount="$s5.total")'],
        )
    )
    for tool in make_memory_tools(memory):
        reg.register(tool)
    return reg


__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolError",
    "ToolTimeout",
    "build_registry",
    "check_policy",
    "search_flights",
    "search_hotels",
    "compute_trip_cost",
    "draft_itinerary",
    "policy_compliance",
    "request_approval",
]

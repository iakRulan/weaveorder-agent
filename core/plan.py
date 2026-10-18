"""计划数据结构、DAG 校验、结果引用解析。

这一层刻意保持「确定性与可测试」：模型只负责产出 JSON，结构合法性、
依赖闭环、参数引用和风险等级全部由引擎检查。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .llm import parse_json_text

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

# 支持 $s2.best.price、$s2.options[0].price、$s5.markdown
FULL_REF = re.compile(r"^\$([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+|\[\d+\])*)$")
REF_TOKEN = re.compile(r"\$([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+|\[\d+\])*)")
PATH_PART = re.compile(r"[A-Za-z0-9_]+|\[\d+\]")


class PlanError(ValueError):
    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("；".join(self.errors))


@dataclass
class Step:
    id: str
    goal: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    success: str = ""
    risk: int = 0
    optional: bool = False
    timeout_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "tool": self.tool,
            "args": self.args,
            "depends_on": list(self.depends_on),
            "success": self.success,
            "risk": self.risk,
            "optional": self.optional,
            **({"timeout_s": self.timeout_s} if self.timeout_s else {}),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Step":
        return cls(
            id=str(raw.get("id", "")).strip(),
            goal=str(raw.get("goal", "")).strip(),
            tool=str(raw.get("tool", "")).strip(),
            args=raw.get("args") if isinstance(raw.get("args"), dict) else {},
            depends_on=list(dict.fromkeys(str(d) for d in (raw.get("depends_on") or []))),
            success=str(raw.get("success", "")).strip(),
            risk=_as_int(raw.get("risk", 0)),
            optional=bool(raw.get("optional", False)),
            timeout_s=raw.get("timeout_s"),
        )


@dataclass
class Plan:
    goal: str
    steps: List[Step]
    assumptions: List[str] = field(default_factory=list)
    strategy: str = "dag"
    created_by: str = "llm"
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "assumptions": list(self.assumptions),
            "strategy": self.strategy,
            "created_by": self.created_by,
            "notes": self.notes,
            "steps": [s.to_dict() for s in self.steps],
        }

    def by_id(self, step_id: str) -> Optional[Step]:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return default


def plan_from_dict(raw: Dict[str, Any], *, strategy: str = "dag", created_by: str = "llm") -> Plan:
    steps: List[Step] = []
    for item in raw.get("steps") or []:
        if not isinstance(item, dict):
            continue
        step = Step.from_dict(item)
        if step.tool == "request_approval":
            step.risk = max(2, step.risk)
        steps.append(step)
    assumptions = [str(a) for a in (raw.get("assumptions") or []) if str(a).strip()]
    return Plan(
        goal=str(raw.get("goal", "")).strip(),
        steps=steps,
        assumptions=assumptions,
        strategy=strategy,
        created_by=created_by,
        notes=str(raw.get("notes", "")).strip(),
    )


def validate_plan(
    plan: Plan,
    registry: Any,
    *,
    max_steps: int = 10,
    available_refs: Optional[Set[str]] = None,
) -> List[str]:
    errors: List[str] = []
    if not plan.steps:
        return ["计划为空"]
    if len(plan.steps) > max_steps:
        errors.append(f"步骤数 {len(plan.steps)} 超过上限 {max_steps}")
    ids: Set[str] = set()
    for step in plan.steps:
        if not ID_RE.match(step.id or ""):
            errors.append(f"步骤 id 非法：{step.id!r}（需字母开头，仅字母数字下划线）")
            continue
        if step.id in ids:
            errors.append(f"步骤 id 重复：{step.id}")
        ids.add(step.id)
        if not registry.has(step.tool):
            errors.append(f"{step.id} 使用未知工具：{step.tool}")
            continue
        required = registry.required(step.tool)
        missing = [key for key in required if key not in (step.args or {})]
        if missing:
            errors.append(f"{step.id} 缺少必填参数：{missing}")
        if step.risk < 0 or step.risk > 3:
            errors.append(f"{step.id} risk 超出 0–3：{step.risk}")
        if step.depends_on and step.id in step.depends_on:
            errors.append(f"{step.id} 依赖自身")
        for dep in step.depends_on:
            if dep not in {s.id for s in plan.steps}:
                errors.append(f"{step.id} 依赖不存在的步骤：{dep}")
    if errors:
        return errors
    try:
        topological_levels(plan.steps)
    except PlanError as exc:
        errors.extend(exc.errors)
    if available_refs:
        for step in plan.steps:
            for ref in refs_in(step.args):
                root = ref.split(".", 1)[0].split("[", 1)[0]
                if root not in available_refs and root not in ids:
                    errors.append(f"{step.id} 引用了未知结果：${ref}")
    errors.extend(_validate_travel_args(plan))
    return errors


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(FULL_REF.match(value.strip()))


def _price_is_grounded(value: Any) -> bool:
    """价格必须来自工具结果引用，或明确为 0（例如单程无返程）。"""

    if _is_ref(value):
        return True
    try:
        return float(value) == 0.0
    except Exception:
        return False


def _validate_travel_args(plan: Plan) -> List[str]:
    """防止 Replanner 用“估算价”替代工具结果，制造看似完成的假数据。"""

    errors: List[str] = []
    for step in plan.steps:
        args = step.args if isinstance(step.args, dict) else {}
        if step.tool == "compute_trip_cost":
            for key in ("outbound_price", "return_price", "hotel_price"):
                if key in args and not _price_is_grounded(args.get(key)):
                    errors.append(
                        f"{step.id} 的 {key} 必须引用工具结果（$sN...），不能使用估算数字 {args.get(key)!r}"
                    )
        elif step.tool == "draft_itinerary":
            for key in ("outbound", "inbound", "hotel"):
                if key not in args:
                    continue
                value = args.get(key)
                if _is_ref(value):
                    continue
                if isinstance(value, dict):
                    valid = (
                        ("flight_no" in value or "price" in value)
                        if key in ("outbound", "inbound")
                        else ("name" in value and "price" in value)
                    )
                    if not valid:
                        errors.append(
                            f"{step.id} 的 {key} 是缺少真实字段的对象，必须引用工具结果"
                        )
                else:
                    errors.append(f"{step.id} 的 {key} 必须引用航班/酒店工具结果")
    return errors


def refs_in(value: Any) -> List[str]:
    refs: List[str] = []
    if isinstance(value, str):
        refs.extend(REF_TOKEN.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            refs.extend(refs_in(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(refs_in(item))
    return refs


def topological_levels(steps: Sequence[Step]) -> List[List[Step]]:
    """返回分层拓扑序；同层步骤互不依赖，可以并行执行。"""

    by_id = {s.id: s for s in steps}
    indeg = {s.id: 0 for s in steps}
    children: Dict[str, List[str]] = {s.id: [] for s in steps}
    for step in steps:
        step.depends_on = list(dict.fromkeys(step.depends_on))
        for dep in step.depends_on:
            if dep not in by_id:
                raise PlanError([f"{step.id} 依赖不存在的步骤：{dep}"])
            indeg[step.id] += 1
            children[dep].append(step.id)
    current = [s.id for s in steps if indeg[s.id] == 0]
    levels: List[List[Step]] = []
    seen = 0
    while current:
        levels.append([by_id[sid] for sid in current])
        nxt: List[str] = []
        for sid in current:
            seen += 1
            for child in children[sid]:
                indeg[child] -= 1
                if indeg[child] == 0:
                    nxt.append(child)
        current = nxt
    if seen != len(steps):
        raise PlanError(["计划存在环（依赖无法拓扑排序）"])
    return levels


def topological_order(steps: Sequence[Step]) -> List[Step]:
    return [step for level in topological_levels(steps) for step in level]


def lookup(path: str, results: Dict[str, Any]) -> Any:
    parts = PATH_PART.findall(path)
    if not parts:
        raise KeyError(f"引用为空：${path}")
    root = parts[0]
    if root not in results:
        known = ", ".join(sorted(results)) or "（还无结果）"
        raise KeyError(f"引用不存在：${path}（步骤 {root} 尚未产出；已有：{known}）")
    current: Any = results[root]
    for part in parts[1:]:
        if part.startswith("["):
            index = int(part[1:-1])
            if not isinstance(current, list):
                raise KeyError(f"引用无法继续：${path}（{part} 需要列表）")
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(f"引用越界：${path}（列表长度 {len(current)}）") from exc
        else:
            if isinstance(current, dict):
                if part not in current:
                    keys = ", ".join(list(current)[:12])
                    raise KeyError(f"引用不存在：${path}（缺少字段 {part}；可用：{keys}）")
                current = current[part]
            else:
                raise KeyError(f"引用无法继续：${path}（{part} 处不是对象）")
    return current


def resolve(value: Any, results: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        full = FULL_REF.match(value.strip())
        if full:
            return lookup(full.group(1), results)
        return REF_TOKEN.sub(lambda m: str(lookup(m.group(1), results)), value)
    if isinstance(value, dict):
        return {key: resolve(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, results) for item in value]
    return value


def compact_plan(plan: Plan, limit: int = 6000) -> str:
    text = json.dumps(plan.to_dict(), ensure_ascii=False)
    return text[:limit]


def summarize_value(value: Any, limit: int = 400) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


def normalize_travel_plan(plan: Plan, request: Any) -> List[str]:
    """把模型常见的「往返方向写反 / 城市写错」纠回确定性约束。

    返回变更说明，交给 Trace 展示。只修正方向、城市与缺失日期，不改写
    用户明确给出的日期，因此重规划换日期时仍然可用。
    """

    changes: List[str] = []
    if request is None:
        return changes
    origin = getattr(request, "origin", "")
    destination = getattr(request, "destination", "")
    depart = getattr(request, "depart", "")
    return_date = getattr(request, "return_date", "")
    flights = [step for step in plan.steps if step.tool == "search_flights"]
    for index, step in enumerate(flights):
        args = step.args if isinstance(step.args, dict) else {}
        step.args = args
        goal = str(step.goal or "")
        date = str(args.get("date") or "")
        cur_origin = str(args.get("origin") or "")
        cur_dest = str(args.get("destination") or "")
        leg = ""
        if "返程" in goal or "回程" in goal or "返回" in goal:
            leg = "in"
        elif "去程" in goal or "前往" in goal:
            leg = "out"
        elif date == return_date and return_date:
            leg = "in"
        elif date == depart and depart:
            leg = "out"
        elif cur_origin == destination and cur_dest == origin:
            leg = "in"
        elif cur_origin == origin:
            leg = "out"
        else:
            leg = "out" if index == 0 else "in"
        expected_origin, expected_dest = (
            (destination, origin) if leg == "in" else (origin, destination)
        )
        if cur_origin and cur_dest and (cur_origin != expected_origin or cur_dest != expected_dest):
            changes.append(
                f"{step.id} 航班方向纠正：{cur_origin}→{cur_dest} ⇒ "
                f"{expected_origin}→{expected_dest}"
            )
            args["origin"] = expected_origin
            args["destination"] = expected_dest
        elif not cur_origin:
            args["origin"] = expected_origin
            changes.append(f"{step.id} 补齐出发城市={expected_origin}")
        elif not cur_dest:
            args["destination"] = expected_dest
            changes.append(f"{step.id} 补齐目的城市={expected_dest}")
        if not date:
            args["date"] = return_date if leg == "in" else depart
            if args["date"]:
                changes.append(f"{step.id} 补齐日期={args['date']}")
    for step in plan.steps:
        if step.tool == "search_hotels":
            args = step.args if isinstance(step.args, dict) else {}
            step.args = args
            if destination and str(args.get("city") or "") != destination:
                changes.append(f"{step.id} 酒店城市纠正：{args.get('city')} ⇒ {destination}")
                args["city"] = destination
            if not args.get("checkin") and depart:
                args["checkin"] = depart
                changes.append(f"{step.id} 补齐入住日期={depart}")
            if not args.get("checkout") and return_date:
                args["checkout"] = return_date
                changes.append(f"{step.id} 补齐离店日期={return_date}")
        elif step.tool == "compute_trip_cost":
            args = step.args if isinstance(step.args, dict) else {}
            step.args = args
            if "nights" not in args:
                args["nights"] = getattr(request, "nights", 1)
                changes.append(f"{step.id} 补齐晚数={args['nights']}")
            if "budget" not in args:
                args["budget"] = getattr(request, "budget", None)
        elif step.tool == "draft_itinerary":
            args = step.args if isinstance(step.args, dict) else {}
            step.args = args
            args.setdefault("budget", getattr(request, "budget", None))
            args.setdefault("nights", getattr(request, "nights", 1))
    return changes


def ensure_approval_step(plan: Plan) -> Plan:
    """兜底：旅行类计划如果没有人工确认步骤，自动补一个。"""

    if any(step.tool == "request_approval" for step in plan.steps):
        return plan
    leaves = [s for s in plan.steps if not any(s.id in other.depends_on for other in plan.steps)]
    final = leaves[-1] if leaves else plan.steps[-1]
    used = {s.id for s in plan.steps}
    new_id = "sA"
    counter = 1
    while new_id in used:
        counter += 1
        new_id = f"sA{counter}"
    detail_ref = "$%s.markdown" % final.id
    if final.tool == "draft_itinerary":
        detail_ref = "$%s.summary" % final.id
    plan.steps.append(
        Step(
            id=new_id,
            goal="提交预订审批",
            tool="request_approval",
            args={
                "action": "预订航班与酒店",
                "detail": detail_ref,
                "amount": _amount_ref(plan),
            },
            depends_on=[final.id],
            success="获得人工确认",
            risk=2,
        )
    )
    return plan


def _amount_ref(plan: Plan) -> Optional[str]:
    for step in reversed(plan.steps):
        if step.tool == "compute_trip_cost":
            return f"${step.id}.total"
    return None


def merge_plan(base: Plan, revision: Dict[str, Any]) -> Plan:
    revised = plan_from_dict(revision, strategy=base.strategy, created_by="replanner")
    if not revised.goal:
        revised.goal = base.goal
    if not revised.assumptions:
        revised.assumptions = list(base.assumptions)
    return revised


__all__ = [
    "Step",
    "Plan",
    "PlanError",
    "plan_from_dict",
    "validate_plan",
    "topological_levels",
    "topological_order",
    "lookup",
    "resolve",
    "compact_plan",
    "summarize_value",
    "ensure_approval_step",
    "normalize_travel_plan",
    "merge_plan",
    "refs_in",
    "parse_json_text",
]

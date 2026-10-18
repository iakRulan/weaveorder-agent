"""目标解析：把一句话差旅需求拆成结构化约束。

先用确定性的规则解析（路线、日期、预算），再让模型补充软偏好和澄清问题。
模型永远不能覆盖规则已经确定的硬约束，这样既快又不容易幻觉。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .catalog import CITY_ALIASES, parse_date_loose, route_cities
from .llm import LLM, LLMError
from .policy import policy_context

DEFAULT_DEPART = "2026-10-28"

KNOWN_CITIES = sorted(
    set(
        route_cities()
        + [
            "上海", "深圳", "北京", "广州", "杭州", "成都", "西安", "重庆", "南京",
            "武汉", "苏州", "厦门", "青岛", "天津", "长沙", "郑州", "合肥", "济南",
            "福州", "昆明", "贵阳", "南宁", "海口", "三亚", "沈阳", "大连", "哈尔滨",
            "长春", "石家庄", "太原", "南昌", "兰州", "银川", "西宁", "乌鲁木齐",
            "拉萨", "呼和浩特", "宁波", "无锡", "东莞", "佛山", "珠海",
        ]
    ),
    key=len,
    reverse=True,
)

PREFERENCE_PATTERNS = [
    ("早班机", ("早班", "早上", "上午", "早晨", "早一点")),
    ("午班机", ("午班", "中午", "午后")),
    ("晚班机", ("晚班", "晚上", "傍晚")),
    ("避免红眼", ("不要红眼", "避免红眼", "别红眼", "不坐红眼")),
    ("直飞", ("直飞", "不经停", "直达")),
    ("可退改", ("可退", "退改", "免费取消", "取消政策")),
    ("经济舱", ("经济舱", "二等座")),
    ("公务舱", ("公务舱", "商务舱", "头等舱")),
    ("安静房", ("安静", "高楼层", "无烟")),
    ("近客户地点", ("离客户近", "近客户", "附近", "3公里", "三公里")),
]

PURPOSE_PATTERNS = [
    ("客户拜访", ("客户拜访", "拜访客户", "见客户", "客户")),
    ("商务会议", ("会议", "开会", "峰会", "论坛")),
    ("内部培训", ("培训", "学习", "workshop")),
    ("面试招聘", ("面试", "招聘", "校招")),
    ("团队建设", ("团建", "team building")),
    ("行业调研", ("调研", "考察", "走访")),
]

BUDGET_RE = re.compile(
    r"(?:预算|费用|花费|控制在|不超过|最多|上限)\s*(?:为|是|在|约|大约)?\s*[¥￥]?\s*(\d{3,6})\s*(?:元|块|rmb|人民币)?",
    re.IGNORECASE,
)
BUDGET_ALT_RE = re.compile(r"[¥￥]\s*(\d{3,6})|(\d{3,6})\s*(?:元|块)(?:以内|之内|以下)?")


@dataclass
class TravelRequest:
    origin: str = "上海"
    destination: str = "深圳"
    depart: str = DEFAULT_DEPART
    return_date: str = "2026-10-30"
    budget: int = 3000
    preferences: List[str] = field(default_factory=list)
    purpose: str = "客户拜访"
    party_size: int = 1
    raw_goal: str = ""
    confidence: float = 0.4
    questions: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    source: str = "rule"

    @property
    def nights(self) -> int:
        try:
            delta = (date.fromisoformat(self.return_date) - date.fromisoformat(self.depart)).days
            return max(1, delta)
        except Exception:
            return 1

    @property
    def route(self) -> str:
        return f"{self.origin} → {self.destination}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "depart": self.depart,
            "return_date": self.return_date,
            "nights": self.nights,
            "budget": self.budget,
            "preferences": list(self.preferences),
            "purpose": self.purpose,
            "party_size": self.party_size,
            "confidence": self.confidence,
            "questions": list(self.questions),
            "assumptions": list(self.assumptions),
            "missing": list(self.missing),
            "source": self.source,
        }


def _find_cities(text: str) -> List[str]:
    found: List[Tuple[int, str]] = []
    for city in KNOWN_CITIES:
        for match in re.finditer(re.escape(city), text):
            found.append((match.start(), city))
    for alias, city in CITY_ALIASES.items():
        for match in re.finditer(re.escape(alias), text):
            found.append((match.start(), city))
    found.sort()
    ordered: List[str] = []
    last_end = -1
    for position, city in found:
        if position < last_end:
            continue
        if city not in ordered:
            ordered.append(city)
        last_end = position + len(city)
    return ordered


def _extract_route(text: str) -> Tuple[Optional[str], Optional[str]]:
    route = re.search(
        r"(?:从)?([\u4e00-\u9fa5]{2,8}?)\s*(?:到|至|去|飞往|飞|→|->|-)\s*"
        r"([\u4e00-\u9fa5]{2,8}?)(?=[，,。；;\s的]|$)",
        text,
    )
    if route:
        origin = route.group(1).removeprefix("从").strip()
        destination = route.group(2).strip()
        origin = CITY_ALIASES.get(origin, origin)
        destination = CITY_ALIASES.get(destination, destination)
        # 过滤掉明显不是城市的动词短语。
        if origin.endswith(("帮我", "安排", "计划")):
            origin = origin[-2:]
        if origin and destination and origin != destination:
            return origin, destination
    cities = _find_cities(text)
    if len(cities) >= 2:
        return cities[0], cities[1]
    if len(cities) == 1:
        return None, cities[0]
    return None, None


def _extract_dates(text: str) -> List[date]:
    dates: List[date] = []
    for pattern in (
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?",
        r"(?<![\d-])\d{1,2}月\d{1,2}日",
        r"(?<![\d-])\d{1,2}-\d{1,2}(?![\d-])",
        r"(?<![\d/])\d{1,2}/\d{1,2}(?![\d/])",
    ):
        for match in re.finditer(pattern, text):
            parsed = parse_date_loose(match.group(0), default_year=2026)
            if parsed and parsed not in dates:
                dates.append(parsed)
    dates.sort()
    return dates


def _extract_budget(text: str) -> Optional[int]:
    match = BUDGET_RE.search(text)
    if match:
        return int(match.group(1))
    for match in BUDGET_ALT_RE.finditer(text):
        value = match.group(1) or match.group(2)
        if value:
            return int(value)
    return None


def _extract_preferences(text: str) -> List[str]:
    prefs: List[str] = []
    for label, keywords in PREFERENCE_PATTERNS:
        if any(keyword in text for keyword in keywords) and label not in prefs:
            prefs.append(label)
    return prefs


def _extract_purpose(text: str) -> str:
    for label, keywords in PURPOSE_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return label
    return "商务出行"


def rule_intake(text: str) -> TravelRequest:
    origin, destination = _extract_route(text)
    dates = _extract_dates(text)
    budget = _extract_budget(text)
    preferences = _extract_preferences(text)
    purpose = _extract_purpose(text)
    missing: List[str] = []
    assumptions: List[str] = []

    if not origin or not destination:
        origin = origin or "上海"
        destination = destination or "深圳"
        missing.append("route")
        assumptions.append(f"未识别完整路线，默认按 {origin} → {destination} 规划")
    if not dates:
        depart = date.fromisoformat(DEFAULT_DEPART)
        ret = depart + timedelta(days=2)
        missing.append("dates")
        assumptions.append(f"未识别日期，默认 {depart.isoformat()} 出发、{ret.isoformat()} 返回")
    else:
        depart = dates[0]
        if len(dates) >= 2:
            ret = dates[1]
        else:
            ret = depart + timedelta(days=2)
            match = re.search(r"([一两二三四五六七八九十])\s*天", text)
            if match:
                span = "一二三四五六七八九十".index(match.group(1)) + 1
                ret = depart + timedelta(days=max(1, span))
                assumptions.append(f"按「{match.group(0)}」推断返程日期 {ret.isoformat()}")
            else:
                missing.append("return_date")
                assumptions.append(f"未识别返程日期，默认 {ret.isoformat()} 返回（2 晚）")
    if ret <= depart:
        ret = depart + timedelta(days=1)
        assumptions.append("返程日期早于去程，自动修正为次日返回")
    if budget is None:
        budget = 3000
        missing.append("budget")
        assumptions.append("未给出预算，默认 3000 元")
    if not preferences:
        preferences = ["早班机"]
        assumptions.append("未给出航班偏好，默认偏好早班机")

    return TravelRequest(
        origin=origin,
        destination=destination,
        depart=depart.isoformat(),
        return_date=ret.isoformat(),
        budget=budget,
        preferences=preferences,
        purpose=purpose,
        raw_goal=text,
        confidence=round(min(0.98, max(0.35, 0.55 + 0.15 * (4 - len(missing)))), 2),
        assumptions=assumptions,
        missing=missing,
        source="rule",
    )


def _merge(ctx: TravelRequest, data: Dict[str, Any]) -> TravelRequest:
    """模型只能补空位，不能推翻规则硬约束。"""

    assumptions = list(ctx.assumptions)
    missing = list(set(ctx.missing))

    if "route" in missing and data.get("origin") and data.get("destination"):
        ctx.origin = str(data["origin"]).strip()
        ctx.destination = str(data["destination"]).strip()
        missing.remove("route")
    if "dates" in missing or "return_date" in missing:
        depart = parse_date_loose(str(data.get("depart", "")), 2026)
        ret = parse_date_loose(str(data.get("return_date", "")), 2026)
        if depart:
            ctx.depart = depart.isoformat()
            missing = [m for m in missing if m != "dates"]
        if ret:
            ctx.return_date = ret.isoformat()
            if ctx.return_date <= ctx.depart:
                ctx.return_date = (date.fromisoformat(ctx.depart) + timedelta(days=1)).isoformat()
            missing = [m for m in missing if m not in ("dates", "return_date")]
    if "budget" in missing and data.get("budget"):
        try:
            value = int(float(data["budget"]))
            if 200 <= value <= 200_000:
                ctx.budget = value
                missing.remove("budget")
        except Exception:
            pass

    prefs = data.get("preferences")
    if isinstance(prefs, list):
        for item in prefs:
            label = str(item).strip()
            if label and label not in ctx.preferences and len(label) <= 12:
                # 去掉与已识别偏好重复的泛化表述
                if not any(label in existing or existing in label for existing in ctx.preferences):
                    ctx.preferences.append(label)
    purpose = str(data.get("purpose") or "").strip()
    if purpose and len(purpose) <= 12 and ctx.purpose in ("商务出行", ""):
        ctx.purpose = purpose

    questions = data.get("questions")
    if isinstance(questions, list):
        for item in questions[:4]:
            question = str(item).strip()
            if question and question not in ctx.questions:
                ctx.questions.append(question)
    extra_assumptions = data.get("assumptions")
    if isinstance(extra_assumptions, list):
        for item in extra_assumptions[:4]:
            line = str(item).strip()
            if line and line not in assumptions:
                assumptions.append(line)
    try:
        confidence = float(data.get("confidence", ctx.confidence))
        ctx.confidence = round(max(0.3, min(0.99, confidence)), 2)
    except Exception:
        pass
    ctx.assumptions = assumptions
    ctx.missing = missing
    ctx.source = "llm+rule"
    return ctx


INTAKE_SYSTEM = """你是「织序 WeaveOrder」的需求解析器，服务于企业差旅场景。
把用户的一句话目标解析为结构化约束，只输出 JSON：
{"origin":"城市","destination":"城市","depart":"YYYY-MM-DD","return_date":"YYYY-MM-DD",
 "budget":3000,"preferences":["早班机"],"purpose":"客户拜访","party_size":1,
 "confidence":0.0-1.0,"assumptions":["..."],"questions":["需要用户确认的问题"]}
规则：
1. 只抽取用户明确说出的信息，不要编造日期、预算、城市。
2. 用户没说预算就填 null；没说日期就填 null。
3. questions 只问真正影响方案的关键缺口，最多 3 条，每条一句话。
4. 城市使用中文标准名；日期统一 YYYY-MM-DD。
5. 偏好可以是：早班机/午班机/晚班机/避免红眼/直飞/可退改/经济舱/安静房/近客户地点。
"""


def llm_intake(llm: Optional[LLM], text: str, base: TravelRequest) -> TravelRequest:
    if llm is None or not llm.available or llm.cfg.mock:
        return base
    user = (
        f"用户目标：{text}\n"
        f"规则已解析：{json.dumps(base.to_dict(), ensure_ascii=False)}\n"
        "请在规则解析的基础上补充软偏好、目的和澄清问题，只输出 JSON。"
    )
    try:
        data = llm.chat_json(
            [
                {"role": "system", "content": INTAKE_SYSTEM},
                {"role": "user", "content": user},
            ],
            role="intake",
            temperature=0,
            max_tokens=800,
            thinking=False,
        )
    except LLMError:
        return base
    return _merge(base, data)


def _default_questions(ctx: TravelRequest) -> List[str]:
    questions: List[str] = []
    if "route" in ctx.missing:
        questions.append("出发城市和目的地城市分别是什么？")
    if "dates" in ctx.missing:
        questions.append("预计哪一天出发、哪一天返回？")
    if "budget" in ctx.missing:
        questions.append("这次差旅的预算上限是多少？")
    if not questions and ctx.budget < 2500:
        questions.append(f"预算 {ctx.budget} 元偏低，是否接受红眼航班或更远的酒店？")
    if ctx.nights > 3:
        questions.append(f"连续住宿 {ctx.nights} 晚，是否需要说明原因？")
    return questions[:3]


def build_policy_hint() -> str:
    return policy_context(top_k=4)


__all__ = [
    "TravelRequest",
    "rule_intake",
    "llm_intake",
    "DEFAULT_DEPART",
    "KNOWN_CITIES",
    "build_policy_hint",
    "_default_questions",
]

"""差旅政策检索与合规校验。

政策既是「知识库」也是「规则引擎」：
* ``search`` 给 Planner / 人看的条款与引用；
* ``compliance`` 给 Verifier 用的确定性判断，不依赖模型。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DATA_DIR

SECTION_KEYWORDS = {
    "机票": ("机票", "航班", "飞行", "舱位", "经济舱", "直飞", "红眼", "航空公司"),
    "酒店": ("酒店", "住宿", "房型", "门店", "公里", "晚"),
    "交通与补贴": ("交通", "打车", "补贴", "地铁", "餐补"),
    "审批": ("审批", "预算", "超支", "确认", "下单", "付款", "例外"),
}

STOPWORDS = {"的", "了", "和", "与", "或", "在", "是", "为", "按", "及", "等", "需", "应"}

# 机器可读规则；与 data/policy.md 的条款编号一一对应。
RULES = {
    "flight_max": 1800,
    "hotel_max_default": 600,
    "hotel_max_south": 550,
    "hotel_distance_max": 3.0,
    "nights_without_note": 3,
    "approval_threshold": 3000,
    "local_transport_max": 200,
    "red_eye_start": 21,
    "red_eye_end": 7,
}
SOUTH_CITIES = {"深圳", "广州", "珠海", "东莞", "佛山", "海口", "三亚"}


@dataclass
class Clause:
    id: str
    section: str
    title: str
    text: str
    order: int
    score: float = 0.0
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "title": self.title,
            "text": self.text,
            "score": round(self.score, 3),
            "tags": self.tags,
            "citation": f"[{self.id}] {self.title}：{self.text}",
        }


@lru_cache(maxsize=1)
def load_clauses() -> Tuple[Clause, ...]:
    path = DATA_DIR / "policy.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    clauses: List[Clause] = []
    section = "总则"
    subsection = ""
    counters: Dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            subsection = ""
            continue
        if line.startswith("### "):
            subsection = line[4:].strip()
            continue
        if line.startswith("# "):
            continue
        if line.startswith("- "):
            body = line[2:].strip()
            if not body:
                continue
            prefix = {
                "机票": "FLT",
                "酒店": "HTL",
                "交通与补贴": "TRN",
                "审批": "APV",
                "总则": "GEN",
            }.get(section, "GEN")
            counters[prefix] = counters.get(prefix, 0) + 1
            clause_id = f"{prefix}-{counters[prefix]:02d}"
            tags = [section] + ([subsection] if subsection else [])
            clauses.append(
                Clause(
                    id=clause_id,
                    section=section,
                    title=subsection or section,
                    text=body,
                    order=len(clauses),
                    tags=tags,
                )
            )
    return tuple(clauses)


def _grams(text: str) -> List[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", " ", text.lower())
    chunks = [chunk for chunk in cleaned.split() if chunk]
    grams: List[str] = []
    for chunk in chunks:
        if re.fullmatch(r"[a-z0-9]+", chunk):
            grams.append(chunk)
            continue
        if len(chunk) == 1:
            grams.append(chunk)
            continue
        grams.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return [gram for gram in grams if gram not in STOPWORDS]


def _score(query: str, clause: Clause) -> float:
    query_grams = _grams(query)
    if not query_grams:
        return 0.0
    text = clause.title + " " + clause.text + " " + " ".join(clause.tags)
    text_grams = set(_grams(text))
    overlap = [gram for gram in query_grams if gram in text_grams]
    if not overlap:
        return 0.0
    score = len(overlap) / math.sqrt(len(query_grams) + 1)
    for section, keywords in SECTION_KEYWORDS.items():
        if section in clause.section and any(key in query for key in keywords):
            score += 1.2
    if any(key in query for key in ("预算", "审批", "超支")) and clause.id.startswith("APV"):
        score += 0.8
    if any(key in query for key in ("价格", "上限", "标准")) and (
        "不超过" in clause.text or "上限" in clause.text
    ):
        score += 0.3
    return round(score, 4)


def search(
    query: str = "差旅 机票 酒店 标准 审批",
    top_k: int = 4,
    category: Optional[str] = None,
) -> dict:
    clauses = list(load_clauses())
    if category:
        clauses = [c for c in clauses if category in c.section or category in c.title]
    scored = []
    for clause in clauses:
        value = _score(query, clause)
        if value > 0:
            scored.append(Clause(**{**clause.__dict__, "score": value}))
    scored.sort(key=lambda c: (-c.score, c.order))
    if not scored:
        scored = clauses[:top_k]
    # 同一 section 最多取 2 条，避免 4 条结果全来自「审批」而看不到机票/酒店标准。
    selected_objs: List[Clause] = []
    per_section: Dict[str, int] = {}
    for clause in scored:
        if per_section.get(clause.section, 0) >= 2:
            continue
        selected_objs.append(clause)
        per_section[clause.section] = per_section.get(clause.section, 0) + 1
        if len(selected_objs) >= top_k:
            break
    if len(selected_objs) < top_k:
        for clause in scored:
            if clause in selected_objs:
                continue
            selected_objs.append(clause)
            if len(selected_objs) >= top_k:
                break
    selected = [c.to_dict() for c in selected_objs]
    return {
        "query": query,
        "category": category,
        "clauses": selected,
        "count": len(selected),
        "total_clauses": len(load_clauses()),
        "rules": dict(RULES),
        "source": "data/policy.md",
    }


def policy_limits() -> Dict[str, Any]:
    return dict(RULES)


def hotel_limit(city: str) -> int:
    return RULES["hotel_max_south"] if city in SOUTH_CITIES else RULES["hotel_max_default"]


def _citation(clause_id: str) -> Optional[str]:
    for clause in load_clauses():
        if clause.id == clause_id:
            return f"[{clause.id}] {clause.text}"
    return None


def compliance(
    *,
    outbound: Optional[dict] = None,
    inbound: Optional[dict] = None,
    hotel: Optional[dict] = None,
    total_cost: Optional[float] = None,
    budget: Optional[float] = None,
    city: str = "",
    nights: int = 1,
) -> dict:
    """确定性合规检查。返回 blockers（必须处理）与 warnings（可接受但需标注）。"""

    violations: List[dict] = []
    warnings: List[dict] = []

    def add(kind: str, rule: str, message: str, citation: Optional[str], severity: str = "warning"):
        item = {
            "rule": rule,
            "message": message,
            "citation": citation,
            "severity": severity,
            "kind": kind,
        }
        (violations if severity == "blocker" else warnings).append(item)

    flights = [f for f in (outbound, inbound) if isinstance(f, dict) and f]
    for leg, flight in zip(("去程", "返程"), flights):
        price = float(flight.get("price", 0) or 0)
        if price > RULES["flight_max"]:
            add(
                "机票",
                "FLT-price",
                f"{leg}航班 {flight.get('flight_no', '')} ¥{price:.0f} 超过单程 {RULES['flight_max']} 元上限",
                _citation("FLT-03"),
            )
        depart = str(flight.get("depart", "12:00"))
        try:
            hour = int(depart.split(":")[0])
        except Exception:
            hour = 12
        if hour < RULES["red_eye_end"] or hour >= RULES["red_eye_start"]:
            add(
                "航班时段",
                "FLT-red-eye",
                f"{leg}航班 {flight.get('flight_no', '')} {depart} 属于红眼/深夜时段",
                _citation("FLT-05"),
            )
        if flight.get("stops"):
            add(
                "直飞",
                "FLT-direct",
                f"{leg}航班 {flight.get('flight_no', '')} 为中转航班，需说明原因",
                _citation("FLT-04"),
            )
        if flight.get("refundable") is False:
            add(
                "退改",
                "FLT-refundable",
                f"{leg}航班 {flight.get('flight_no', '')} 为不可退改产品",
                _citation("FLT-06"),
            )

    if isinstance(hotel, dict) and hotel:
        price = float(hotel.get("price", 0) or 0)
        limit = hotel_limit(city or str(hotel.get("city", "")))
        if price > limit:
            add(
                "酒店",
                "HTL-price",
                f"酒店 {hotel.get('name', '')} ¥{price:.0f}/晚 超过 {city or hotel.get('city', '')} 的 {limit} 元标准",
                _citation("HTL-01"),
            )
        distance = float(hotel.get("distance_km", 0) or 0)
        if distance > RULES["hotel_distance_max"]:
            add(
                "酒店位置",
                "HTL-distance",
                f"酒店 {hotel.get('name', '')} 距客户地点 {distance} km，超过 {RULES['hotel_distance_max']} km 建议范围",
                _citation("HTL-02"),
            )
        if hotel.get("refundable") is False:
            add(
                "退改",
                "HTL-refundable",
                f"酒店 {hotel.get('name', '')} 默认房型不可免费取消",
                _citation("HTL-04"),
            )
    if nights > RULES["nights_without_note"]:
        add(
            "住宿时长",
            "HTL-nights",
            f"连续住宿 {nights} 晚，超过 {RULES['nights_without_note']} 晚需说明原因",
            _citation("HTL-03"),
        )

    if total_cost is not None and budget is not None:
        total = float(total_cost)
        cap = float(budget)
        if total > cap:
            add(
                "预算",
                "BUD-total",
                f"预估总费用 ¥{total:.0f} 超过预算 ¥{cap:.0f}（超出 ¥{total - cap:.0f}）",
                _citation("APV-02"),
                severity="blocker",
            )
        if total > RULES["approval_threshold"]:
            add(
                "审批",
                "APV-threshold",
                f"总费用 ¥{total:.0f} 超过 {RULES['approval_threshold']} 元，需部门负责人审批",
                _citation("APV-01"),
            )

    score = max(0, 100 - 18 * len(violations) - 6 * len(warnings))
    return {
        "ok": not violations,
        "score": score,
        "violations": violations,
        "warnings": warnings,
        "blockers": len(violations),
        "warnings_count": len(warnings),
        "limits": dict(RULES),
        "source": "data/policy.md",
    }


def policy_context(top_k: int = 4) -> str:
    result = search("差旅 机票 酒店 预算 审批", top_k=top_k)
    return "\n".join(f"[{c['id']}] {c['text']}" for c in result["clauses"])


__all__ = [
    "RULES",
    "SOUTH_CITIES",
    "load_clauses",
    "search",
    "policy_limits",
    "hotel_limit",
    "compliance",
    "policy_context",
]

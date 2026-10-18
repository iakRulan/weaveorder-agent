"""确定性 mocks 数据源：航班、酒店、政策条款。

比赛环境不依赖外部票务 API，但也不希望模型「编造」价格，所以数据生成
必须完全确定：同一条路线 + 日期永远返回同一批选项，评测可复现。
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DATA_DIR

# ---------------------------------------------------------------------------
# 航班
# ---------------------------------------------------------------------------

# (航司, 航班号, 起飞, 飞行分钟, 基准价, 经停, 可退改)
RouteTable = Dict[Tuple[str, str], List[Tuple[str, str, str, int, int, int, bool]]]

ROUTES: RouteTable = {
    ("上海", "深圳"): [
        ("东方航空", "MU5331", "07:30", 155, 980, 0, True),
        ("南方航空", "CZ3558", "08:20", 155, 1080, 0, True),
        ("东方航空", "MU5351", "12:10", 155, 880, 0, True),
        ("深圳航空", "ZH9520", "18:40", 155, 1250, 0, False),
        ("海南航空", "HU7605", "21:30", 155, 780, 0, True),
    ],
    ("深圳", "上海"): [
        ("东方航空", "MU5332", "07:20", 150, 930, 0, True),
        ("南方航空", "CZ3557", "10:40", 150, 820, 0, True),
        ("深圳航空", "ZH9519", "15:20", 150, 780, 0, True),
        ("东方航空", "MU5352", "19:50", 150, 720, 0, False),
        ("海南航空", "HU7606", "22:10", 150, 660, 0, True),
    ],
    ("北京", "深圳"): [
        ("中国国航", "CA1301", "07:15", 205, 1180, 0, True),
        ("南方航空", "CZ3156", "09:10", 205, 1290, 0, True),
        ("深圳航空", "ZH9102", "12:40", 205, 1020, 0, True),
        ("中国国航", "CA1315", "19:30", 205, 1450, 0, False),
        ("海南航空", "HU7701", "22:00", 205, 890, 0, True),
    ],
    ("深圳", "北京"): [
        ("中国国航", "CA1302", "08:00", 200, 1120, 0, True),
        ("南方航空", "CZ3155", "11:30", 200, 980, 0, True),
        ("深圳航空", "ZH9101", "16:00", 200, 890, 0, True),
        ("中国国航", "CA1316", "20:10", 200, 820, 0, False),
        ("海南航空", "HU7702", "22:40", 200, 740, 0, True),
    ],
    ("上海", "广州"): [
        ("吉祥航空", "HO1151", "08:00", 130, 1050, 0, True),
        ("南方航空", "CZ3588", "10:30", 130, 1180, 0, True),
        ("东方航空", "MU5301", "13:00", 130, 880, 0, True),
        ("深圳航空", "ZH9608", "18:30", 130, 1320, 0, False),
        ("九元航空", "AQ1002", "21:40", 130, 760, 0, True),
    ],
    ("广州", "上海"): [
        ("东方航空", "MU5302", "07:30", 125, 980, 0, True),
        ("南方航空", "CZ3587", "11:00", 125, 890, 0, True),
        ("吉祥航空", "HO1152", "15:30", 125, 820, 0, True),
        ("深圳航空", "ZH9607", "19:00", 125, 750, 0, False),
        ("九元航空", "AQ1001", "22:20", 125, 680, 0, True),
    ],
    ("北京", "上海"): [
        ("中国国航", "CA1519", "07:00", 135, 1250, 0, True),
        ("东方航空", "MU5102", "09:30", 135, 1120, 0, True),
        ("中国国航", "CA1521", "13:20", 135, 980, 0, True),
        ("吉祥航空", "HO1252", "18:50", 135, 1380, 0, False),
        ("中国联合航空", "KN5977", "21:20", 135, 860, 0, True),
    ],
    ("上海", "北京"): [
        ("东方航空", "MU5101", "07:10", 140, 1180, 0, True),
        ("中国国航", "CA1520", "09:40", 140, 1020, 0, True),
        ("吉祥航空", "HO1251", "13:00", 140, 930, 0, True),
        ("中国国航", "CA1522", "19:10", 140, 1290, 0, False),
        ("中国联合航空", "KN5978", "21:50", 140, 820, 0, True),
    ],
    ("广州", "深圳"): [
        ("南方航空", "CZ3901", "08:10", 70, 520, 0, True),
        ("深圳航空", "ZH9002", "12:20", 70, 460, 0, True),
        ("南方航空", "CZ3903", "18:30", 70, 610, 0, False),
    ],
    ("深圳", "广州"): [
        ("南方航空", "CZ3902", "08:30", 70, 510, 0, True),
        ("深圳航空", "ZH9001", "12:40", 70, 450, 0, True),
        ("南方航空", "CZ3904", "19:10", 70, 600, 0, False),
    ],
    ("杭州", "深圳"): [
        ("长龙航空", "GJ8925", "07:40", 140, 860, 0, True),
        ("深圳航空", "ZH9882", "11:20", 140, 780, 0, True),
        ("东方航空", "MU5215", "17:30", 140, 990, 0, True),
    ],
    ("深圳", "杭州"): [
        ("长龙航空", "GJ8926", "08:20", 140, 840, 0, True),
        ("深圳航空", "ZH9881", "12:10", 140, 760, 0, True),
        ("东方航空", "MU5216", "18:40", 140, 960, 0, True),
    ],
    ("成都", "深圳"): [
        ("四川航空", "3U8731", "07:50", 155, 920, 0, True),
        ("深圳航空", "ZH9202", "12:30", 155, 840, 0, True),
        ("中国国航", "CA4315", "19:00", 155, 1080, 0, True),
    ],
    ("深圳", "成都"): [
        ("四川航空", "3U8732", "08:10", 155, 900, 0, True),
        ("深圳航空", "ZH9201", "13:10", 155, 820, 0, True),
        ("中国国航", "CA4316", "19:40", 155, 1060, 0, True),
    ],
}

CABINS = {"economy": "经济舱", "business": "公务舱", "first": "头等舱"}

SUPPORTED_ORIGINS = sorted({pair[0] for pair in ROUTES})


def _stable_seed(*parts: Any) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def _minutes(hhmm: str) -> int:
    hour, minute = str(hhmm).split(":")
    return int(hour) * 60 + int(minute)


def _hhmm(total: int) -> Tuple[str, int]:
    day_offset = total // (24 * 60)
    total = total % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}", day_offset


def _price(base: int, origin: str, destination: str, day: date, depart: str) -> int:
    rng = random.Random(_stable_seed(origin, destination, day.isoformat(), depart))
    weekday = day.weekday()  # 0=Mon
    modifier = 0
    if weekday in (4, 6):  # 周五、周日返程高峰
        modifier += 90
    elif weekday == 5:
        modifier += 40
    elif weekday == 1:
        modifier -= 30
    jitter = rng.choice([-60, -40, -20, 0, 20, 40, 60])
    price = base + modifier + jitter
    # 临近节假日（10-01..10-07、01-20..02-10）上浮，让演示更真实。
    if (day.month == 10 and 1 <= day.day <= 7) or (day.month == 1 and day.day >= 20):
        price = int(price * 1.35)
    return max(300, int(round(price / 10.0) * 10))


def _on_time_rate(flight_no: str) -> float:
    rng = random.Random(_stable_seed("otp", flight_no))
    return round(0.72 + rng.random() * 0.22, 2)


def generate_flights(origin: str, destination: str, day: str) -> List[dict]:
    origin = _norm_city(origin)
    destination = _norm_city(destination)
    parsed = _parse_date(day)
    rows: List[dict] = []
    templates = ROUTES.get((origin, destination))
    if not templates:
        return []
    for airline, flight_no, depart, duration, base, stops, refundable in templates:
        price = _price(base, origin, destination, parsed, depart)
        arrive, offset = _hhmm(_minutes(depart) + duration)
        rows.append(
            {
                "flight_no": flight_no,
                "airline": airline,
                "origin": origin,
                "destination": destination,
                "date": parsed.isoformat(),
                "depart": depart,
                "arrive": arrive,
                "arrive_next_day": bool(offset),
                "duration_min": duration,
                "price": price,
                "cabin": CABINS["economy"],
                "stops": stops,
                "refundable": refundable,
                "on_time_rate": _on_time_rate(flight_no),
                "source": "mock-catalog",
            }
        )
    return rows


def flight_score(flight: dict) -> float:
    """综合价格、出发时段、经停、退改、准点率的排序分（越低越好）。"""

    depart = flight.get("depart", "12:00")
    hour = _minutes(depart) / 60.0
    score = float(flight.get("price", 0))
    if 7 <= hour <= 10:
        score -= 80  # 早班有明确偏好
    elif 10 < hour <= 14:
        score += 120
    elif 14 < hour <= 19:
        score += 160
    elif 19 < hour < 21:
        score += 260
    else:
        score += 620  # 红眼/深夜
    if flight.get("stops"):
        score += 420
    if not flight.get("refundable", True):
        score += 110
    score -= float(flight.get("on_time_rate", 0.8)) * 100
    return round(score, 2)


def search_flights(
    origin: str,
    destination: str,
    date: str,
    max_price: Optional[int] = None,
    depart_after: Optional[str] = None,
    depart_before: Optional[str] = None,
    direct_only: bool = False,
    cabin: str = "economy",
    limit: int = 12,
) -> dict:
    """搜索单程航班。只返回数据源真实覆盖的路线，绝不编造未覆盖城市。"""

    origin = _norm_city(origin)
    destination = _norm_city(destination)
    supported = (origin, destination) in ROUTES
    rows = generate_flights(origin, destination, date) if supported else []
    total = len(rows)
    if max_price is not None:
        cap = float(max_price)
        rows = [row for row in rows if row["price"] <= cap]
    if depart_after:
        after = _minutes(depart_after)
        rows = [row for row in rows if _minutes(row["depart"]) >= after]
    if depart_before:
        before = _minutes(depart_before)
        rows = [row for row in rows if _minutes(row["depart"]) <= before]
    if direct_only:
        rows = [row for row in rows if not row.get("stops")]
    if cabin and cabin != "economy":
        rows = []  # mock 数据源只有经济舱
        note = f"数据源未覆盖 {cabin} 舱位"
    else:
        note = ""

    rows = sorted(rows, key=lambda row: (row["price"], row["depart"]))[:limit]
    best = min(rows, key=flight_score) if rows else None
    cheapest = rows[0] if rows else None
    return {
        "origin": origin,
        "destination": destination,
        "date": date,
        "options": rows,
        "count": len(rows),
        "total_before_filter": total,
        "cheapest": cheapest,
        "best": best,
        "coverage": {
            "supported": supported,
            "cities": SUPPORTED_ORIGINS,
            "routes": len(ROUTES),
        },
        "filters": {
            "max_price": max_price,
            "depart_after": depart_after,
            "depart_before": depart_before,
            "direct_only": direct_only,
            "cabin": cabin,
        },
        "source": "mock-catalog",
        "notes": note
        or ("mock 数据源，仅覆盖已登记城市对；价格按日期确定性生成" if supported else ""),
    }


# ---------------------------------------------------------------------------
# 酒店
# ---------------------------------------------------------------------------

HOTELS: Dict[str, List[dict]] = {
    "深圳": [
        {"name": "深圳南山智选假日酒店", "star": 4, "price": 520, "distance_km": 1.2, "address": "南山区科技园", "refundable": True, "breakfast": True},
        {"name": "深圳福田亚朵酒店", "star": 4, "price": 480, "distance_km": 2.8, "address": "福田区车公庙", "refundable": True, "breakfast": True},
        {"name": "深圳湾万怡酒店", "star": 5, "price": 680, "distance_km": 0.8, "address": "南山区深圳湾", "refundable": True, "breakfast": True},
        {"name": "深圳罗湖快捷酒店", "star": 3, "price": 320, "distance_km": 6.5, "address": "罗湖区人民南路", "refundable": True, "breakfast": False},
        {"name": "深圳前海希尔顿欢朋", "star": 4, "price": 560, "distance_km": 3.4, "address": "南山区前海", "refundable": False, "breakfast": True},
        {"name": "深圳龙华全季酒店", "star": 3, "price": 380, "distance_km": 9.2, "address": "龙华区民治", "refundable": True, "breakfast": True},
    ],
    "广州": [
        {"name": "广州天河体育中心亚朵", "star": 4, "price": 460, "distance_km": 2.1, "address": "天河区体育西路", "refundable": True, "breakfast": True},
        {"name": "广州珠江新城木莲庄", "star": 5, "price": 680, "distance_km": 0.9, "address": "天河区珠江新城", "refundable": True, "breakfast": True},
        {"name": "广州北京路维也纳", "star": 3, "price": 380, "distance_km": 5.2, "address": "越秀区北京路", "refundable": True, "breakfast": False},
        {"name": "广州琶洲保利洲际", "star": 5, "price": 760, "distance_km": 3.6, "address": "海珠区琶洲", "refundable": False, "breakfast": True},
    ],
    "北京": [
        {"name": "北京国贸建国饭店", "star": 4, "price": 580, "distance_km": 1.4, "address": "朝阳区建国门外", "refundable": True, "breakfast": True},
        {"name": "北京中关村智选假日", "star": 4, "price": 500, "distance_km": 2.6, "address": "海淀区中关村", "refundable": True, "breakfast": True},
        {"name": "北京西单美爵酒店", "star": 5, "price": 720, "distance_km": 0.7, "address": "西城区西单", "refundable": True, "breakfast": True},
        {"name": "北京南站如家精选", "star": 3, "price": 340, "distance_km": 7.4, "address": "丰台区北京南站", "refundable": False, "breakfast": False},
    ],
    "上海": [
        {"name": "上海静安亚朵酒店", "star": 4, "price": 560, "distance_km": 1.6, "address": "静安区南京西路", "refundable": True, "breakfast": True},
        {"name": "上海陆家嘴智选假日", "star": 4, "price": 620, "distance_km": 0.9, "address": "浦东新区陆家嘴", "refundable": True, "breakfast": True},
        {"name": "上海虹桥全季酒店", "star": 3, "price": 380, "distance_km": 5.8, "address": "闵行区虹桥", "refundable": True, "breakfast": False},
        {"name": "上海外滩华尔道夫", "star": 5, "price": 980, "distance_km": 0.6, "address": "黄浦区外滩", "refundable": False, "breakfast": True},
    ],
    "杭州": [
        {"name": "杭州西湖亚朵酒店", "star": 4, "price": 520, "distance_km": 1.8, "address": "西湖区曙光路", "refundable": True, "breakfast": True},
        {"name": "杭州钱江新城全季", "star": 3, "price": 380, "distance_km": 4.6, "address": "上城区钱江新城", "refundable": True, "breakfast": False},
    ],
    "成都": [
        {"name": "成都天府广场亚朵", "star": 4, "price": 480, "distance_km": 1.5, "address": "青羊区天府广场", "refundable": True, "breakfast": True},
        {"name": "成都高新区智选假日", "star": 4, "price": 420, "distance_km": 3.2, "address": "高新区天府大道", "refundable": True, "breakfast": True},
    ],
}


def hotel_score(hotel: dict) -> float:
    score = float(hotel.get("price", 0)) + float(hotel.get("distance_km", 0)) * 100
    if hotel.get("star", 3) < 3:
        score += 260
    if not hotel.get("refundable", True):
        score += 150
    if hotel.get("breakfast"):
        score -= 40
    return round(score, 2)


def search_hotels(
    city: str,
    checkin: str,
    checkout: str,
    max_price: Optional[int] = None,
    min_star: Optional[int] = None,
    max_distance_km: Optional[float] = None,
    limit: int = 10,
) -> dict:
    city = _norm_city(city)
    check_in = _parse_date(checkin)
    check_out = _parse_date(checkout)
    nights = max(1, (check_out - check_in).days)
    rows = [dict(item, city=city) for item in HOTELS.get(city, [])]
    if max_price is not None:
        cap = float(max_price)
        rows = [row for row in rows if row["price"] <= cap]
    if min_star is not None:
        rows = [row for row in rows if int(row.get("star", 0)) >= int(min_star)]
    if max_distance_km is not None:
        rows = [row for row in rows if float(row.get("distance_km", 99)) <= float(max_distance_km)]
    rows = sorted(rows, key=lambda row: (row["price"], row["distance_km"]))[:limit]
    for row in rows:
        row["nights"] = nights
        row["checkin"] = check_in.isoformat()
        row["checkout"] = check_out.isoformat()
        row["total"] = row["price"] * nights
    best = min(rows, key=hotel_score) if rows else None
    return {
        "city": city,
        "checkin": check_in.isoformat(),
        "checkout": check_out.isoformat(),
        "nights": nights,
        "options": rows,
        "count": len(rows),
        "cheapest": rows[0] if rows else None,
        "best": best,
        "coverage": {"supported": city in HOTELS, "cities": sorted(HOTELS)},
        "source": "mock-catalog",
        "notes": "mock 数据源，价格为每晚含税前参考价；已按距离与星级给出 best",
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

CITY_ALIASES = {
    "沪": "上海",
    "申": "上海",
    "鹏城": "深圳",
    "羊城": "广州",
    "穗": "广州",
    "帝都": "北京",
    "首都": "北京",
    "蓉城": "成都",
    "蓉": "成都",
    "杭城": "杭州",
}


def _norm_city(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[省市]$", "", text)
    return CITY_ALIASES.get(text, text)


def supported_routes() -> List[dict]:
    return [
        {"origin": origin, "destination": destination}
        for origin, destination in sorted(ROUTES)
    ]


def route_cities() -> List[str]:
    cities = set()
    for origin, destination in ROUTES:
        cities.update((origin, destination))
    return sorted(cities)


def nights_between(depart: str, ret: str) -> int:
    return max(1, (_parse_date(ret) - _parse_date(depart)).days)


def parse_date_loose(text: str, default_year: int = 2026) -> Optional[date]:
    text = str(text or "").strip()
    patterns = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", True),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", True),
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日", True),
        (r"(\d{1,2})月(\d{1,2})日", False),
        (r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])", False),
        (r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])", False),
    ]
    for pattern, has_year in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if has_year:
                year, month, day = (int(x) for x in match.groups())
            else:
                year = default_year
                month, day = (int(x) for x in match.groups())
            return date(year, month, day)
        except ValueError:
            continue
    return None


def date_window(depart: date, span: int = 3) -> List[str]:
    return [(depart + timedelta(days=offset)).isoformat() for offset in range(span)]


__all__ = [
    "ROUTES",
    "HOTELS",
    "CABINS",
    "search_flights",
    "search_hotels",
    "flight_score",
    "hotel_score",
    "supported_routes",
    "route_cities",
    "nights_between",
    "parse_date_loose",
    "date_window",
]

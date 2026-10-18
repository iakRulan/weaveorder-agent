"""长期记忆：跨运行的偏好画像 + 历史行程（episodic memory）。

* 偏好是**可解释**的：每条都带 evidence（用户原话）、confidence、更新时间；
* 历史只保留摘要，不存原始对话，控制隐私面；
* 所有写入都做白名单与长度校验，模型不能把任意文本塞进记忆。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import MEMORY_DIR

PREFERENCE_KEYS = {
    "home_city": "常驻/出发城市",
    "hotel_brand": "酒店品牌偏好",
    "hotel_price_max": "酒店每晚预算上限",
    "airline_pref": "航司偏好",
    "seat_pref": "座位偏好",
    "flight_time_pref": "航班时段偏好",
    "budget_habit": "差旅预算习惯",
    "avoid_red_eye": "是否避免红眼航班",
    "arrival_time": "到达时间偏好",
    "trip_purpose": "常见出行目的",
    "local_transport": "市内交通偏好",
}

DISALLOWED_PATTERNS = (
    r"\d{17}[\dXx]",  # 身份证
    r"1[3-9]\d{9}",  # 手机号
    r"[\w.+-]+@[\w-]+\.[\w.]+",  # 邮箱
    r"(?i)(password|passwd|token|secret|密码)",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class MemoryStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else MEMORY_DIR
        self.profile_path = self.root / "profile.json"
        self.episodes_path = self.root / "episodes.json"
        self._lock = threading.RLock()
        self._profile = self._load_profile()
        self._episodes = self._load_episodes()

    # -- load ------------------------------------------------------------
    def _load_profile(self) -> Dict[str, Any]:
        default = {
            "version": 1,
            "updated_at": _now(),
            "preferences": {},
            "stats": {"runs": 0, "total_cost": 0.0, "avg_budget": 0.0},
            "routes": {},
        }
        if not self.profile_path.exists():
            return default
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default.update(data)
                default.setdefault("preferences", {})
                default.setdefault("stats", {"runs": 0, "total_cost": 0.0, "avg_budget": 0.0})
                default.setdefault("routes", {})
                return default
        except Exception:
            pass
        return default

    def _load_episodes(self) -> List[dict]:
        if not self.episodes_path.exists():
            return []
        try:
            data = json.loads(self.episodes_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)][-60:]
        except Exception:
            pass
        return []

    # -- read ------------------------------------------------------------
    def profile(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._profile, ensure_ascii=False))

    def preferences(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._profile.get("preferences", {}), ensure_ascii=False))

    def recent_episodes(self, limit: int = 5) -> List[dict]:
        with self._lock:
            return list(self._episodes[-limit:])

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._profile.get("stats", {}))

    def context_text(self, limit: int = 4) -> str:
        with self._lock:
            lines: List[str] = []
            prefs = self._profile.get("preferences", {})
            if prefs:
                pairs = []
                for key, item in list(prefs.items())[:8]:
                    value = item.get("value") if isinstance(item, dict) else item
                    label = PREFERENCE_KEYS.get(key, key)
                    pairs.append(f"{label}={value}")
                lines.append("长期偏好：" + "；".join(pairs))
            routes = self._profile.get("routes", {})
            if routes:
                top = sorted(routes.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)[:3]
                lines.append(
                    "常飞路线："
                    + "；".join(f"{route}×{row.get('count', 0)}" for route, row in top)
                )
            episodes = self._episodes[-limit:]
            if episodes:
                lines.append("最近行程：")
                for episode in episodes:
                    lines.append(
                        f"- {episode.get('depart', '')} {episode.get('origin', '')}→"
                        f"{episode.get('destination', '')} "
                        f"预算¥{episode.get('budget', '?')} 实际¥{episode.get('total', '?')} "
                        f"结果={episode.get('status', '?')}"
                    )
            return "\n".join(lines)
        return ""

    # -- write -----------------------------------------------------------
    def _valid_value(self, key: str, value: str) -> bool:
        if not isinstance(value, str):
            return False
        value = value.strip()
        if not value or len(value) > 60:
            return False
        for pattern in DISALLOWED_PATTERNS:
            if re.search(pattern, value):
                return False
        if key == "seat_pref" and not any(word in value for word in ("靠窗", "过道", "前排", "后排", "无烟")):
            return False
        if key == "flight_time_pref" and not any(
            word in value for word in ("早班", "上午", "午班", "中午", "晚班", "下午", "晚上", "红眼")
        ):
            return False
        return True

    def set_preference(
        self,
        key: str,
        value: str,
        *,
        evidence: str = "",
        source: str = "tool",
        confidence: float = 0.8,
    ) -> bool:
        key = str(key or "").strip()
        value = str(value or "").strip()
        if key not in PREFERENCE_KEYS or not self._valid_value(key, value):
            return False
        with self._lock:
            prefs = self._profile.setdefault("preferences", {})
            prefs[key] = {
                "value": value,
                "evidence": str(evidence or "")[:200],
                "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
                "source": source,
                "updated_at": _now(),
            }
            self._profile["updated_at"] = _now()
            _atomic_write(self.profile_path, self._profile)
        return True

    def remove_preference(self, key: str) -> bool:
        with self._lock:
            prefs = self._profile.setdefault("preferences", {})
            if key in prefs:
                prefs.pop(key)
                self._profile["updated_at"] = _now()
                _atomic_write(self.profile_path, self._profile)
                return True
        return False

    def record_episode(self, episode: Dict[str, Any]) -> None:
        with self._lock:
            self._episodes.append(episode)
            self._episodes = self._episodes[-60:]
            _atomic_write(self.episodes_path, self._episodes)

            stats = self._profile.setdefault("stats", {})
            stats["runs"] = int(stats.get("runs", 0)) + 1
            total = episode.get("total")
            if isinstance(total, (int, float)):
                stats["total_cost"] = round(float(stats.get("total_cost", 0)) + float(total), 2)
                runs = stats["runs"]
                stats["avg_total"] = round(float(stats["total_cost"]) / runs, 2)
            budget = episode.get("budget")
            if isinstance(budget, (int, float)):
                runs = max(1, int(stats.get("runs", 1)))
                previous = float(stats.get("avg_budget", 0)) * (runs - 1)
                stats["avg_budget"] = round((previous + float(budget)) / runs, 2)
            route = f"{episode.get('origin', '')}>{episode.get('destination', '')}"
            routes = self._profile.setdefault("routes", {})
            row = routes.setdefault(route, {"count": 0, "last": ""})
            row["count"] = int(row.get("count", 0)) + 1
            row["last"] = episode.get("ts", _now())
            row["last_total"] = episode.get("total")
            self._profile["updated_at"] = _now()
            _atomic_write(self.profile_path, self._profile)

    def clear(self) -> None:
        with self._lock:
            self._profile = {
                "version": 1,
                "updated_at": _now(),
                "preferences": {},
                "stats": {"runs": 0, "total_cost": 0.0, "avg_budget": 0.0},
                "routes": {},
            }
            self._episodes = []
            _atomic_write(self.profile_path, self._profile)
            _atomic_write(self.episodes_path, self._episodes)

    # -- inference helpers ----------------------------------------------
    def infer_from_goal(self, goal: str) -> List[Dict[str, str]]:
        """从用户原话里抽取少量高置信度偏好，不调用模型。"""

        found: List[Dict[str, str]] = []
        brand_match = re.search(
            r"(亚朵|全季|智选假日|万怡|希尔顿|洲际|美爵|维也纳|如家|木莲庄|华尔道夫)",
            goal,
        )
        if brand_match:
            found.append(
                {"key": "hotel_brand", "value": brand_match.group(1), "evidence": brand_match.group(0)}
            )
        if any(word in goal for word in ("不要红眼", "避免红眼", "别红眼", "不坐红眼")):
            found.append({"key": "avoid_red_eye", "value": "是", "evidence": "用户明确要求避免红眼"})
        if any(word in goal for word in ("靠窗", "过道")) and not any(
            word in goal for word in ("早班", "午班", "晚班")
        ):
            value = "靠窗" if "靠窗" in goal else "过道"
            found.append({"key": "seat_pref", "value": value, "evidence": f"用户偏好{value}"})
        for label, words in (
            ("早班机", ("早班", "早上", "上午")),
            ("午班机", ("午班", "中午")),
            ("晚班机", ("晚班", "晚上")),
            ("避免红眼", ("不要红眼", "避免红眼", "不坐红眼")),
        ):
            if any(word in goal for word in words):
                found.append(
                    {"key": "flight_time_pref", "value": label, "evidence": f"用户提到{label}"}
                )
                break
        return found


__all__ = ["MemoryStore", "PREFERENCE_KEYS"]

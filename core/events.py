"""事件总线：一次运行的所有状态都变成可订阅事件。

Trace（写盘）、SSE（Web 实时流）、CLI（打印）共用同一份事件，避免
「界面看到的」和「trace 记录的」不一致。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


class EventBus:
    def __init__(self, max_events: int = 4000) -> None:
        self._events: List[dict] = []
        self._seq = 0
        self._cond = threading.Condition()
        self._closed = False
        self._started = time.time()
        self._max_events = max_events
        self._listeners: List[Callable[[dict], None]] = []
        self._lock = threading.Lock()

    # -- publish ---------------------------------------------------------
    def emit(self, event_type: str, **data: Any) -> dict:
        with self._cond:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "t": int((time.time() - self._started) * 1000),
                "type": event_type,
            }
            event.update(data)
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
            self._cond.notify_all()
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # listener 不应该影响主流程
                pass
        return event

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # -- consume ---------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def snapshot(self, after: int = 0, limit: Optional[int] = None) -> List[dict]:
        with self._cond:
            items = [e for e in self._events if e["seq"] > after]
        if limit:
            items = items[-limit:]
        return items

    def wait_after(self, after: int, timeout: float = 15.0) -> List[dict]:
        """等待 after 之后的新事件；超时返回空列表，供 SSE 发送心跳。"""

        deadline = time.time() + max(0.0, timeout)
        with self._cond:
            while True:
                items = [e for e in self._events if e["seq"] > after]
                if items:
                    return items
                if self._closed:
                    return []
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._cond.wait(min(remaining, 5.0))

    def wait_closed(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        with self._cond:
            while not self._closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 5.0))
            return True

    def as_jsonl(self) -> str:
        return "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in self.snapshot())


def sse_pack(event: dict) -> str:
    """SSE 帧：只发 data，事件类型放在 JSON 的 type 字段里。

    这样浏览器端一个 ``onmessage`` 就能处理所有事件，避免为几十种事件
    分别注册 listener，也方便断线重连后按 seq 去重。
    """

    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


class NullBus(EventBus):
    """离线工具调用时使用，避免到处判空。"""

    def __init__(self) -> None:
        super().__init__(max_events=0)


def describe_event(event: Dict[str, Any]) -> str:
    """CLI 打印用的一行摘要。"""

    kind = event.get("type", "?")
    detail = event.get("message") or event.get("reason") or event.get("summary") or ""
    if not detail:
        for key in ("goal", "tool", "step_id", "status"):
            if event.get(key):
                detail = str(event[key])
                break
    return f"[{event.get('t', 0):>6}ms] {kind:<16} {detail}"


__all__ = ["EventBus", "NullBus", "sse_pack", "describe_event"]

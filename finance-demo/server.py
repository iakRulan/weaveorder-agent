#!/usr/bin/env python3
"""织序 · 智能信贷风控控制台后端。

为 180 赛题演示视频与评审提供可运行的真实界面：
    自然语言需求 → 信息理解 → 确定性规划 DAG → 系统调用（SQL 真执行）
    → 独立校验 → 可回溯风险报告

与 ``app/server.py`` 共用 EventBus / RunTrace 事件管线，只是把编排器
换成金融场景的 ``FinanceRun``（工具集与数据源不同，引擎原语一致）。

启动：
    .venv/bin/uvicorn finance-demo.server:app --host 127.0.0.1 --port 8010
"""

from __future__ import annotations

import json
import queue
import threading
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.events import sse_pack  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="织序 · 智能信贷风控控制台", version="0.4.0")

EXAMPLES = [
    {"label": "违约归因", "goal": "分析这批授信客户的违约风险，找出高风险客群并归因"},
    {"label": "逾期分析", "goal": "分析逾期客群的违约风险"},
    {"label": "额度与使用率", "goal": "分析授信额度和额度使用率与违约的关系"},
    {"label": "客群分层", "goal": "分析不同年龄和学历客群的违约率差异"},
]


class RunRequest(BaseModel):
    goal: str = Field(..., min_length=2)
    mock: bool = Field(True, description="无模型 Key 时使用规则解析")


def _build_llm(mock: bool):
    if mock:
        return None
    try:
        from core.llm import LLM
        return LLM()
    except Exception:
        return None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "scenario": "finance", "version": "0.4.0"}


@app.get("/api/examples")
def examples() -> dict:
    return {"examples": EXAMPLES}


@app.post("/api/run")
def run(req: RunRequest) -> StreamingResponse:
    """SSE 流式执行：把 FinanceRun 的 EventBus 事件逐条推给前端。"""
    from finance_agent import FinanceRun

    goal = (req.goal or "").strip()
    if len(goal) < 2:
        raise HTTPException(status_code=400, detail="需求太短")

    q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            run = FinanceRun(goal, verify=True, llm=_build_llm(req.mock))
            box["run_id"] = run.run_id
            run.bus.subscribe(q.put)
            payload = run.run()
            box["payload"] = payload
            q.put({"type": "__done__", "payload": payload})
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "__error__", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=_worker, daemon=True).start()

    async def _stream():
        while True:
            try:
                event = q.get(timeout=1.0)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if event.get("type") == "__done__":
                payload = event.get("payload") or {}
                yield sse_pack({
                    "type": "run_end",
                    "run_id": payload.get("run_id"),
                    "status": payload.get("status"),
                    "duration_ms": payload.get("duration_ms"),
                    "intent": payload.get("intent"),
                    "metrics": payload.get("metrics"),
                    "artifacts": payload.get("artifacts"),
                })
                yield sse_pack({"type": "__closed__"})
                break
            if event.get("type") == "__error__":
                yield sse_pack({"type": "run_error", "error": event.get("error")})
                break
            # trace 自带的 run_end 字段不全，统一由上面的合成事件承担
            if event.get("type") == "run_end":
                continue
            yield sse_pack(event)

    return StreamingResponse(_stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/run/{run_id}")
def run_result(run_id: str) -> dict:
    path = ROOT / "runs" / run_id / "result.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="运行不存在")
    return json.loads(path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")

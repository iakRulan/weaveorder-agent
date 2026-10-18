#!/usr/bin/env python3
"""织序 WeaveOrder · A2A（Agent2Agent）协议 Server。

实现 A2A 开放协议（Google 发起，现属 Linux Foundation）的 JSON-RPC 2.0
over HTTP 绑定，让织序可以被任何 A2A 客户端发现与调用：

- GET  /.well-known/agent-card.json（含 v1.0 ``supportedInterfaces`` 与
        v0.3 兼容字段；``/.well-known/agent.json`` 为旧版别名）
- POST /  JSON-RPC：
    message/send    同步执行一次编排，返回 Task（artifacts 含报告）
    message/stream  SSE 流式返回 TaskStatusUpdateEvent / TaskArtifactUpdateEvent
    tasks/get       读取任务状态
    tasks/cancel    协作式取消运行中的任务
  同时接受 v1.0 命名（SendMessage / SendStreamingMessage / GetTask /
  CancelTask / SubscribeToTask）作为别名。

安全与部署：
- 默认 mock 模式（无需 API Key），可公开部署给评委体验；
  设 ``WEAVEORDER_A2A_ALLOW_LIVE=1`` 并配置 .env 后启用真实书生模型。
- 高风险动作在 A2A 侧默认自动批准（对话式审批由 Web 控制台承担）；
  审批记录仍完整写入 Trace。

Run:
    .venv/bin/python a2a_server.py --host 127.0.0.1 --port 8001
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core import VERSION
from core.engine import Orchestrator, RunOptions
from core.events import EventBus
from core.skills import list_skills

REPO_URL = "https://github.com/iakRulan/weaveorder-agent"
ALLOW_LIVE = False  # 由环境变量在启动时决定；见 main()

app = FastAPI(title="织序 WeaveOrder · A2A Server", version=VERSION)

_LOCK = threading.Lock()
_TASKS: Dict[str, Dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def agent_card(base_url: str) -> Dict[str, Any]:
    return {
        "name": "织序 WeaveOrder",
        "description": (
            "一句话目标 → 可执行、可校验、可审计的任务编排：并行 DAG、确定性校验、"
            "三级失败恢复（步骤内修复 → 预算优化 → 重规划）、高风险动作人工审批、"
            "全量 Trace 与长期记忆。基于书生大模型（Intern-S2 / TokenPlan）。"
        ),
        "version": VERSION,
        "protocolVersion": "0.3.0",
        "url": base_url,
        "supportedInterfaces": [
            {"url": base_url, "transport": "JSONRPC", "protocolVersion": "0.3.0"}
        ],
        "provider": {"organization": "兰儒恺（个人开发者）", "url": REPO_URL},
        "documentationUrl": REPO_URL,
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [skill.to_card() for skill in list_skills()],
    }


def _rpc(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _extract_goal(message: Any) -> str:
    """从 A2A Message 提取文本（兼容 v0.3 kind:text 与 v1.0 text part）。"""
    if not isinstance(message, dict):
        return ""
    chunks: List[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            if isinstance(part, str):
                chunks.append(part)
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text)
            continue
        data = part.get("data")
        if isinstance(data, dict) and isinstance(data.get("goal"), str):
            chunks.append(data["goal"])
    return "\n".join(chunks).strip()


def _artifact_report(result: Dict[str, Any]) -> Dict[str, Any]:
    report = str(result.get("report") or "")
    facts = result.get("facts") or {}
    return {
        "artifactId": f"artifact-{result.get('run_id', 'unknown')}-report",
        "name": "travel-report.md",
        "description": "可审计的差旅编排报告（含费用明细、合规检查与决策依据）",
        "parts": [
            {"kind": "text", "text": report},
            {
                "kind": "data",
                "data": {
                    "run_id": result.get("run_id"),
                    "status": result.get("status"),
                    "total": facts.get("total"),
                    "budget": facts.get("budget"),
                    "outbound_flight": facts.get("outbound_flight"),
                    "inbound_flight": facts.get("inbound_flight"),
                    "hotel": facts.get("hotel"),
                    "warnings": (result.get("warnings") or [])[:10],
                    "metrics": {
                        key: (result.get("metrics") or {}).get(key)
                        for key in (
                            "duration_ms",
                            "steps_ok",
                            "steps_total",
                            "replans",
                            "repairs",
                            "parallel_rounds",
                        )
                    },
                },
                "metadata": {"mediaType": "application/json"},
            },
        ],
    }


def _start_task(goal: str) -> Dict[str, Any]:
    task_id = uuid.uuid4().hex
    context_id = uuid.uuid4().hex
    task = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "submitted", "timestamp": _now()},
        "artifacts": [],
        "history": [
            {
                "kind": "message",
                "messageId": uuid.uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": goal}],
                "contextId": context_id,
                "taskId": task_id,
            }
        ],
        "metadata": {"mock": not ALLOW_LIVE},
    }
    entry = {"task": task, "orch": None, "done": threading.Event(), "error": ""}
    with _LOCK:
        _TASKS[task_id] = entry

    def _worker() -> None:
        task["status"] = {"state": "working", "timestamp": _now()}
        bus = EventBus()
        options = RunOptions(
            goal=goal,
            mode="dag",
            mock=not ALLOW_LIVE,
            use_memory=False,
            auto_approve=True,
            run_id=f"a2a-{task_id[:12]}",
        )
        orch = Orchestrator(options, bus=bus)
        entry["orch"] = orch
        try:
            result = orch.run()
            entry["result"] = result
            if result.get("status") == "error":
                task["status"] = {
                    "state": "failed",
                    "message": {"kind": "message", "role": "agent", "parts": [
                        {"kind": "text", "text": str(result.get("error") or "运行失败")[:1000]}
                    ]},
                    "timestamp": _now(),
                }
            else:
                task["status"] = {"state": "completed", "timestamp": _now()}
                task["artifacts"] = [_artifact_report(result)]
        except Exception as exc:  # pragma: no cover - 防御性兜底
            entry["error"] = f"{type(exc).__name__}: {exc}"
            task["status"] = {
                "state": "failed",
                "message": {"kind": "message", "role": "agent", "parts": [
                    {"kind": "text", "text": entry["error"][:1000]}
                ]},
                "timestamp": _now(),
            }
        finally:
            entry["done"].set()

    threading.Thread(target=_worker, daemon=True).start()
    return entry


def _task_not_found(request_id: Any, task_id: Any) -> Dict[str, Any]:
    return _rpc_error(request_id, -32001, f"Task not found: {task_id}")


def _handle_rpc(payload: Dict[str, Any]) -> Dict[str, Any]:
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    aliases = {
        "SendMessage": "message/send",
        "SendStreamingMessage": "message/stream",
        "GetTask": "tasks/get",
        "CancelTask": "tasks/cancel",
        "SubscribeToTask": "message/stream",
    }
    method = aliases.get(method, method)

    if method == "ping":
        return _rpc(request_id, {})

    if method == "message/send":
        goal = _extract_goal(params.get("message"))
        if not goal:
            return _rpc_error(request_id, -32602, "params.message 缺少文本内容")
        entry = _start_task(goal)
        # 同步语义：等待运行结束（默认 mock，秒级返回）。
        entry["done"].wait(timeout=600)
        return _rpc(request_id, entry["task"])

    if method == "tasks/get":
        task_id = params.get("id")
        with _LOCK:
            entry = _TASKS.get(str(task_id or ""))
        if entry is None:
            return _task_not_found(request_id, task_id)
        return _rpc(request_id, entry["task"])

    if method == "tasks/cancel":
        task_id = str(params.get("id") or "")
        with _LOCK:
            entry = _TASKS.get(task_id)
        if entry is None:
            return _task_not_found(request_id, task_id)
        orch = entry.get("orch")
        if orch is not None:
            orch.cancel()
        entry["task"]["status"] = {"state": "canceled", "timestamp": _now()}
        return _rpc(request_id, entry["task"])

    return _rpc_error(request_id, -32601, f"Method not found: {method}")


@app.get("/")
async def index() -> Dict[str, Any]:
    return {"server": "weaveorder-a2a", "version": VERSION, "mock": not ALLOW_LIVE}


@app.get("/.well-known/agent-card.json")
@app.get("/.well-known/agent.json")
async def well_known_agent_card(request: Request) -> Dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return agent_card(base)


@app.post("/")
async def rpc_endpoint(payload: Dict[str, Any], request: Request) -> Any:
    method = str(payload.get("method") or "")
    aliases = {
        "SendMessage": "message/send",
        "SendStreamingMessage": "message/stream",
        "GetTask": "tasks/get",
        "CancelTask": "tasks/cancel",
        "SubscribeToTask": "message/stream",
    }
    method = aliases.get(method, method)
    if method in ("message/stream",) and "text/event-stream" in (
        request.headers.get("accept") or ""
    ):
        return await message_stream(payload)
    return JSONResponse(_handle_rpc(payload))


@app.post("/rpc")
async def rpc_alias(payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(_handle_rpc(payload))


def _sse(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/stream")
async def message_stream(payload: Dict[str, Any]) -> StreamingResponse:
    """SSE 版 message/stream（POST / 的 /stream 绑定，事件顺序与线上一致）。"""
    params = payload.get("params") or {}
    goal = _extract_goal(params.get("message"))
    if not goal:
        return JSONResponse(
            _rpc_error(payload.get("id"), -32602, "params.message 缺少文本内容"),
            status_code=200,
        )
    entry = _start_task(goal)
    task = entry["task"]

    def _gen():  # 同步生成器：Starlette 在线程池中迭代，轮询等待不阻塞事件循环
        yield _sse({"task": task})
        last_state = ""
        while not entry["done"].wait(0.3):
            state = task["status"].get("state", "")
            if state != last_state:
                last_state = state
                yield _sse({"statusUpdate": {
                    "kind": "status-update",
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "status": task["status"],
                    "final": False,
                }})
        state = task["status"].get("state", "")
        for artifact in task.get("artifacts") or []:
            yield _sse({"artifactUpdate": {
                "kind": "artifact-update",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "artifact": artifact,
                "append": False,
                "lastChunk": True,
            }})
        yield _sse({"statusUpdate": {
            "kind": "status-update",
            "taskId": task["id"],
            "contextId": task["contextId"],
            "status": task["status"],
            "final": True,
        }})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main() -> None:
    global ALLOW_LIVE
    import os

    parser = argparse.ArgumentParser(description="织序 WeaveOrder A2A Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--live",
        action="store_true",
        help="启用真实书生模型（需 .env 配置；默认 mock，无需 Key）",
    )
    args = parser.parse_args()
    ALLOW_LIVE = bool(args.live or os.environ.get("WEAVEORDER_A2A_ALLOW_LIVE"))

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

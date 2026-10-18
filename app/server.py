"""FastAPI 控制台后端：SSE 实时事件、运行历史、审批、记忆、模型元数据。

启动：
    .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    ApprovalGate,
    Config,
    EventBus,
    MemoryStore,
    Orchestrator,
    RunOptions,
    VERSION,
)
from core.config import RUNS_DIR, ensure_dirs  # noqa: E402
from core.events import sse_pack  # noqa: E402
from core.memory import PREFERENCE_KEYS  # noqa: E402
from core.tools import build_registry  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
GLOBAL_MEMORY = MemoryStore()
ensure_dirs()

PUBLIC_DEMO = os.environ.get("WEAVE_PUBLIC_DEMO", "").lower() in ("1", "true", "yes")
_PUBLIC_HITS: Dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
_PUBLIC_LOCK = threading.Lock()


def _public_rate_ok(ip: str) -> bool:
    """公开演示限流：每个 IP 10 分钟内最多 20 次。"""
    if not PUBLIC_DEMO:
        return True
    now = time.time()
    with _PUBLIC_LOCK:
        hits = _PUBLIC_HITS[ip]
        while hits and now - hits[0] > 600:
            hits.popleft()
        if len(hits) >= 20:
            return False
        hits.append(now)
        return True


app = FastAPI(title="织序 WeaveOrder", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------------------
# 运行注册表
# ---------------------------------------------------------------------------


@dataclass
class ActiveRun:
    run_id: str
    goal: str
    mode: str
    model: str
    mock: bool
    auto_approve: bool
    bus: EventBus
    gate: ApprovalGate
    orchestrator: Orchestrator
    started_at: float = field(default_factory=time.time)
    started_iso: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "queued"
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    thread: Optional[threading.Thread] = None
    finished_at: Optional[float] = None

    def summary(self) -> Dict[str, Any]:
        result = self.result or {}
        metrics = result.get("metrics") or {}
        facts = result.get("facts") or {}
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "mode": self.mode,
            "model": self.model,
            "mock": self.mock,
            "status": self.status,
            "started_at": self.started_iso,
            "duration_ms": metrics.get("duration_ms")
            or int(((self.finished_at or time.time()) - self.started_at) * 1000),
            "total": facts.get("total"),
            "budget": facts.get("budget"),
            "steps_total": metrics.get("steps_total", 0),
            "steps_ok": metrics.get("steps_ok", 0),
            "warnings": len(result.get("warnings") or []),
            "replans": metrics.get("replans", 0),
            "tokens": (metrics.get("llm") or {}).get("total_tokens", 0),
            "error": self.error,
            "active": self.status in ("queued", "running", "waiting_approval"),
        }


class RunRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: Dict[str, ActiveRun] = {}

    def new_id(self) -> str:
        base = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        candidate = base
        counter = 1
        while (RUNS_DIR / candidate).exists() or candidate in self._runs:
            counter += 1
            candidate = f"{base}-{counter}"
        return candidate

    def create(self, options: RunOptions, auto_approve: bool) -> ActiveRun:
        run_id = options.run_id or self.new_id()
        options.run_id = run_id
        bus = EventBus()
        gate = ApprovalGate(
            mode="auto" if auto_approve else "remote",
            timeout=Config().approval_timeout,
        )
        orchestrator = Orchestrator(
            options,
            bus=bus,
            gate=gate,
            memory=GLOBAL_MEMORY if options.use_memory else None,
        )
        run = ActiveRun(
            run_id=run_id,
            goal=options.goal,
            mode=options.mode,
            model=options.model or orchestrator.cfg.model,
            mock=orchestrator.cfg.mock,
            auto_approve=auto_approve,
            bus=bus,
            gate=gate,
            orchestrator=orchestrator,
        )

        def worker() -> None:
            try:
                run.status = "running"
                result = orchestrator.run()
                run.result = result
                run.status = result.get("status", "unknown")
            except Exception as exc:  # pragma: no cover
                run.status = "error"
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                run.finished_at = time.time()

        run.thread = threading.Thread(target=worker, name=f"weave-{run_id}", daemon=True)
        with self._lock:
            self._runs[run_id] = run
            # 保留最近 40 个内存运行，磁盘里的历史仍可查询。
            if len(self._runs) > 40:
                old = sorted(
                    self._runs.values(),
                    key=lambda item: item.started_at,
                )[: len(self._runs) - 40]
                for item in old:
                    if item.status not in ("queued", "running", "waiting_approval"):
                        self._runs.pop(item.run_id, None)
        run.thread.start()
        return run

    def get(self, run_id: str) -> Optional[ActiveRun]:
        with self._lock:
            return self._runs.get(run_id)

    def list_active(self) -> List[ActiveRun]:
        with self._lock:
            return list(self._runs.values())

    def pending(self) -> List[dict]:
        items = []
        for run in self.list_active():
            for request in run.gate.pending():
                items.append({"run_id": run.run_id, "goal": run.goal, **request})
        return items


REGISTRY = RunRegistry()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    goal: str = Field(..., min_length=2, max_length=2000)
    mode: str = Field("dag", pattern="^(dag|agent)$")
    mock: bool = False
    inject_failure: bool = False
    model: Optional[str] = None
    fast_model: Optional[str] = None
    thinking: Optional[bool] = None
    use_memory: bool = True
    auto_approve: bool = False


class ApprovalBody(BaseModel):
    approval_id: Optional[str] = None
    approved: bool = True
    note: str = ""


class MemoryBody(BaseModel):
    key: str
    value: str
    evidence: str = ""


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    cfg = Config()
    registry = build_registry(None, cfg)
    return {
        "ok": True,
        "version": VERSION,
        "model": cfg.model,
        "fast_model": cfg.fast_model,
        "base_url": cfg.base_url,
        "mock_default": cfg.mock,
        "public_demo": PUBLIC_DEMO,
        "key_configured": bool(cfg.api_key),
        "max_tokens": cfg.max_tokens,
        "tools": len(registry.schemas()),
        "capabilities": ["dag", "agent", "sse", "approval", "memory", "mcp"],
        "runs_on_disk": len([p for p in RUNS_DIR.glob("*/result.json")]),
        "pending_approvals": len(REGISTRY.pending()),
    }


_MODEL_CACHE: Dict[str, Any] = {"ts": 0.0, "models": []}
_MODEL_LOCK = threading.Lock()

KNOWN_MODELS = [
    {"id": "intern-s2", "label": "Intern-S2", "note": "书生旗舰 · 深度规划/审查（推荐）", "recommended": True},
    {"id": "qwen3.8-27b", "label": "Qwen3.8-27B", "note": "快速结构化 · 适合演示与抽取"},
    {"id": "glm-5.3", "label": "GLM-5.3", "note": "通用对话与写作"},
    {"id": "kimi-k2.6", "label": "Kimi K2.6", "note": "长上下文"},
    {"id": "minimax-m3", "label": "MiniMax M3", "note": "通用"},
    {"id": "deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash", "note": "低成本快速"},
]


@app.get("/api/models")
def models() -> dict:
    cfg = Config()
    with _MODEL_LOCK:
        stale = time.time() - float(_MODEL_CACHE.get("ts", 0)) > 300
        if stale or not _MODEL_CACHE.get("models"):
            from core.llm import LLM

            client = LLM(cfg)
            ids = client.list_models()
            known = {item["id"]: item for item in KNOWN_MODELS}
            merged = []
            for model_id in ids or [item["id"] for item in KNOWN_MODELS]:
                meta = known.get(model_id, {"id": model_id, "label": model_id, "note": ""})
                merged.append(meta)
            if cfg.model not in {item["id"] for item in merged}:
                merged.insert(0, {"id": cfg.model, "label": cfg.model, "note": "当前配置"})
            _MODEL_CACHE.update({"ts": time.time(), "models": merged})
        return {
            "models": _MODEL_CACHE["models"],
            "default": cfg.model,
            "fast": cfg.fast_model,
        }


@app.get("/api/examples")
def examples() -> dict:
    return {
        "examples": [
            {
                "label": "标准差旅",
                "tag": "DAG",
                "hint": "2 晚 · ¥3000 · 早班",
                "goal": "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元，偏好早班机",
            },
            {
                "label": "预算紧张",
                "tag": "降级",
                "hint": "预算压缩 → 冲突标注",
                "goal": "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 2000 元，偏好早班机",
            },
            {
                "label": "北京出发",
                "tag": "换路线",
                "hint": "¥3500",
                "goal": "帮我安排 2026-10-28 北京到深圳的客户拜访，10-30 返回，预算 3500 元",
            },
            {
                "label": "广州短途",
                "tag": "2 天",
                "hint": "¥2500",
                "goal": "帮我安排 2026-10-28 上海到广州的两天出差，10-30 返回，预算 2500 元",
            },
            {
                "label": "信息不全",
                "tag": "澄清",
                "hint": "缺日期 → 假设与追问",
                "goal": "帮我安排 上海到深圳的客户拜访，预算 3000 元",
            },
            {
                "label": "不可达城市",
                "tag": "审计",
                "hint": "不编造数据",
                "goal": "帮我安排 2026-10-28 成都到西安的客户拜访，10-30 返回，预算 3000 元",
            },
            {
                "label": "长住 4 晚",
                "tag": "政策",
                "hint": "超 3 晚需说明",
                "goal": "帮我安排 2026-10-28 上海到深圳的项目驻场，11-01 返回，预算 4000 元",
            },
            {
                "label": "自主 Agent",
                "tag": "Function Calling",
                "hint": "模型自己选工具",
                "mode": "agent",
                "goal": "帮我安排 2026-10-28 上海到深圳的客户拜访，10-30 返回，预算 3000 元，偏好早班机",
            },
        ]
    }


@app.get("/api/tools")
def tools() -> dict:
    cfg = Config()
    registry = build_registry(GLOBAL_MEMORY, cfg)
    return {"tools": registry.schemas()}


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


@app.post("/api/runs")
def create_run(body: RunRequest, request: Request) -> dict:
    goal = body.goal.strip()
    if PUBLIC_DEMO:
        ip = (request.client.host if request.client else "unknown") or "unknown"
        if not _public_rate_ok(ip):
            raise HTTPException(status_code=429, detail="公开演示限流：请 10 分钟后再试。")
        if len(goal) > 400:
            raise HTTPException(status_code=400, detail="公开演示目标请控制在 400 字以内。")
        # 公开演示硬约束：只跑确定性 Mock，不写用户记忆，不触达真实模型
        body = body.model_copy(update={"mock": True, "mode": "dag", "use_memory": False,
                                       "model": None, "fast_model": None})
    if len(goal) < 2:
        raise HTTPException(status_code=400, detail="目标不能为空")
    if not body.mock and not Config().api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 INTERN_API_KEY，无法运行真实模型；请开启 Mock 或先在 .env 中配置 Key。",
        )
    if body.mode == "agent" and body.mock:
        raise HTTPException(
            status_code=400,
            detail="自主 Agent 模式需要真实模型；请关闭 Mock，或使用 DAG 编排。",
        )
    options = RunOptions(
        goal=goal,
        mode=body.mode,
        mock=body.mock,
        model=(body.model or "").strip() or None,
        fast_model=(body.fast_model or "").strip() or None,
        thinking=body.thinking,
        use_memory=body.use_memory,
        auto_approve=body.auto_approve,
        inject_failure=body.inject_failure,
    )
    run = REGISTRY.create(options, auto_approve=body.auto_approve)
    return {
        "run_id": run.run_id,
        "status": "queued",
        "mode": run.mode,
        "model": run.model,
        "mock": run.mock,
        "events_url": f"/api/runs/{run.run_id}/events",
    }


@app.get("/api/runs")
def list_runs(limit: int = Query(40, ge=1, le=200)) -> dict:
    items: Dict[str, Dict[str, Any]] = {}
    for path in sorted(RUNS_DIR.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if result.get("version") != VERSION:
            continue
        metrics = result.get("metrics") or {}
        facts = result.get("facts") or {}
        items[result.get("run_id", path.parent.name)] = {
            "run_id": result.get("run_id", path.parent.name),
            "goal": result.get("goal", ""),
            "mode": result.get("mode", "dag"),
            "model": result.get("model", ""),
            "mock": bool(result.get("mock")),
            "status": result.get("status", "unknown"),
            "started_at": result.get("started_at", ""),
            "duration_ms": result.get("duration_ms") or metrics.get("duration_ms"),
            "total": facts.get("total"),
            "budget": facts.get("budget"),
            "steps_total": metrics.get("steps_total", 0),
            "steps_ok": metrics.get("steps_ok", 0),
            "warnings": len(result.get("warnings") or []),
            "replans": metrics.get("replans", 0),
            "tokens": (metrics.get("llm") or {}).get("total_tokens", 0),
            "error": result.get("error", ""),
            "active": False,
        }
    for run in REGISTRY.list_active():
        items[run.run_id] = run.summary()
    ordered = sorted(
        items.values(),
        key=lambda item: str(item.get("started_at", "")) + str(item.get("run_id", "")),
        reverse=True,
    )
    return {"runs": ordered[:limit], "pending": REGISTRY.pending()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    active = REGISTRY.get(run_id)
    run_dir = RUNS_DIR / run_id
    if active is not None and active.result is None:
        return {
            "run_id": run_id,
            "status": active.status,
            "goal": active.goal,
            "mode": active.mode,
            "model": active.model,
            "mock": active.mock,
            "started_at": active.started_iso,
            "active": True,
            "pending": active.gate.pending(),
            "events_url": f"/api/runs/{run_id}/events",
        }
    result_path = run_dir / "result.json"
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["active"] = False
            payload.setdefault("events_url", f"/api/runs/{run_id}/events")
            return payload
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取运行结果失败：{exc}") from exc
    if active is not None:
        return active.summary()
    raise HTTPException(status_code=404, detail="运行不存在")


def _clean_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run_id or ""):
        raise HTTPException(status_code=400, detail="非法 run_id")
    return run_id


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str, after: int = Query(0, ge=0)):
    run_id = _clean_run_id(run_id)
    run = REGISTRY.get(run_id)
    if run is None:
        if (RUNS_DIR / run_id / "trace.jsonl").exists():
            events = []
            for line in (RUNS_DIR / run_id / "trace.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue

            def replay():
                for event in events:
                    yield sse_pack(event)
                yield 'data: {"type": "done"}\n\n' 

            return StreamingResponse(
                replay(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        raise HTTPException(status_code=404, detail="运行不存在")
    bus = run.bus

    def generator():
        last = after
        idle = 0
        while True:
            events = bus.wait_after(last, timeout=10)
            if events:
                idle = 0
                for event in events:
                    last = max(last, int(event.get("seq", 0)))
                    yield sse_pack(event)
                continue
            if bus.closed and last >= bus.last_seq:
                break
            idle += 1
            if idle % 2 == 0:
                yield f": ping {int(time.time())}\n\n"
        yield 'data: {"type": "done"}\n\n' 

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str, body: ApprovalBody) -> dict:
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在或已结束")
    pending = run.gate.pending()
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待审批动作")
    targets = [body.approval_id] if body.approval_id else [item["approval_id"] for item in pending]
    resolved = []
    for approval_id in targets:
        if approval_id and run.gate.resolve(approval_id, body.approved, body.note):
            resolved.append(approval_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="审批单不存在或已处理")
    return {"ok": True, "resolved": resolved, "approved": body.approved}


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str) -> dict:
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在或已结束")
    run.orchestrator.cancel()
    return {"ok": True, "status": "cancelling"}


@app.get("/api/runs/{run_id}/artifacts/{name}")
def artifact(run_id: str, name: str) -> FileResponse:
    run_id = _clean_run_id(run_id)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name or "") or name.startswith(".") or ".." in name:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = (RUNS_DIR / run_id / name).resolve()
    try:
        path.relative_to((RUNS_DIR / run_id).resolve())
    except Exception as exc:
        raise HTTPException(status_code=400, detail="非法路径") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    media = {
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".jsonl": "application/x-ndjson; charset=utf-8",
    }.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


@app.get("/api/skills")
def skills() -> dict:
    """已安装的 Agent Skills（agentskills.io 开放标准）。"""
    from core.skills import list_skills

    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "path": str(s.path.relative_to(ROOT)),
                "meta": s.meta,
            }
            for s in list_skills()
        ],
        "standard": "https://agentskills.io/specification",
    }


@app.get("/api/memory")
def memory() -> dict:
    return {
        "profile": GLOBAL_MEMORY.profile(),
        "preferences": GLOBAL_MEMORY.preferences(),
        "episodes": GLOBAL_MEMORY.recent_episodes(limit=12),
        "stats": GLOBAL_MEMORY.stats(),
        "context": GLOBAL_MEMORY.context_text(),
        "allowed_keys": PREFERENCE_KEYS,
    }


@app.post("/api/memory")
def set_memory(body: MemoryBody) -> dict:
    ok = GLOBAL_MEMORY.set_preference(
        body.key, body.value, evidence=body.evidence, source="user"
    )
    if not ok:
        raise HTTPException(status_code=400, detail=f"不支持的偏好键或值：{body.key}")
    return {"ok": True, "preferences": GLOBAL_MEMORY.preferences()}


@app.delete("/api/memory/{key}")
def delete_memory(key: str) -> dict:
    removed = GLOBAL_MEMORY.remove_preference(key)
    return {"ok": removed, "preferences": GLOBAL_MEMORY.preferences()}


@app.delete("/api/memory")
def clear_memory() -> dict:
    GLOBAL_MEMORY.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


@app.get("/api/stats")
def stats() -> dict:
    results = []
    for path in RUNS_DIR.glob("*/result.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if result.get("version") == VERSION:
            results.append(result)
    total = len(results)
    status_counts: Dict[str, int] = {}
    tokens = 0
    replans = 0
    repairs = 0
    tool_calls = 0
    durations = []
    for result in results:
        status = result.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        metrics = result.get("metrics") or {}
        tokens += int((metrics.get("llm") or {}).get("total_tokens", 0) or 0)
        replans += int(metrics.get("replans", 0) or 0)
        repairs += int(metrics.get("repairs", 0) or 0) + int(metrics.get("optimizations", 0) or 0)
        tool_calls += int(metrics.get("tool_calls", 0) or 0)
        if result.get("duration_ms"):
            durations.append(int(result["duration_ms"]))
    success = sum(
        count
        for status, count in status_counts.items()
        if status in ("ok", "ok_with_warnings", "degraded")
    )
    return {
        "runs": total,
        "status_counts": status_counts,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "tokens": tokens,
        "replans": replans,
        "repairs": repairs,
        "tool_calls": tool_calls,
        "active": len([run for run in REGISTRY.list_active() if run.result is None]),
        "pending_approvals": len(REGISTRY.pending()),
    }

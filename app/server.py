"""FastAPI wrapper around the WeaveOrder orchestrator.

Run from the project root:
    .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="织序 WeaveOrder", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class RunRequest(BaseModel):
    goal: str = Field(..., min_length=2, max_length=2000)
    mock: bool = False
    inject_failure: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    cfg = agent.Config()
    return {
        "ok": True,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "mock_default": cfg.mock,
        "max_tokens": cfg.max_tokens,
    }


@app.post("/api/run")
def run(req: RunRequest) -> dict:
    goal = req.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal 不能为空")

    cfg = agent.Config(mock=req.mock)
    llm = agent.LLM(cfg)
    tools = agent.build_registry()
    orch = agent.Orchestrator(
        llm,
        tools,
        auto_approve=True,
        inject_failure=req.inject_failure,
    )
    try:
        report, run_dir = orch.run(goal)
    except Exception as exc:  # keep the UI useful even when the model fails
        raise HTTPException(
            status_code=500,
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    plan_path = run_dir / "plan.json"
    trace_path = run_dir / "trace.jsonl"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    trace = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    status = {
        step_id: {"status": state[0], "reason": state[1]}
        for step_id, state in orch.status.items()
    }
    return {
        "run_id": run_dir.name,
        "mode": "mock" if cfg.mock else "live",
        "model": cfg.model,
        "plan": plan,
        "trace": trace,
        "report": report,
        "warnings": orch.warnings,
        "status": status,
    }

"""运行配置：环境变量、路径、模型路由与代理兼容。

设计要点
--------
1. 所有路径都从本文件回溯到项目根，CLI / Web / MCP 共用一套。
2. `.env` 是唯一配置入口；没有配置 Key 时自动进入 mock，保证离线可演示。
3. 修复 httpx 0.28 对 ``NO_PROXY=[::1]`` 的解析崩溃：去掉方括号 IPv6 条目。
4. 允许按角色覆盖模型，实现「Intern-S2 规划 + 轻量模型做结构化抽取」的路由。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RUNS_DIR = Path(os.getenv("WEAVE_RUNS_DIR", str(ROOT / "runs"))).expanduser().resolve()
MEMORY_DIR = ROOT / "memory"
CACHE_DIR = ROOT / ".cache"
STATIC_DIR = ROOT / "app" / "static"

VERSION = "0.4.0"
CODE_NAME = "织序 WeaveOrder"

DEFAULT_BASE_URL = "https://discovery-api.intern-ai.org.cn/v1"
DEFAULT_MODEL = "intern-s2"
DEFAULT_FAST_MODEL = "qwen3.8-27b"


def load_env() -> None:
    """加载 .env；不依赖 python-dotenv 也能运行。"""

    env_path = ROOT / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        if not env_path.exists():
            return
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    _sanitize_no_proxy()


def _sanitize_no_proxy() -> None:
    """httpx 0.28 会把 ``[::1]`` 当成非法端口；保留其余代理配置即可。"""

    for key in ("NO_PROXY", "no_proxy"):
        raw = os.getenv(key)
        if not raw:
            continue
        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        safe = [p for p in parts if not (p.startswith("[") and p.endswith("]"))]
        if safe:
            os.environ[key] = ",".join(safe)
        else:
            os.environ.pop(key, None)


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """一次运行的全部可调参数。线程安全：构造后只读。"""

    api_key: str = field(default_factory=lambda: _env_str("INTERN_API_KEY"))
    base_url: str = field(default_factory=lambda: _env_str("INTERN_BASE_URL") or DEFAULT_BASE_URL)
    model: str = field(default_factory=lambda: _env_str("INTERN_MODEL") or DEFAULT_MODEL)
    fast_model: str = field(
        default_factory=lambda: _env_str("INTERN_FAST_MODEL") or DEFAULT_FAST_MODEL
    )
    max_tokens: int = field(default_factory=lambda: _env_int("INTERN_MAX_TOKENS", 4096))
    temperature: float = field(default_factory=lambda: _env_float("INTERN_TEMPERATURE", 0.1))
    timeout: float = field(default_factory=lambda: _env_float("INTERN_TIMEOUT", 180.0))
    thinking: bool = field(default_factory=lambda: _env_bool("INTERN_THINKING", False))
    mock: bool = False
    use_memory: bool = True
    auto_approve: bool = True
    approval_timeout: float = field(
        default_factory=lambda: _env_float("WEAVE_APPROVAL_TIMEOUT", 600.0)
    )
    max_steps: int = field(default_factory=lambda: _env_int("WEAVE_MAX_STEPS", 10))
    max_replans: int = field(default_factory=lambda: _env_int("WEAVE_MAX_REPLANS", 2))
    tool_timeout: float = field(default_factory=lambda: _env_float("WEAVE_TOOL_TIMEOUT", 30.0))
    max_reasoning_chars: int = 6000

    # 角色 -> 模型。空表示使用主模型；fast 表示使用 fast_model。
    role_models: Dict[str, str] = field(
        default_factory=lambda: {
            "intake": _env_str("INTERN_INTAKE_MODEL") or "fast",
            "planner": _env_str("INTERN_PLANNER_MODEL"),
            "critic": _env_str("INTERN_CRITIC_MODEL") or "fast",
            "replanner": _env_str("INTERN_REPLANNER_MODEL"),
            "verifier": _env_str("INTERN_VERIFIER_MODEL") or "fast",
            "narrator": _env_str("INTERN_NARRATOR_MODEL") or "fast",
            "memory": _env_str("INTERN_MEMORY_MODEL") or "fast",
            "router": _env_str("INTERN_ROUTER_MODEL") or "fast",
        }
    )

    def __post_init__(self) -> None:
        load_env()
        # 重新读取一次，保证在 load_env 之前构造也正确。
        if not self.api_key:
            self.api_key = _env_str("INTERN_API_KEY")
        if not self.base_url:
            self.base_url = _env_str("INTERN_BASE_URL", DEFAULT_BASE_URL)
        if not self.api_key:
            self.mock = True
        if self.mock:
            self.auto_approve = True

    def model_for(self, role: str) -> str:
        target = (self.role_models.get(role) or "").strip()
        if target == "fast":
            return self.fast_model
        return target or self.model

    def describe(self) -> dict:
        return {
            "version": VERSION,
            "model": self.model,
            "fast_model": self.fast_model,
            "base_url": self.base_url,
            "mock": self.mock,
            "thinking": self.thinking,
            "use_memory": self.use_memory,
            "auto_approve": self.auto_approve,
            "max_tokens": self.max_tokens,
            "role_models": {k: self.model_for(k) for k in self.role_models},
        }


def ensure_dirs() -> None:
    for path in (RUNS_DIR, MEMORY_DIR, CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)


# 在类定义后立刻加载，保证 dataclass 默认工厂读到 .env。
load_env()
ensure_dirs()


def public_base_url(cfg: Optional[Config] = None) -> str:
    return (cfg or Config()).base_url

"""书生大模型客户端（OpenAI 兼容）。

关键能力
--------
* **思考开关**：Intern-S2 默认会消耗大量 reasoning token。结构化任务用
  ``thinking={"type":"disabled"}`` 可把规划耗时从分钟级压到秒级；难题再打开。
* **JSON 模式 + 解析修复**：优先 response_format，失败时从文本中抽取，再让模型修复。
* **流式输出**：给 Web 控制台推送真实 token；同时保留首 token 延迟指标。
* **重试与统计**：所有失败、token、耗时都进入运行指标，报告可审计。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import Config, load_env

try:  # pragma: no cover - 环境相关
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class LLMError(RuntimeError):
    pass


def clean_json_text(text: str) -> str:
    text = (text or "").strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_text(text: str) -> Dict[str, Any]:
    """从模型输出里尽量抠出一个 JSON 对象。"""

    cleaned = clean_json_text(text)
    if not cleaned:
        raise LLMError("模型输出为空，无法解析 JSON")
    try:
        value = json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"模型输出中没有 JSON 对象：{cleaned[:200]}")
        fragment = cleaned[start : end + 1]
        try:
            value = json.loads(fragment)
        except Exception:
            # 常见小毛病：尾逗号、中文引号。
            repaired = re.sub(r",\s*([}\]])", r"\1", fragment)
            repaired = repaired.replace("“", '"').replace("”", '"')
            try:
                value = json.loads(repaired)
            except Exception as exc:
                raise LLMError(f"JSON 解析失败：{exc}；片段：{fragment[:300]}") from exc
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, dict):
        raise LLMError("JSON 根节点必须是对象")
    return value


@dataclass
class LLMResult:
    text: str
    model: str
    role: str
    latency_ms: int
    usage: Dict[str, int] = field(default_factory=dict)
    reasoning: str = ""
    attempts: int = 1
    first_token_ms: Optional[int] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    def json(self) -> Dict[str, Any]:
        return parse_json_text(self.text)


class LLMStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        self.retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.latency_ms = 0
        self.first_token_ms = 0
        self.by_role: Dict[str, Dict[str, int]] = {}
        self.by_model: Dict[str, Dict[str, int]] = {}

    def record(
        self,
        role: str,
        model: str,
        latency_ms: int,
        usage: Dict[str, int],
        attempts: int,
        ok: bool = True,
        first_token_ms: Optional[int] = None,
    ) -> None:
        with self._lock:
            self.calls += 1
            if not ok:
                self.errors += 1
            self.retries += max(0, attempts - 1)
            self.latency_ms += latency_ms
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            reasoning = int(usage.get("reasoning_tokens", 0) or 0)
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.reasoning_tokens += reasoning
            if first_token_ms:
                self.first_token_ms += first_token_ms
            for bucket, key in ((self.by_role, role), (self.by_model, model)):
                row = bucket.setdefault(
                    key,
                    {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0},
                )
                row["calls"] += 1
                row["prompt_tokens"] += prompt
                row["completion_tokens"] += completion
                row["latency_ms"] += latency_ms

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "errors": self.errors,
                "retries": self.retries,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "latency_ms": self.latency_ms,
                "avg_latency_ms": round(self.latency_ms / self.calls) if self.calls else 0,
                "by_role": {k: dict(v) for k, v in self.by_role.items()},
                "by_model": {k: dict(v) for k, v in self.by_model.items()},
            }


class LLM:
    """线程安全的模型客户端；一次运行内共享。"""

    def __init__(
        self,
        cfg: Optional[Config] = None,
        on_event: Optional[Callable[..., None]] = None,
    ) -> None:
        load_env()
        self.cfg = cfg or Config()
        self.stats = LLMStats()
        self.on_event = on_event
        self._client = None
        self._client_lock = threading.Lock()
        self._init_error: Optional[str] = None
        if not self.cfg.mock:
            self._ensure_client()

    # -- client ----------------------------------------------------------
    def _ensure_client(self):
        if self._client is not None or self.cfg.mock:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            if OpenAI is None:
                self._init_error = "未安装 openai 包"
                self.cfg.mock = True
                return None
            try:
                self._client = OpenAI(
                    api_key=self.cfg.api_key,
                    base_url=self.cfg.base_url,
                    timeout=self.cfg.timeout,
                    max_retries=0,
                )
            except Exception as exc:  # pragma: no cover - 本地环境
                self._init_error = str(exc)
                self.cfg.mock = True
                print(f"[warn] 初始化模型客户端失败：{exc}；改用 mock", file=sys.stderr)
        return self._client

    @property
    def available(self) -> bool:
        return not self.cfg.mock and self._client is not None

    def _model(self, role: str, model: Optional[str]) -> str:
        return model or self.cfg.model_for(role)

    def list_models(self) -> List[str]:
        """列出书生 TokenPlan 当前可用的模型；失败时返回空列表。"""

        if not self.available and not self._ensure_client():
            return []
        try:
            assert self._client is not None
            client = self._client.with_options(timeout=8.0) if hasattr(self._client, "with_options") else self._client
            return [item.id for item in client.models.list().data]
        except Exception:
            return []

    # -- calls -----------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        role: str = "default",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        thinking: Optional[bool] = None,
        stream_cb: Optional[Callable[[str], None]] = None,
        retries: int = 2,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        allow_tool_calls: bool = False,
    ) -> LLMResult:
        if not self.available and not self._ensure_client():
            raise LLMError("模型不可用（无 Key 或初始化失败），请使用 mock 模式")
        client = self._client
        assert client is not None
        model_name = self._model(role, model)
        use_thinking = self.cfg.thinking if thinking is None else thinking
        base_tokens = max_tokens or self.cfg.max_tokens
        last_exc: Optional[Exception] = None
        attempts = max(1, retries + 1)
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            first_token_ms: Optional[int] = None
            chunks: List[str] = []
            usage: Dict[str, int] = {}
            reasoning_parts: List[str] = []
            try:
                kwargs: Dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "temperature": self.cfg.temperature if temperature is None else temperature,
                    "max_tokens": base_tokens,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                if not use_thinking:
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                if tools:
                    kwargs["tools"] = tools
                if tool_choice:
                    kwargs["tool_choice"] = tool_choice
                if stream_cb is not None:
                    kwargs["stream"] = True
                    kwargs["stream_options"] = {"include_usage": True}
                response = client.chat.completions.create(
                    timeout=timeout or self.cfg.timeout, **kwargs
                )
                text = ""
                tool_calls: List[Dict[str, Any]] = []
                if stream_cb is not None:
                    for chunk in response:
                        if getattr(chunk, "usage", None):
                            usage = self._usage_dict(chunk.usage)
                        if not getattr(chunk, "choices", None):
                            continue
                        delta = chunk.choices[0].delta
                        piece = getattr(delta, "content", None)
                        if piece:
                            if first_token_ms is None:
                                first_token_ms = int((time.perf_counter() - started) * 1000)
                            chunks.append(piece)
                            try:
                                stream_cb(piece)
                            except Exception:
                                pass
                    text = "".join(chunks)
                else:
                    message = response.choices[0].message
                    text = message.content or ""
                    reasoning_parts.append(getattr(message, "reasoning_content", "") or "")
                    usage = self._usage_dict(response.usage)
                    for call in (getattr(message, "tool_calls", None) or []):
                        function = getattr(call, "function", None)
                        tool_calls.append(
                            {
                                "id": getattr(call, "id", ""),
                                "name": getattr(function, "name", "") if function else "",
                                "arguments": getattr(function, "arguments", "") if function else "",
                            }
                        )
                if not text.strip() and not (allow_tool_calls and tool_calls):
                    reasoning = "".join(reasoning_parts).strip()
                    raise LLMError(
                        "模型返回空 content"
                        + ("（只有 reasoning_content，可能被 max_tokens 截断）" if reasoning else "")
                    )
                latency = int((time.perf_counter() - started) * 1000)
                self.stats.record(
                    role, model_name, latency, usage, attempt, True, first_token_ms
                )
                result = LLMResult(
                    text=text,
                    model=model_name,
                    role=role,
                    latency_ms=latency,
                    usage=usage,
                    reasoning="".join(reasoning_parts),
                    attempts=attempt,
                    first_token_ms=first_token_ms,
                    tool_calls=tool_calls,
                )
                self._emit(
                    "llm_call",
                    role=role,
                    model=model_name,
                    latency_ms=latency,
                    tokens=usage.get("completion_tokens", 0),
                    attempts=attempt,
                )
                return result
            except Exception as exc:
                last_exc = exc
                latency = int((time.perf_counter() - started) * 1000)
                self.stats.record(role, model_name, latency, usage, attempt, False)
                self._emit(
                    "llm_error",
                    role=role,
                    model=model_name,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    attempt=attempt,
                )
                if attempt >= attempts:
                    break
                # 空 content 通常是思考 token 吃满了：下一次强制关闭思考并放大额度。
                if isinstance(exc, LLMError) and "空 content" in str(exc):
                    use_thinking = False
                    base_tokens = min(8192, int(base_tokens * 1.5))
                if "response_format" in str(exc).lower():
                    json_mode = False
                time.sleep(min(4.0, 0.6 * attempt))
        raise LLMError(f"模型调用失败（{model_name}, {attempts} 次）：{last_exc}")

    def chat_json(
        self,
        messages: List[Dict[str, Any]],
        *,
        role: str = "default",
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        thinking: Optional[bool] = None,
        retries: int = 1,
        repair: bool = True,
    ) -> Dict[str, Any]:
        result = self.chat(
            messages,
            role=role,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            thinking=thinking,
            retries=retries,
        )
        try:
            return result.json()
        except LLMError as exc:
            if not repair:
                raise
            self._emit("llm_repair", role=role, reason=str(exc)[:200])
            repair_messages = [
                {
                    "role": "system",
                    "content": "你是 JSON 修复器。把给定文本修复为严格合法的 JSON 对象，只输出 JSON，不要解释。",
                },
                {"role": "user", "content": (result.text or "")[:12000]},
            ]
            fixed = self.chat(
                repair_messages,
                role=role + ":repair",
                model=model,
                temperature=0,
                max_tokens=max_tokens or 2048,
                json_mode=True,
                thinking=False,
                retries=0,
            )
            return fixed.json()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _usage_dict(usage: Any) -> Dict[str, int]:
        if usage is None:
            return {}
        details = getattr(usage, "completion_tokens_details", None)
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "reasoning_tokens": int(getattr(details, "reasoning_tokens", 0) or 0)
            if details is not None
            else 0,
        }

    def _emit(self, event: str, **data: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, **data)
        except Exception:
            pass


def summarise_usage(stats: Dict[str, Any]) -> str:
    return (
        f"{stats.get('calls', 0)} 次调用 · "
        f"{stats.get('total_tokens', 0)} tokens · "
        f"{stats.get('avg_latency_ms', 0)}ms 平均"
    )


__all__ = ["LLM", "LLMError", "LLMResult", "LLMStats", "parse_json_text", "clean_json_text"]

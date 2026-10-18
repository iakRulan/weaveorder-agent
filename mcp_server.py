#!/usr/bin/env python3
"""WeaveOrder MCP server (stdio, newline-delimited JSON-RPC).

No third-party MCP SDK is required, so it runs on Python 3.9+ as well as 3.11+.
It exposes the same typed travel tools used by the orchestrator to any
MCP-capable harness (Claude Code, OpenClaw, Hermes, ...).

协议版本协商：客户端 initialize 时携带的 protocolVersion 若在支持列表内则
原样回显，否则回退到本服务端支持的最新版本（MCP 规范的生命周期要求）。

Run:
    .venv/bin/python mcp_server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
from core import VERSION  # noqa: E402

# 已核对语义并支持协议修订版；最新规范 2026-07-28 引入无状态核心与
# header 配置，主要影响 HTTP 传输，stdio 语义与本实现兼容。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "weaveorder-travel", "version": VERSION}
REGISTRY = agent.build_registry()


def negotiate_protocol_version(requested: Any) -> str:
    text = str(requested or "").strip()
    return text if text in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION


def tool_definitions() -> list[dict]:
    return [
        {
            "name": schema["name"],
            "description": schema["description"],
            "inputSchema": schema["parameters"],
        }
        for schema in REGISTRY.schemas()
    ]


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiate_protocol_version(requested),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tool_definitions()},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments 必须是对象")
            result = REGISTRY.call(str(name), arguments)
            text = json.dumps(result, ensure_ascii=False, default=str)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        else:
            response = handle(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

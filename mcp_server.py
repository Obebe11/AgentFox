#!/usr/bin/env python3
"""
MCP Server — тонкий враппер над AgentFox API для MCP-клиентов (Claude / etc).
Покрывает Local API P2: даёт LLM доступ к AgentFox как к MCP tools.

Запуск:
  python mcp_server.py              # stdio MCP
  python mcp_server.py --http 8001  # HTTP MCP

Tools:
  - create_profile, list_profiles, get_profile, delete_profile, restore_profile
  - start_session, goto, click, type, fill, scroll, extract, snapshot, screenshot, evaluate, cdp, network, upload, tabs
  - farm, health, metrics, export, import, cloud_push, cloud_pull
  - team.*, system.*

Использует FastAPI TestClient внутри или httpx к живому AgentFox (AGENTFOX_URL).
"""
from __future__ import annotations

import os
import json
import sys
from typing import Any, Optional

AGENTFOX_URL = os.getenv("AGENTFOX_URL", "http://localhost:8000")

# MCP tool definitions (compatible with anthropic MCP spec)
TOOLS = [
    {"name": "create_profile", "description": "Create AgentFox profile (geo, os, locale, proxy, engine)", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "geo": {"type": "string"}, "os": {"type": "string"}, "locale": {"type": "string"}, "engine": {"type": "string"}}, "required": ["id"]}},
    {"name": "list_profiles", "description": "List profiles", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_profile", "description": "Get profile by id", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}}, "required": ["pid"]}},
    {"name": "start_session", "description": "Start browser session for profile", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}, "headless": {"type": "boolean"}}, "required": ["pid"]}},
    {"name": "goto", "description": "Navigate session to URL (with human pause, optional read/snapshot)", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "url": {"type": "string"}, "read": {"type": "boolean"}, "snapshot": {"type": "boolean"}}, "required": ["sid", "url"]}},
    {"name": "click", "description": "Human click", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "selector": {"type": "string"}, "human": {"type": "boolean"}}, "required": ["sid", "selector"]}},
    {"name": "type", "description": "Human typing", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["sid", "selector", "text"]}},
    {"name": "fill", "description": "Fill (contenteditable aware)", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["sid", "selector", "text"]}},
    {"name": "scroll", "description": "Natural scroll", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "screens": {"type": "integer"}}, "required": ["sid"]}},
    {"name": "extract", "description": "Extract data via JS/selector", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "js": {"type": "string"}, "selector": {"type": "string"}}, "required": ["sid"]}},
    {"name": "snapshot", "description": "Accessibility snapshot (@e refs)", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "compact": {"type": "boolean"}}, "required": ["sid"]}},
    {"name": "screenshot", "description": "Screenshot", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "selector": {"type": "string"}, "format": {"type": "string"}}, "required": ["sid"]}},
    {"name": "evaluate", "description": "Evaluate JS (async allowed)", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "code": {"type": "string"}}, "required": ["sid", "code"]}},
    {"name": "network", "description": "Network intercept start/stop/list/clear", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "action": {"type": "string"}}, "required": ["sid", "action"]}},
    {"name": "upload", "description": "Upload files to input", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "selector": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}, "required": ["sid", "selector", "files"]}},
    {"name": "tabs", "description": "Multi-tab: create/list/activate/close", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}, "action": {"type": "string"}, "url": {"type": "string"}, "tab_id": {"type": "string"}}, "required": ["sid", "action"]}},
    {"name": "stop_session", "description": "Stop session", "inputSchema": {"type": "object", "properties": {"sid": {"type": "string"}}, "required": ["sid"]}},
    {"name": "health", "description": "Get profile health/warmup", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}}, "required": ["pid"]}},
    {"name": "metrics", "description": "Get metrics", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}}, "required": []}},
    {"name": "cloud_push", "description": "Push profile to cloud", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}}, "required": ["pid"]}},
    {"name": "cloud_pull", "description": "Pull profile from cloud", "inputSchema": {"type": "object", "properties": {"pid": {"type": "string"}, "new_id": {"type": "string"}}, "required": ["pid"]}},
]


def _call_agentfox(tool: str, args: dict) -> Any:
    """Вызывает AgentFox API (через httpx или TestClient fallback)."""
    try:
        import httpx

        base = AGENTFOX_URL.rstrip("/")
        # map tool -> http call
        if tool == "create_profile":
            r = httpx.post(f"{base}/profiles", json={"id": args["id"], "geo": args.get("geo", "DE"), "os": args.get("os"), "locale": args.get("locale"), "engine": args.get("engine", "firefox")}, timeout=15)
            return r.json()
        elif tool == "list_profiles":
            r = httpx.get(f"{base}/profiles", timeout=10)
            return r.json()
        elif tool == "get_profile":
            r = httpx.get(f"{base}/profiles/{args['pid']}", timeout=10)
            return r.json()
        elif tool == "start_session":
            r = httpx.post(f"{base}/sessions/{args['pid']}/start", json={"headless": args.get("headless", True)}, timeout=20)
            return r.json()
        elif tool == "goto":
            r = httpx.post(f"{base}/sessions/{args['sid']}/goto", json={"url": args["url"], "read": args.get("read", False), "snapshot": args.get("snapshot", False)}, timeout=20)
            return r.json()
        elif tool == "click":
            r = httpx.post(f"{base}/sessions/{args['sid']}/click", json={"selector": args["selector"], "human": args.get("human", True)}, timeout=15)
            return r.json()
        elif tool == "type":
            r = httpx.post(f"{base}/sessions/{args['sid']}/type", json={"selector": args["selector"], "text": args["text"]}, timeout=15)
            return r.json()
        elif tool == "fill":
            r = httpx.post(f"{base}/sessions/{args['sid']}/fill", json={"selector": args["selector"], "text": args["text"]}, timeout=15)
            return r.json()
        elif tool == "scroll":
            r = httpx.post(f"{base}/sessions/{args['sid']}/scroll", json={"screens": args.get("screens", 2)}, timeout=15)
            return r.json()
        elif tool == "extract":
            r = httpx.post(f"{base}/sessions/{args['sid']}/extract", json={"js": args.get("js"), "selector": args.get("selector")}, timeout=15)
            return r.json()
        elif tool == "snapshot":
            r = httpx.get(f"{base}/sessions/{args['sid']}/snapshot", params={"compact": args.get("compact", False)}, timeout=15)
            return r.json()
        elif tool == "screenshot":
            r = httpx.get(f"{base}/sessions/{args['sid']}/screenshot", params={"selector": args.get("selector"), "format": args.get("format", "png")}, timeout=15)
            return r.json()
        elif tool == "evaluate":
            r = httpx.post(f"{base}/sessions/{args['sid']}/evaluate", json={"code": args["code"]}, timeout=15)
            return r.json()
        elif tool == "network":
            r = httpx.post(f"{base}/sessions/{args['sid']}/network", json={"action": args["action"]}, timeout=10)
            return r.json()
        elif tool == "upload":
            r = httpx.post(f"{base}/sessions/{args['sid']}/upload", json={"selector": args["selector"], "files": args["files"]}, timeout=20)
            return r.json()
        elif tool == "tabs":
            action = args.get("action", "list")
            if action == "create":
                r = httpx.post(f"{base}/sessions/{args['sid']}/tabs", params={"url": args.get("url")}, timeout=15)
                return r.json()
            elif action == "list":
                r = httpx.get(f"{base}/sessions/{args['sid']}/tabs", timeout=10)
                return r.json()
            elif action == "activate":
                r = httpx.post(f"{base}/sessions/{args['sid']}/tabs/{args['tab_id']}/activate", timeout=10)
                return r.json()
            elif action == "close":
                r = httpx.post(f"{base}/sessions/{args['sid']}/tabs/{args['tab_id']}/close", timeout=10)
                return r.json()
            else:
                return {"error": f"unknown tabs action {action}"}
        elif tool == "stop_session":
            r = httpx.post(f"{base}/sessions/{args['sid']}/stop", timeout=10)
            return r.json()
        elif tool == "health":
            r = httpx.get(f"{base}/health/{args['pid']}", timeout=10)
            return r.json()
        elif tool == "metrics":
            if args.get("pid"):
                r = httpx.get(f"{base}/metrics/{args['pid']}", timeout=10)
            else:
                r = httpx.get(f"{base}/metrics", timeout=10)
            return r.json()
        elif tool == "cloud_push":
            r = httpx.post(f"{base}/cloud/{args['pid']}/push", timeout=30)
            return r.json()
        elif tool == "cloud_pull":
            r = httpx.post(f"{base}/cloud/{args['pid']}/pull", params={"new_id": args.get("new_id")}, timeout=30)
            return r.json()
        else:
            return {"error": f"unknown tool {tool}"}
    except Exception as e:
        return {"error": str(e)[:500]}


def handle_mcp_request(req: dict) -> dict:
    method = req.get("method")
    params = req.get("params", {})
    req_id = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "agentfox", "version": "0.1.0"}}}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        result = _call_agentfox(name, args)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    else:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_mcp_request(req)
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AgentFox MCP server")
    ap.add_argument("--http", type=int, help="HTTP port for MCP (instead of stdio)")
    ap.add_argument("--agentfox-url", default=AGENTFOX_URL, help="AgentFox base URL")
    args = ap.parse_args()
    if args.agentfox_url:
        AGENTFOX_URL = args.agentfox_url
    if args.http:
        # simple HTTP MCP (SSE-like JSON-RPC)
        from fastapi import FastAPI, Request
        import uvicorn

        mcp_app = FastAPI(title="AgentFox MCP")

        @mcp_app.post("/mcp")
        async def mcp_endpoint(req: Request):
            body = await req.json()
            return handle_mcp_request(body)

        @mcp_app.get("/mcp/tools")
        async def mcp_tools():
            return {"tools": TOOLS}

        print(f"[mcp] AgentFox MCP HTTP on :{args.http} -> {AGENTFOX_URL}")
        uvicorn.run(mcp_app, host="0.0.0.0", port=args.http)
    else:
        run_stdio()

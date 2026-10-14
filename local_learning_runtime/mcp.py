"""Small stdio MCP retrieval adapter, no network or write-promotion tools."""

import json
import sys
from pathlib import Path
from typing import Any, Dict

from .core import Store, RuntimeErrorBase, canonical, require
from .learning import search, status, run_registered
from .discovery import discover

TOOLS = [
    {"name": "local_learning_search",
     "description": "Find relevant private local project knowledge before planning. Identify workspace "
                    "from actual cwd. Return bounded scoped summaries, conditions, revision and evidence "
                    "IDs. Runtime is published-observation authority; Userbot is legacy background.",
     "inputSchema": {"type": "object", "properties": {
         "cwd": {"type": "string"}, "query": {"type": "string"},
         "session": {"type": "string"}, "conditions": {"type": "object"}},
         "required": ["cwd", "query", "session"], "additionalProperties": False}},
    {"name": "local_learning_validators",
     "description": "Discover preauthorized local App runtime validators for a task. Returns only "
                    "names and scopes, never runs an App or adds authorization.",
     "inputSchema": {"type": "object", "properties": {
         "cwd": {"type": "string"}, "session": {"type": "string"},
         "platform": {"type": "string", "enum": ["android", "ios"]}},
         "required": ["cwd", "session"], "additionalProperties": False}},
    {"name": "local_learning_validate",
     "description": "Execute one previously user-approved bounded local validator. Automatically "
                    "captures minimized evidence, verifies predicates, publishes under local policy "
                    "and updates the graph in the same workflow. No extra save request needed. "
                    "Cannot register validators or expand their action scope. Include actual session/task.",
     "inputSchema": {"type": "object", "properties": {
         "cwd": {"type": "string"}, "platform": {"type": "string", "enum": ["android", "ios"]},
         "runner": {"type": "string"}, "session": {"type": "string"},
         "task": {"type": "string"}, "milestone": {"type": "string"}},
         "required": ["cwd", "runner", "session", "task", "milestone"],
         "additionalProperties": False}},
    {"name": "local_learning_status",
     "description": "Explain task capture, validation, publication and missing evidence.",
     "inputSchema": {"type": "object", "properties": {"task_run": {"type": "string"}},
                     "additionalProperties": False}}
]


def respond(store: Store, message: Dict[str, Any]) -> Dict[str, Any]:
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-learning-runtime", "version": "0.2.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message["params"]
        args = params.get("arguments", {})
        if params["name"] == "local_learning_search":
            result = search(store, Path(args["cwd"]), args["query"], args["session"],
                            args.get("conditions"))
        elif params["name"] == "local_learning_status":
            result = status(store, args.get("task_run"))
        elif params["name"] in ("local_learning_validators", "local_learning_validate"):
            found = discover(store, Path(args["cwd"]), args["session"], args.get("platform"))
            require(found["status"] == "DISCOVERED", "Validator workspace discovery: " + found["status"])
            workspace = found["workspace"]
            if params["name"] == "local_learning_validate":
                result = run_registered(store, workspace, args["runner"], args["session"],
                                        args["task"], args["milestone"])
            else:
                path = store.home / "runners.json"
                records = json.loads(path.read_text()) if path.exists() else {}
                result = {"workspace": workspace, "validators": [
                    {"runner": name[len(workspace) + 1:], "title": item["title"],
                     "scope": "Exact preapproved executable and checks; at most 30 seconds"}
                    for name, item in records.items() if name.startswith(workspace + ":")][:16]}
        else:
            raise RuntimeErrorBase("Unknown MCP tool")
        return {"content": [{"type": "text", "text": canonical(result)}],
                "isError": bool(result.get("error"))}
    raise RuntimeErrorBase("Unsupported MCP method")


def serve(store: Store) -> None:
    while True:
        line = sys.stdin.buffer.readline(256_001)
        if not line:
            return
        message = None
        try:
            require(len(line) <= 256_000, "MCP request too large")
            message = json.loads(line)
            if "id" not in message:
                continue
            result = respond(store, message)
            output = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except (RuntimeErrorBase, OSError, ValueError, KeyError, TypeError) as exc:
            output = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                      "error": {"code": -32602, "message": str(exc)}}
        sys.stdout.write(canonical(output) + "\n")
        sys.stdout.flush()

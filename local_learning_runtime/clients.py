"""Bounded lifecycle adapters. No transcript reads, model calls or app actions."""

import json
import re
import subprocess
import sys
import shlex
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .core import Store, canonical, digest, now, require
from .discovery import discover
from .learning import begin_task, ingest, load_policy, privacy_key, search, status
from .privacy import sanitize
from .context import packet


def hook(store: Store, client: str, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    require(len(canonical(payload).encode()) <= 256_000, "Hook input exceeds 256000 bytes")
    session = payload.get("sessionId") or payload.get("session_id") or payload.get("thread-id")
    cwd = payload.get("cwd")
    require(isinstance(session, str) and isinstance(cwd, str),
            "Client did not supply stable session ID/cwd; event not recorded")
    prompt = payload.get("prompt") or payload.get("initialPrompt") or payload.get("initial_prompt")
    context = None
    if isinstance(prompt, str) and prompt.strip() and event in ("sessionStart", "userPromptSubmitted"):
        context = packet(store, Path(cwd), prompt[:2000], session, level=2)
        selected_workspace = context.get("workspace") or ""
        platform = "android" if selected_workspace.endswith("/android") else None
        if selected_workspace.endswith("/ios"):
            platform = "ios"
        found = discover(store, Path(cwd), session, platform)
    else:
        found = discover(store, Path(cwd), session)
        if found["status"] != "DISCOVERED":
            # Turn-ending hooks use the authenticated session's last discovery, not a guessed platform.
            previous = store.db.execute("SELECT workspace FROM task_runs WHERE session=? "
                                        "ORDER BY updated_at DESC LIMIT 1", (session,)).fetchone()
            if previous and previous[0]:
                found = discover(store, Path(cwd), session, store.workspace(previous[0])["platform"])
    if found["status"] != "DISCOVERED":
        if client == "codex-hooks" and event in ("sessionStart", "userPromptSubmitted"):
            return {"hookSpecificOutput": {
                "hookEventName": "SessionStart" if event == "sessionStart" else "UserPromptSubmit",
                "additionalContext": canonical(found)}}
        return {"additionalContext": canonical(found)} if event == "sessionStart" else {}
    workspace = found["workspace"]
    load_policy(store, workspace)
    if event == "sessionStart":
        if context is None:
            context = {"workspace": workspace, "revision": found["revision"],
                       "task_types": found["task_types"], "claim_ids": found["claim_ids"],
                       "notice": "Use local_learning_search MCP for the actual task; runtime is the "
                                 "published observation authority, Userbot is legacy background."}
    if event == "userPromptSubmitted" and isinstance(prompt, str):
        title, _ = sanitize(prompt[:2000], privacy_key(store))
        task = "turn-" + digest(canonical([session, payload.get("turn_id"),
                                           payload.get("timestamp"), prompt]).encode())[:24]
        begin_task(store, workspace, session, task, title)
        policy = load_policy(store, workspace)
        if client == "codex-hooks" and policy.get("local_first_readonly") is True:
            from .solutions import route, execute_solution, get_solution, all_steps
            decision = route(store, Path(cwd), prompt.strip(), session)
            if decision["status"] == "LOCAL_READY":
                record = get_solution(store, workspace, decision["solution"])
                if all(step["op"] in ("inspect_file", "select", "branch") for step in all_steps(record["spec"])):
                    result = execute_solution(store, workspace, decision["solution"], session,
                                              task, decision["parameters"], {})
                    if result["status"] == "LOCAL_SOLVED":
                        with store.transaction():
                            body = {"solution": decision["solution"], "result": result,
                                    "model_request_blocked": True, "host_pid": os.getpid(),
                                    "adapter_api": "0.4"}
                            store.db.execute("INSERT OR IGNORE INTO client_receipts VALUES (?,?,?,?,?,?,?)",
                                             (digest(canonical([session, task, body]).encode()),
                                              client, session, "localSolved", workspace, canonical(body), now()))
                        readable = "\n".join(str(key) + ": " + (str(value) if type(value) in
                            (str, int, bool) else canonical(value)) for key, value in result["result"].items())
                        return {"decision": "block", "reason": "Solved locally · 已在本地完成\n" + readable,
                                "systemMessage": "Local solution completed; no model request required."}
        if client != "codex-hooks":
            context = None
    if event == "postToolUse":
        # Persist only a minimal diagnostic; raw tool responses and user content are not logged.
        task_row = store.db.execute("SELECT * FROM task_runs WHERE session=? AND workspace=? "
                                   "ORDER BY updated_at DESC LIMIT 1", (session, workspace)).fetchone()
        tool_name = payload.get("toolName", payload.get("tool_name", "unknown"))
        if task_row is not None and "local_learning_validate" not in tool_name:
            result = payload.get("toolResult", payload.get("tool_response", {}))
            if not isinstance(result, dict):
                result = {"resultType": "unknown"}
            result_type = result.get("resultType", "unknown")
            text = result.get("textResultForLlm", "")
            assignments = []
            if isinstance(text, str) and len(text.encode()) <= 64_000:
                from .privacy import SENSITIVE_KEY, marker
                for number, line in enumerate(text.splitlines(), 1):
                    match = re.search(r"\b[\w.-]+\[\d+\]\s+(.{1,200}?)\s+=\s+(.{1,1000})$", line)
                    if match:
                        expression, value = match.groups()
                        if SENSITIVE_KEY.search(expression):
                            value = marker(value, privacy_key(store))
                        assignments.append({"line": number, "expression": expression, "value": value})
                        if len(assignments) == 16:
                            break
            event_body = {"schema": "llr.milestone.v1", "workspace": workspace, "session": session,
                          "task": task_row["task"],
                          "milestone": "tool-" + digest(canonical(
                              [payload.get("timestamp"), payload.get("toolName"),
                               payload.get("tool_name"), result_type]).encode())[:24],
                          "title": task_row["title"],
                          "kind": "runtime_observation" if assignments else "hypothesis",
                          "outcome": "FAIL" if result_type == "failure" else "INCONCLUSIVE", "conditions": {},
                          "operations": [{"tool": payload.get("toolName", payload.get("tool_name", "unknown"))}],
                          "observation": {"tool_transport_result": result_type,
                                          "runtime_assertions_supplied": False,
                                          "debugger_assignments": assignments},
                          "checks": [], "anchors": [],
                          "uncertainties": ["A successful tool transport is not an app verification."]}
            ingest(store, event_body)
    if event in ("agentStop", "sessionEnd", "notify"):
        # End-of-turn is not proof of task success. Preserve completed milestones separately.
        with store.transaction():
            store.db.execute("UPDATE task_runs SET state='AWAITING_EVIDENCE',updated_at=? "
                             "WHERE session=? AND state='in_progress'", (now(), session))
    safe_receipt = {"state_home": str(store.home), "workspace": workspace,
                    "revision": store.revision(), "event": event,
                    "host_pid": os.getpid(), "adapter_api": "0.4",
                    "context_injected": context is not None,
                    "task_state": [item["state"] for item in status(store)["tasks"]
                                   if item["session"] == session]}
    with store.transaction():
        receipt_id = digest(canonical([client, session, event, payload.get("timestamp"),
                                       payload.get("turn-id"), safe_receipt]).encode())
        store.db.execute("INSERT OR IGNORE INTO client_receipts VALUES (?,?,?,?,?,?,?)",
                         (receipt_id, client, session, event, workspace, canonical(safe_receipt), now()))
    if client == "codex-hooks" and context is not None:
        return {"hookSpecificOutput": {"hookEventName":
                "SessionStart" if event == "sessionStart" else "UserPromptSubmit",
                "additionalContext": canonical(context)}}
    if client == "codex-hooks" and event == "agentStop":
        return {"systemMessage": "Local learning task status: " + canonical(safe_receipt["task_state"])}
    return {"additionalContext": canonical(context)} if context is not None else {}


def install_codex_hooks(store: Store, root: Path, profile: Path, approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit permission needed to install trusted-review Codex hooks")
    command = " ".join(shlex.quote(arg) for arg in (
        sys.executable, str(launcher_at(root)), "--home", str(store.home),
        "hook", "--client", "codex-hooks"))
    events = {"SessionStart": "sessionStart", "UserPromptSubmit": "userPromptSubmitted",
              "PostToolUse": "postToolUse", "Stop": "agentStop"}
    config = {"description": "Local knowledge: bounded recall, task receipts and status. "
                             "No transcript upload, model call or automatic App action.",
              "hooks": {name: [{"hooks": [{"type": "command", "command": command + " --event " + event,
                                          "timeout": 10, "additionalContextLimit": 2000}
                                         if name in ("SessionStart", "UserPromptSubmit") else {
                                          "type": "command", "command": command + " --event " + event,
                                          "timeout": 10}]}] for name, event in events.items()}}
    target = profile.expanduser() / "hooks.json"
    if target.exists():
        existing = json.loads(target.read_text())
        if existing != config:
            require(not existing.get("hooks"), "Existing hook handlers require a reviewed merge")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2))
    return {"status": "INSTALLED_REQUIRES_CLIENT_TRUST", "path": str(target),
            "notice": "Review and trust via Codex hook UI. No trust bypass flag or database edit is used."}


def install_copilot(store: Store, root: Path, profile: Path, approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit permission needed to install local client hooks")
    launcher = launcher_at(root)
    directory = profile.expanduser() / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "local-learning-runtime.json"
    events = ("sessionStart", "userPromptSubmitted", "postToolUse", "agentStop", "sessionEnd")
    config = {"version": 1, "hooks": {
        event: [{"type": "command", "exec": sys.executable,
                 "args": [str(launcher), "--home", str(store.home), "hook",
                          "--client", "copilot", "--event", event], "timeoutSec": 15}]
        for event in events}}
    if target.exists():
        require(json.loads(target.read_text()) == config,
                "Existing hook config differs; review before replacing")
    else:
        target.write_text(json.dumps(config, indent=2), encoding="utf-8")
    mcp_path = profile.expanduser() / "mcp-config.json"
    mcp_config = json.loads(mcp_path.read_text()) if mcp_path.exists() else {"mcpServers": {}}
    entry = {"type": "local", "command": sys.executable,
             "args": [str(launcher), "--home", str(store.home), "mcp"], "tools": ["*"]}
    previous_mcp = mcp_config.setdefault("mcpServers", {}).get("local_learning")
    require(previous_mcp is None or previous_mcp == entry, "Existing MCP adapter differs")
    mcp_config["mcpServers"]["local_learning"] = entry
    mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
    mcp_path.chmod(0o600)
    return {"status": "INSTALLED_NOT_YET_ATTESTED", "path": str(target),
            "events": list(events), "mcp_config": str(mcp_path),
            "notice": "Actual harness must load and invoke these hooks."}


def install_codex(store: Store, root: Path, profile: Path, approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit permission needed to install Codex adapters")
    profile = profile.expanduser().resolve()
    config = profile / "config.toml"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = launcher_at(root)
    text = config.read_text() if config.exists() else ""
    marker = "[mcp_servers.local_learning]"
    block = (marker + "\ncommand = " + json.dumps(sys.executable) + "\nargs = "
             + json.dumps([str(launcher), "--home", str(store.home), "mcp"]) + "\n")
    notify = re.search(r"(?m)^notify\s*=\s*(\[[^\n]*\])\s*$", text)
    previous = json.loads(notify.group(1)) if notify else []
    chain_file = store.home / "codex-notify-chain.json"
    new_notify = [sys.executable, str(launcher), "--home", str(store.home), "notify"]
    backup = store.home / "client-backups"
    backup.mkdir(exist_ok=True, mode=0o700)
    original_path = backup / ("codex-" + digest(text.encode()) + ".toml")
    if not original_path.exists():
        original_path.write_text(text)
        original_path.chmod(0o600)
    if previous != new_notify:
        require(all(isinstance(arg, str) for arg in previous), "Unsupported existing notify format")
        chain_file.write_text(canonical(previous), encoding="utf-8")
        chain_file.chmod(0o600)
        line = "notify = " + json.dumps(new_notify)
        if notify:
            text = text[:notify.start()] + line + text[notify.end():]
        else:
            text = line + "\n" + text
    if marker not in text:
        text += "\n" + block
    else:
        require(block.strip() in text, "Existing local_learning MCP differs; manual merge needed")
    config.write_text(text)
    return {"status": "INSTALLED_NOT_YET_ATTESTED", "profile": str(profile),
            "backup": str(original_path),
            "capabilities": ["MCP task search", "chained end-of-turn notification"],
            "limits": "Codex 0.85 has no validated tool lifecycle hook; controlled runners publish "
                      "automatically, other tool outputs are not captured by notify."}


def codex_notify(store: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = hook(store, "codex", "notify", payload)
    chain = store.home / "codex-notify-chain.json"
    if chain.exists():
        argv = json.loads(chain.read_text())
        if argv:
            # Existing caller-owned notification behavior is preserved, not intercepted silently.
            completed = subprocess.run(argv + [canonical(payload)], timeout=15, capture_output=True)
            require(completed.returncode == 0, "Existing Codex notify chain failed")
    return result


def launcher_at(root: Path) -> Path:
    source = root.resolve() / "tools" / "llr.py"
    if source.is_file():
        return source
    installed = Path(__file__).resolve().parent / "entry.py"
    require(installed.is_file(), "Runtime entry point missing")
    return installed


def setup_client(store: Store, workspace: str, root: Path, platform: str,
                 client: str, profile: Optional[Path], approve: bool,
                 local_first: bool = False) -> Dict[str, Any]:
    require(approve, "Explicit approval required to register a project and install client adapter")
    require(client in ("copilot", "codex", "codex-hooks"), "Unsupported installer client")
    from .learning import authorize
    store.register(workspace, root, platform)
    authorize(store, [workspace], True)
    target = profile or Path.home() / (".copilot" if client == "copilot" else ".codex")
    runtime_root = Path(__file__).resolve().parents[1]
    installer = {"copilot": install_copilot, "codex": install_codex,
                 "codex-hooks": install_codex_hooks}[client]
    result = installer(store, runtime_root, target, True)
    if local_first:
        require(client == "codex-hooks", "Zero-model native gate only available through supported Codex hooks")
        path = store.home / "automation.json"
        policy = json.loads(path.read_text())
        policy["local_first_readonly"] = True
        path.write_text(canonical(policy))
    result.update({"workspace": workspace, "state_home": str(store.home),
                   "host": "on-demand shared service", "app_scope_expanded": False,
                   "local_first_readonly": local_first})
    return result


def doctor(store: Store) -> Dict[str, Any]:
    receipts = [dict(row) for row in store.db.execute(
        "SELECT client,event,session,workspace,created_at FROM client_receipts "
        "ORDER BY created_at DESC LIMIT 12")]
    return {"status": "OK", "state_home": str(store.home), "host_pid": os.getpid(),
            "registered_workspaces": [dict(row) for row in store.db.execute("SELECT * FROM workspaces")],
            "recent_handoffs": receipts,
            "client_trust": "Must be verified in the actual client UI; file presence is not invocation.",
            "local_only_default": True, "model_gateway_configured": (store.home / "model-gateway.json").exists(),
            "notice": "Configured gateway is not independently proven to enforce provider billing caps."}

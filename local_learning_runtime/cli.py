"""Client-neutral JSON command interface."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional, Sequence

from .core import MAX_EVIDENCE_BYTES, RuntimeErrorBase, Store, file_digest, require, workspace_file
from .devices import android_capture, ios_capture, run_android_recipe
from .knowledge import catalog, export_portal, import_graph
from .acceptance import android_acceptance
from .discovery import discover, register_alias
from .learning import (authorize, ingest, status, graph, search, register_runner, run_registered,
                       import_debugger_log, forget_workspace)
from .learning import retry_milestone
from .clients import (hook, install_copilot, install_codex, install_codex_hooks,
                      codex_notify, setup_client, doctor)
from .mcp import serve
from .probes import android_node
from .solutions import propose_solution, execute_solution, route, invalidate_solution
from .context import packet, define_theme, refresh_context
from .missions import create_mission, mission_status, control, tick
from .preferences import save_preference
from .lifecycle import configure as configure_memory, assess as memory_health, maintenance


def read_json(path: str) -> Any:
    file = Path(path)
    require(file.stat().st_size <= 256_000, "JSON input exceeds 256000 bytes")
    return json.loads(file.read_text(encoding="utf-8"))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Offline evidence-backed learning runtime")
    result.add_argument("--home", type=Path,
                        default=Path(os.environ.get("LLR_HOME", "~/.local-learning-runtime")).expanduser())
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    memory_policy = commands.add_parser("memory-policy")
    memory_policy.add_argument("spec")
    memory_policy.add_argument("--workspace", required=True)
    memory_policy.add_argument("--kind", choices=["claim", "solution"], required=True)
    memory_policy.add_argument("--id", required=True)
    memory_policy.add_argument("--approve", action="store_true")
    memory = commands.add_parser("memory-health")
    memory.add_argument("--workspace", required=True)
    memory.add_argument("--kind", choices=["claim", "solution"], required=True)
    memory.add_argument("--id", required=True)
    memory.add_argument("--case-key")
    maintenance_parser = commands.add_parser("maintenance")
    maintenance_parser.add_argument("--workspace", required=True)
    maintenance_parser.add_argument("--limit", type=int, default=50)
    maintenance_parser.add_argument("--cursor", type=int, default=0)
    setup = commands.add_parser("setup")
    setup.add_argument("workspace")
    setup.add_argument("--root", type=Path, required=True)
    setup.add_argument("--platform", choices=["android", "ios", "generic"], required=True)
    setup.add_argument("--client", choices=["copilot", "codex", "codex-hooks"], required=True)
    setup.add_argument("--profile", type=Path)
    setup.add_argument("--local-first-readonly", action="store_true")
    setup.add_argument("--approve", action="store_true")
    commands.add_parser("host-serve")
    commands.add_parser("host-status")
    commands.add_parser("host-stop")
    commands.add_parser("doctor")
    preference = commands.add_parser("preference")
    preference.add_argument("id")
    preference.add_argument("--text", required=True)
    preference.add_argument("--kind", choices=["explicit", "inferred"], required=True)
    preference.add_argument("--expected-version", type=int, required=True)
    preference.add_argument("--workspace")
    preference.add_argument("--approve", action="store_true")
    context = commands.add_parser("context")
    context.add_argument("--cwd", type=Path, default=Path.cwd())
    context.add_argument("--query", default="")
    context.add_argument("--session", required=True)
    context.add_argument("--level", type=int, choices=[0, 1, 2, 3], default=2)
    context.add_argument("--node")
    context.add_argument("--revision")
    context.add_argument("--cursor", type=int, default=0)
    context.add_argument("--budget", type=int, default=6000)
    context_refresh = commands.add_parser("context-refresh")
    context_refresh.add_argument("--session", required=True)
    theme = commands.add_parser("define-theme")
    theme.add_argument("spec")
    theme.add_argument("--workspace", required=True)
    theme.add_argument("--approve", action="store_true")
    proposal = commands.add_parser("solution-propose")
    proposal.add_argument("spec")
    proposal.add_argument("--expected-version", type=int, required=True)
    proposal.add_argument("--approve", action="store_true")
    router = commands.add_parser("route")
    router.add_argument("--cwd", type=Path, default=Path.cwd())
    router.add_argument("--query", required=True)
    router.add_argument("--session", required=True)
    router.add_argument("--parameters")
    router.add_argument("--conditions")
    for name in ("solution-verify", "solution-execute"):
        execution = commands.add_parser(name)
        execution.add_argument("solution")
        execution.add_argument("--workspace", required=True)
        execution.add_argument("--session", required=True)
        execution.add_argument("--invocation", required=True)
        execution.add_argument("--parameters", required=True)
        execution.add_argument("--conditions", required=True)
        execution.add_argument("--approve", action="store_true")
    invalidation = commands.add_parser("solution-invalidate")
    invalidation.add_argument("solution")
    invalidation.add_argument("--workspace", required=True)
    invalidation.add_argument("--version", type=int, required=True)
    invalidation.add_argument("--state", choices=["stale", "conflicted", "superseded"], required=True)
    invalidation.add_argument("--approve", action="store_true")
    mission = commands.add_parser("mission-create")
    mission.add_argument("spec")
    for name in ("mission-control", "mission-status", "mission-tick"):
        job = commands.add_parser(name)
        job.add_argument("mission")
        if name == "mission-control":
            job.add_argument("action", choices=["approve", "start", "pause", "resume", "cancel"])
            job.add_argument("--approve", action="store_true")
    retry = commands.add_parser("retry-milestone")
    retry.add_argument("event_id")
    probe = commands.add_parser("probe-android-node")
    probe.add_argument("--device", required=True)
    probe.add_argument("--app", required=True)
    probe.add_argument("--resource-id", required=True)
    probe.add_argument("--adb", default="adb")
    probe.add_argument("--approve", action="store_true")
    alias = commands.add_parser("register-alias")
    alias.add_argument("--workspace", required=True)
    alias.add_argument("--root", type=Path, required=True)
    alias.add_argument("--require-platform", action="store_true")
    alias.add_argument("--approve", action="store_true")
    debugger = commands.add_parser("import-debugger")
    debugger.add_argument("source", type=Path)
    debugger.add_argument("--workspace", required=True)
    debugger.add_argument("--session", required=True)
    debugger.add_argument("--task", required=True)
    debugger.add_argument("--title", required=True)
    debugger.add_argument("--approve", action="store_true")
    forgotten = commands.add_parser("forget-workspace")
    forgotten.add_argument("--workspace", required=True)
    forgotten.add_argument("--approve", action="store_true")
    policy = commands.add_parser("authorize-local")
    policy.add_argument("--workspace", action="append", required=True)
    policy.add_argument("--approve", action="store_true")
    incoming = commands.add_parser("ingest")
    incoming.add_argument("event")
    states = commands.add_parser("task-status")
    states.add_argument("--task-run")
    projection = commands.add_parser("graph")
    projection.add_argument("--workspace", required=True)
    lookup = commands.add_parser("search")
    lookup.add_argument("--cwd", type=Path, default=Path.cwd())
    lookup.add_argument("--query", required=True)
    lookup.add_argument("--session", required=True)
    lookup.add_argument("--conditions")
    registration = commands.add_parser("register-runner")
    registration.add_argument("name")
    registration.add_argument("spec")
    registration.add_argument("--workspace", required=True)
    registration.add_argument("--approve", action="store_true")
    runner = commands.add_parser("run-validator")
    runner.add_argument("name")
    runner.add_argument("--workspace", required=True)
    runner.add_argument("--session", required=True)
    runner.add_argument("--task", required=True)
    runner.add_argument("--milestone", required=True)
    lifecycle = commands.add_parser("hook")
    lifecycle.add_argument("--client", choices=["copilot", "vscode-local", "codex-hooks"], required=True)
    lifecycle.add_argument("--event", required=True)
    installation = commands.add_parser("install-client")
    installation.add_argument("client", choices=["copilot", "codex", "codex-hooks"])
    installation.add_argument("--runtime-root", type=Path, required=True)
    installation.add_argument("--profile", type=Path, required=True)
    installation.add_argument("--approve", action="store_true")
    commands.add_parser("mcp")
    notify = commands.add_parser("notify")
    notify.add_argument("payload")
    discovery = commands.add_parser("discover")
    discovery.add_argument("--cwd", type=Path, default=Path.cwd())
    discovery.add_argument("--session", required=True)
    discovery.add_argument("--platform", choices=["android", "ios", "generic"])
    register = commands.add_parser("register")
    register.add_argument("workspace")
    register.add_argument("--root", type=Path, required=True)
    register.add_argument("--platform", choices=["android", "ios", "generic"], required=True)
    anchor = commands.add_parser("anchor")
    anchor.add_argument("workspace")
    anchor.add_argument("path")
    observe = commands.add_parser("observe")
    observe.add_argument("--workspace", required=True)
    observe.add_argument("--event", required=True)
    observe.add_argument("--conditions", required=True)
    observe.add_argument("--file", type=Path, required=True)
    observe.add_argument("--type", choices=["application/json", "application/xml", "image/png"],
                         required=True)
    propose = commands.add_parser("propose")
    propose.add_argument("spec")
    for name in ("verify", "accept"):
        command = commands.add_parser(name)
        command.add_argument("candidate")
        if name == "accept":
            command.add_argument("--expected-version", type=int, required=True)
            command.add_argument("--expected-revision", required=True,
                                 help="Use 'none' only for initial publication")
            command.add_argument("--approve", action="store_true")
    recall = commands.add_parser("recall")
    recall.add_argument("task")
    recall.add_argument("--session", required=True)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--session", required=True)
    commands.add_parser("questions")
    intake = commands.add_parser("import-graph")
    intake.add_argument("source", type=Path)
    intake.add_argument("--workspace", required=True)
    intake.add_argument("--node", action="append", required=True)
    intake.add_argument("--approve", action="store_true")
    cards = commands.add_parser("catalog")
    cards.add_argument("--workspace", required=True)
    portal = commands.add_parser("export-portal")
    portal.add_argument("--workspace", required=True)
    portal.add_argument("--output", type=Path, required=True)
    acceptance = commands.add_parser("acceptance-android")
    acceptance.add_argument("--workspace", required=True)
    acceptance.add_argument("--device", required=True)
    acceptance.add_argument("--app", required=True)
    acceptance.add_argument("--resource-id", required=True)
    acceptance.add_argument("--context", required=True)
    acceptance.add_argument("--output", type=Path, required=True)
    acceptance.add_argument("--approve", action="store_true")
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("claim")
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--lifecycle", choices=["stale", "conflicted"], required=True)
    invalidate.add_argument("--expected-version", type=int, required=True)
    invalidate.add_argument("--expected-revision", required=True)
    invalidate.add_argument("--approve", action="store_true")
    recipe = commands.add_parser("run-android")
    recipe.add_argument("recipe")
    recipe.add_argument("--approve", action="store_true")
    recipe.add_argument("--session")
    recipe.add_argument("--task")
    packet = commands.add_parser("learning-packet")
    packet.add_argument("task")
    packet.add_argument("--question", required=True)
    for name in ("capture-android", "capture-ios"):
        capture = commands.add_parser(name)
        capture.add_argument("--workspace", required=True)
        capture.add_argument("--device", required=True)
        capture.add_argument("--app", required=True)
        capture.add_argument("--context", required=True)
        capture.add_argument("--approve", action="store_true")
    unlock = commands.add_parser("unlock-device")
    unlock.add_argument("device")
    unlock.add_argument("--approve", action="store_true")
    return result


def dispatch(store: Store, args: argparse.Namespace) -> Any:
    if args.command == "init":
        return {"status": "ok", "home": str(store.home), "model_calls": 0}
    if args.command == "memory-policy":
        return configure_memory(store, args.workspace, args.kind, args.id, read_json(args.spec), args.approve)
    if args.command == "memory-health":
        return memory_health(store, args.workspace, args.kind, args.id, args.case_key)
    if args.command == "maintenance":
        return maintenance(store, args.workspace, args.limit, args.cursor)
    if args.command == "setup":
        return setup_client(store, args.workspace, args.root, args.platform, args.client,
                            args.profile, args.approve, args.local_first_readonly)
    if args.command == "doctor":
        return doctor(store)
    if args.command == "context":
        return packet(store, args.cwd, args.query, args.session, args.level,
                      args.node, args.revision, args.cursor, args.budget)
    if args.command == "preference":
        return save_preference(store, args.id, args.text, args.kind,
                               args.expected_version, args.approve, args.workspace)
    if args.command == "context-refresh":
        return refresh_context(store, args.session)
    if args.command == "define-theme":
        return define_theme(store, args.workspace, read_json(args.spec), args.approve)
    if args.command == "solution-propose":
        return propose_solution(store, read_json(args.spec), args.expected_version, args.approve)
    if args.command == "route":
        return route(store, args.cwd, args.query, args.session,
                     read_json(args.parameters) if args.parameters else None,
                     read_json(args.conditions) if args.conditions else None)
    if args.command in ("solution-verify", "solution-execute"):
        return execute_solution(store, args.workspace, args.solution, args.session,
                                args.invocation, read_json(args.parameters), read_json(args.conditions),
                                args.command == "solution-verify", args.approve)
    if args.command == "solution-invalidate":
        return invalidate_solution(store, args.workspace, args.solution, args.version, args.state, args.approve)
    if args.command == "mission-create":
        return create_mission(store, read_json(args.spec))
    if args.command == "mission-control":
        return control(store, args.mission, args.action, args.approve)
    if args.command == "mission-status":
        return mission_status(store, args.mission)
    if args.command == "mission-tick":
        return tick(store, args.mission)
    if args.command == "retry-milestone":
        return retry_milestone(store, args.event_id)
    if args.command == "probe-android-node":
        return android_node(args.device, args.app, args.resource_id, args.adb, args.approve)
    if args.command == "register-alias":
        return register_alias(store, args.workspace, args.root, args.require_platform, args.approve)
    if args.command == "import-debugger":
        return import_debugger_log(store, args.workspace, args.source, args.session, args.task,
                                   args.title, args.approve)
    if args.command == "forget-workspace":
        return forget_workspace(store, args.workspace, args.approve)
    if args.command == "authorize-local":
        return authorize(store, args.workspace, args.approve)
    if args.command == "ingest":
        return ingest(store, read_json(args.event))
    if args.command == "task-status":
        return status(store, args.task_run)
    if args.command == "graph":
        return graph(store, args.workspace)
    if args.command == "search":
        return search(store, args.cwd, args.query, args.session,
                      read_json(args.conditions) if args.conditions else None)
    if args.command == "register-runner":
        return register_runner(store, args.workspace, args.name, read_json(args.spec), args.approve)
    if args.command == "run-validator":
        return run_registered(store, args.workspace, args.name, args.session, args.task, args.milestone)
    if args.command == "hook":
        raw = sys.stdin.buffer.read(256_001)
        require(len(raw) <= 256_000, "Hook input too large")
        return hook(store, args.client, args.event, json.loads(raw))
    if args.command == "install-client":
        installer = {"copilot": install_copilot, "codex": install_codex,
                     "codex-hooks": install_codex_hooks}[args.client]
        return installer(store, args.runtime_root, args.profile, args.approve)
    if args.command == "notify":
        return codex_notify(store, json.loads(args.payload))
    if args.command == "discover":
        return discover(store, args.cwd, args.session, args.platform)
    if args.command == "register":
        return store.register(args.workspace, args.root, args.platform)
    if args.command == "anchor":
        scope = store.workspace(args.workspace)
        path = workspace_file(Path(scope["root"]), args.path)
        return {"path": args.path, "sha256": file_digest(path)}
    if args.command == "observe":
        require(args.file.stat().st_size <= MAX_EVIDENCE_BYTES, "Evidence too large")
        return store.observe(args.event, args.workspace, read_json(args.conditions),
                             args.file.read_bytes(), args.type)
    if args.command == "propose":
        return store.propose(read_json(args.spec))
    if args.command == "verify":
        return store.verify(store.candidate(args.candidate))
    if args.command == "accept":
        revision = None if args.expected_revision == "none" else args.expected_revision
        return store.accept(args.candidate, args.expected_version, revision, args.approve)
    if args.command == "recall":
        return store.recall(read_json(args.task), args.session)
    if args.command == "refresh":
        return store.refresh(args.session)
    if args.command == "questions":
        return store.questions()
    if args.command == "import-graph":
        return import_graph(store, args.workspace, args.source, args.node, args.approve)
    if args.command == "catalog":
        return catalog(store, args.workspace)
    if args.command == "export-portal":
        return export_portal(store, args.workspace, args.output)
    if args.command == "acceptance-android":
        return android_acceptance(store, args.workspace, args.device, args.app, args.resource_id,
                                  read_json(args.context), args.output, args.approve)
    if args.command == "invalidate":
        return store.invalidate(args.claim, args.reason, args.lifecycle, args.expected_version,
                                args.expected_revision, args.approve)
    if args.command == "run-android":
        if args.session:
            os.environ["LLR_SESSION"] = args.session
        if args.task:
            os.environ["LLR_TASK"] = args.task
        return run_android_recipe(store, read_json(args.recipe), args.approve)
    if args.command == "learning-packet":
        return store.learning_packet(read_json(args.task), args.question)
    if args.command == "capture-android":
        return android_capture(store, args.workspace, args.device, args.app,
                               read_json(args.context), args.approve)
    if args.command == "capture-ios":
        return ios_capture(store, args.workspace, args.device, args.app,
                           read_json(args.context), args.approve)
    if args.command == "unlock-device":
        return store.unlock_device(args.device, args.approve)
    raise RuntimeErrorBase("Unknown command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = parser().parse_args(arguments)
    try:
        from .host import call, serve_host, ensure_host, stop_host
        if args.command == "host-serve":
            serve_host(args.home)
            return 0
        if args.command == "host-status":
            output = ensure_host(args.home)
        elif args.command == "host-stop":
            output = stop_host(args.home)
        elif args.command == "mcp":
            from .mcp import serve_bridge
            serve_bridge(args.home)
            return 0
        else:
            payload = {"argv": ["--home", str(args.home)] + arguments}
            if args.command == "hook":
                raw = sys.stdin.buffer.read(256_001)
                require(len(raw) <= 256_000, "Hook input too large")
                payload["hook_input"] = json.loads(raw)
            output = call(args.home, "command", payload)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        if isinstance(output, dict) and output.get("error"):
            return 1
        if isinstance(output, dict) and output.get("status") in (
                "BLOCKED", "NEEDS_INVESTIGATION", "NEEDS_INPUT", "NEEDS_EXPLORATION", "FAILED", "ITEM_TOO_LARGE"):
            return 2
        if isinstance(output, dict) and output.get("status") in ("AMBIGUOUS", "UNKNOWN_WORKSPACE"):
            return 2
        if isinstance(output, dict) and output.get("verdict") == "FAIL":
            return 2
        if isinstance(output, dict) and output.get("verdict") in ("INCONCLUSIVE", "PARTIAL"):
            return 2
        return 0
    except (RuntimeErrorBase, OSError, ValueError, sqlite3.Error, ET.ParseError,
            subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

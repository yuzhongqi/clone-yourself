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
    register = commands.add_parser("register")
    register.add_argument("workspace")
    register.add_argument("--root", type=Path, required=True)
    register.add_argument("--platform", choices=["android", "ios"], required=True)
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
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.home)
        output = dispatch(store, args)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        if isinstance(output, dict) and output.get("status") in ("BLOCKED", "NEEDS_INVESTIGATION"):
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
    finally:
        if store is not None:
            store.close()

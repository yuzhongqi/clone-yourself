"""Explicit bounded learning missions; no implicit resume or model loop."""

import json
import math
import os
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .core import (Store, RuntimeErrorBase, canonical, digest, file_digest, identifier,
                   now, object_fields, require, workspace_file)
from .learning import ingest, load_policy, privacy_key
from .privacy import sanitize
from .solutions import execute_solution
from .gateway_transport import send_gateway


def create_mission(store: Store, spec: Dict[str, Any]) -> Dict[str, Any]:
    object_fields(spec, {"schema", "id", "workspace", "goal", "deadline", "permissions",
                         "budgets", "questions"})
    require(spec["schema"] == "llr.mission.v1", "Unknown mission schema")
    identifier(spec["id"], "Mission")
    store.workspace(spec["workspace"])
    require(isinstance(spec["goal"], str) and 0 < len(spec["goal"]) <= 2000, "Invalid mission goal")
    deadline = datetime.fromisoformat(spec["deadline"].replace("Z", "+00:00"))
    require(deadline.tzinfo is not None and deadline.timestamp() > time.time(), "Deadline must be in future")
    object_fields(spec["permissions"], {"read_source", "run_validators", "allow_model"})
    require(all(type(value) is bool for value in spec["permissions"].values()), "Permissions must be bool")
    object_fields(spec["budgets"], {"max_items", "max_seconds", "max_files",
                                   "max_model_calls", "max_packet_bytes", "max_spend_micro"})
    limits = {"max_items": (1, 32), "max_seconds": (1, 21600), "max_files": (1, 1000),
              "max_model_calls": (0, 32), "max_packet_bytes": (1000, 12000),
              "max_spend_micro": (0, 100_000_000)}
    for name, (low, high) in limits.items():
        value = spec["budgets"][name]
        require(type(value) is int and low <= value <= high, "Invalid budget: " + name)
    require(isinstance(spec["questions"], list) and 0 < len(spec["questions"])
            <= spec["budgets"]["max_items"], "Question count exceeds budget")
    for index, question in enumerate(spec["questions"]):
        require(isinstance(question, dict) and question.get("kind") in
                ("inventory", "inspect_file", "verify_solution", "model_explain"), "Unknown learning task")
        require(isinstance(question.get("goal"), str) and bool(question["goal"]), "Question goal required")
        if question["kind"] == "inventory":
            object_fields(question, {"kind", "goal", "glob"})
            require(isinstance(question["glob"], str) and not Path(question["glob"]).is_absolute()
                    and ".." not in Path(question["glob"]).parts, "Inventory glob must stay in workspace")
        elif question["kind"] == "inspect_file":
            object_fields(question, {"kind", "goal", "path"})
        elif question["kind"] == "verify_solution":
            object_fields(question, {"kind", "goal", "solution", "parameters", "conditions"})
        else:
            object_fields(question, {"kind", "goal", "input_from"})
            require(isinstance(question["input_from"], list)
                    and all(type(item) is int and 0 <= item < index for item in question["input_from"]),
                    "Model questions can reference only prior checkpoints")
    safe, changed = sanitize(spec, privacy_key(store))
    require(not changed, "Mission contract contains sensitive literals; minimize before approval")
    body = dict(safe, created_at=now(), started_at=None, active_seconds=0.0)
    body["deadline"] = deadline.isoformat()
    with store.transaction():
        require(store.db.execute("SELECT 1 FROM missions WHERE id=?", (spec["id"],)).fetchone() is None,
                "Mission ID already exists")
        store.db.execute("INSERT INTO missions VALUES (?,?,?,?,?)",
                         (spec["id"], spec["workspace"], "DRAFT", canonical(body), now()))
        for index, question in enumerate(spec["questions"]):
            store.db.execute("INSERT INTO mission_items VALUES (?,?,?,?,?,?)",
                             (spec["id"], index, "PENDING", canonical(question), "{}", now()))
    return mission_status(store, spec["id"])


def mission_status(store: Store, mission: str) -> Dict[str, Any]:
    row = store.db.execute("SELECT * FROM missions WHERE id=?", (mission,)).fetchone()
    require(row is not None, "Mission not found")
    return {"id": mission, "workspace": row["workspace"], "state": row["state"],
            "completion_meaning": "Queue checkpoints completed; semantic claims retain their own verification level",
            "contract": json.loads(row["body"]), "updated_at": row["updated_at"],
            "items": [dict(item, body=json.loads(item["body"]), result=json.loads(item["result"]))
                      for item in store.db.execute("SELECT * FROM mission_items WHERE mission=? "
                                                   "ORDER BY ordinal", (mission,))]}


def control(store: Store, mission: str, action: str, approve: bool = False) -> Dict[str, Any]:
    require(action in ("approve", "start", "pause", "resume", "cancel"), "Unknown mission action")
    current = mission_status(store, mission)
    spec = current["contract"]
    if action in ("approve", "resume"):
        require(approve, "Explicit approval needed for learning or resume")
        load_policy(store, current["workspace"])
        require(datetime.fromisoformat(spec["deadline"]).timestamp() > time.time(), "Deadline expired")
    transitions = {
        "approve": ({"DRAFT"}, "APPROVED"), "start": ({"APPROVED"}, "RUNNING"),
        "pause": ({"RUNNING"}, "PAUSED"),
        "resume": ({"PAUSED", "WAITING_USER"}, "RUNNING"),
        "cancel": ({"DRAFT", "APPROVED", "RUNNING", "PAUSED", "WAITING_USER"}, "CANCELLED")}
    allowed, target = transitions[action]
    require(current["state"] in allowed, "Invalid mission transition")
    if action == "start":
        spec["started_at"] = now()
    with store.transaction():
        require(store.db.execute("SELECT state FROM missions WHERE id=?", (mission,)).fetchone()[0]
                == current["state"], "Mission state changed concurrently")
        store.db.execute("UPDATE missions SET state=?,body=?,updated_at=? WHERE id=?",
                         (target, canonical(spec), now(), mission))
        if action == "resume":
            for item in current["items"]:
                if item["state"] not in ("BLOCKED", "RUNNING"):
                    continue
                kind = item["body"]["kind"]
                require(kind != "verify_solution", "Review interrupted runtime outcome; do not rerun implicitly")
                if kind == "model_explain":
                    request_id = digest(canonical([mission, item["body"]]).encode())
                    require(store.db.execute("SELECT 1 FROM mission_model_calls WHERE id=?",
                                             (request_id,)).fetchone() is None,
                            "Model request was already attempted; audit billing before a new mission")
                store.db.execute("UPDATE mission_items SET state='PENDING',updated_at=? WHERE mission=? "
                                 "AND ordinal=?", (now(), mission, item["ordinal"]))
    return mission_status(store, mission)


def model_explain(store: Store, mission: str, spec: Dict[str, Any],
                  question: Dict[str, Any]) -> Dict[str, Any]:
    require(spec["permissions"]["allow_model"], "Mission does not authorize a model call")
    config_path = store.home / "model-gateway.json"
    require(config_path.is_file(), "No verified model cost gateway; unattended model work blocked")
    config = json.loads(config_path.read_text())
    object_fields(config, {"endpoint", "model", "token_env", "input_micro_per_million",
                          "output_micro_per_million", "max_output_tokens", "currency",
                          "external_hard_cap_verified", "allowed_workspaces"})
    require(config["external_hard_cap_verified"] is True
            and spec["workspace"] in config["allowed_workspaces"], "Gateway/workspace not authorized")
    endpoint = urllib.parse.urlparse(config["endpoint"])
    require(endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query,
            "Gateway endpoint cannot contain credentials or query parameters")
    require(endpoint.scheme == "https" or (endpoint.scheme == "http"
            and endpoint.hostname in ("127.0.0.1", "localhost")), "Gateway must use TLS or loopback")
    require(config["currency"] in ("USD", "CREDITS"), "Unknown spend unit")
    for name in ("input_micro_per_million", "output_micro_per_million", "max_output_tokens"):
        require(type(config[name]) is int and config[name] > 0, "Unknown gateway rates/limits")
    evidence = []
    for index in question["input_from"]:
        row = store.db.execute("SELECT state,result FROM mission_items WHERE mission=? AND ordinal=?",
                               (mission, index)).fetchone()
        require(row is not None and row["state"] == "DONE", "Required learning checkpoint unavailable")
        evidence.append({"checkpoint": index, "result": json.loads(row["result"])})
    packet = {"question": question["goal"], "evidence": evidence,
              "instruction": "Return scoped code inferences and unknowns. No tools, no runtime success claim."}
    require(len(canonical(packet).encode()) <= spec["budgets"]["max_packet_bytes"],
            "Learning packet exceeds bound; reduce scope, never inherit conversation history")
    body = {"model": config["model"], "messages": [{"role": "user", "content": canonical(packet)}],
            "max_tokens": config["max_output_tokens"]}
    raw = canonical(body).encode()
    worst = math.ceil(((len(raw) + 256) * config["input_micro_per_million"]
                       + config["max_output_tokens"] * config["output_micro_per_million"]) / 1_000_000)
    request_id = digest(canonical([mission, question]).encode())
    token = os.environ.get(config["token_env"])
    require(token, "Gateway credential unavailable; request not sent")
    with store.transaction():
        row = store.db.execute("SELECT count(*),COALESCE(sum(reserved_micro),0) FROM mission_model_calls "
                               "WHERE mission=?", (mission,)).fetchone()
        require(row[0] < spec["budgets"]["max_model_calls"], "Model call budget exhausted")
        require(row[1] + worst <= spec["budgets"]["max_spend_micro"], "Spend budget exhausted")
        require(store.db.execute("SELECT 1 FROM mission_model_calls WHERE id=?", (request_id,)).fetchone()
                is None, "Model request already attempted; do not repeat an unknown billed request")
        store.db.execute("INSERT INTO mission_model_calls VALUES (?,?,?,?,?,?)",
                         (request_id, mission, worst, None, "RESERVED",
                          canonical({"currency": config["currency"], "bytes": len(raw)})))
    headers = {
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "X-LLR-Request-ID": request_id, "X-LLR-Max-Cost-Micro": str(worst)}
    remaining = min(30, spec["budgets"]["max_seconds"] - spec["active_seconds"],
                    datetime.fromisoformat(spec["deadline"]).timestamp() - time.time())
    answer = send_gateway(config["endpoint"], headers, raw, remaining)
    require(answer.get("cost_enforced") is True, "Gateway did not attest the required hard cap")
    usage = answer.get("usage", {})
    require(type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] >= 0
            and type(usage.get("completion_tokens")) is int and usage["completion_tokens"] >= 0,
            "Gateway usage unknown; block unattended continuation")
    actual = math.ceil((usage["prompt_tokens"] * config["input_micro_per_million"]
                        + usage["completion_tokens"] * config["output_micro_per_million"]) / 1_000_000)
    require(0 <= actual <= worst, "Gateway exceeded reserved cap; stop and audit")
    content = answer["choices"][0]["message"]["content"]
    require(isinstance(content, str) and len(content.encode()) <= 12_000, "Invalid model answer")
    safe, _ = sanitize(content, privacy_key(store))
    with store.transaction():
        store.db.execute("UPDATE mission_model_calls SET state='ACCOUNTED',actual_micro=? WHERE id=?",
                         (actual, request_id))
    return {"kind": "code_inference", "answer": safe, "usage": usage, "cost_micro": actual,
            "currency": config["currency"], "not_runtime_verified": True}


def tick(store: Store, mission: str) -> Dict[str, Any]:
    current = mission_status(store, mission)
    require(current["state"] == "RUNNING", "Mission is not running")
    spec = current["contract"]
    expired = datetime.fromisoformat(spec["deadline"]).timestamp() <= time.time()
    if expired or spec["active_seconds"] >= spec["budgets"]["max_seconds"]:
        with store.transaction():
            store.db.execute("UPDATE missions SET state='BUDGET_EXHAUSTED',updated_at=? WHERE id=?",
                             (now(), mission))
        return mission_status(store, mission)
    running = [item for item in current["items"] if item["state"] == "RUNNING"]
    if running:
        with store.transaction():
            store.db.execute("UPDATE missions SET state='WAITING_USER',updated_at=? WHERE id=?",
                             (now(), mission))
        return mission_status(store, mission)
    pending = [item for item in current["items"] if item["state"] == "PENDING"]
    if not pending:
        with store.transaction():
            store.db.execute("UPDATE missions SET state=?,updated_at=? WHERE id=?",
                             ("PARTIAL" if any(item["state"] != "DONE" for item in current["items"])
                              else "COMPLETED", now(), mission))
        return mission_status(store, mission)
    item = pending[0]
    question = item["body"]
    with store.transaction():
        require(store.db.execute("SELECT state FROM mission_items WHERE mission=? AND ordinal=?",
                                 (mission, item["ordinal"])).fetchone()[0] == "PENDING",
                "Learning item claimed concurrently")
        store.db.execute("UPDATE mission_items SET state='RUNNING',updated_at=? WHERE mission=? AND ordinal=?",
                         (now(), mission, item["ordinal"]))
    started = time.monotonic()
    result, item_state, state = {}, "DONE", "RUNNING"
    try:
        root = Path(store.workspace(spec["workspace"])["root"])
        if question["kind"] in ("inventory", "inspect_file"):
            require(spec["permissions"]["read_source"], "Source inspection not permitted")
            used_files = sum(len(row["result"].get("files", []))
                             + int(row["result"].get("kind") == "source_metadata")
                             for row in current["items"])
            remaining_files = spec["budgets"]["max_files"] - used_files
            require(remaining_files > 0, "File budget exhausted")
            if question["kind"] == "inspect_file":
                path = workspace_file(root, question["path"])
                require(path.stat().st_size <= 1024 * 1024, "Source file exceeds inspection limit")
                result = {"path": question["path"], "sha256": file_digest(path),
                          "bytes": path.stat().st_size, "kind": "source_metadata",
                          "meaning": "File identity, not architecture understanding or runtime proof"}
            else:
                files, omitted = [], False
                for path in root.glob(question["glob"]):
                    if (len(files) >= min(64, remaining_files) or
                            time.monotonic() - started > min(5, spec["budgets"]["max_seconds"]
                                                            - spec["active_seconds"])):
                        omitted = True
                        break
                    if not path.is_file():
                        continue
                    relative = path.relative_to(root).as_posix()
                    path = workspace_file(root, relative)
                    require(path.stat().st_size <= 1024 * 1024, "Inventory includes oversized file")
                    files.append({"path": relative, "sha256": file_digest(path),
                                  "bytes": path.stat().st_size})
                result = {"kind": "source_inventory", "files": files,
                          "partial": omitted, "meaning": "Structural inventory, not runtime verification"}
                if omitted:
                    item_state = "PARTIAL"
        elif question["kind"] == "verify_solution":
            require(spec["permissions"]["run_validators"], "Mission does not authorize runtime operations")
            result = execute_solution(store, spec["workspace"], question["solution"],
                                      "mission-" + mission, "item-" + str(item["ordinal"]),
                                      question["parameters"], question["conditions"])
            require(result["status"] == "LOCAL_SOLVED", "Solution revalidation failed")
        else:
            result = model_explain(store, mission, spec, question)
        if question["kind"] != "verify_solution":
            event = {"schema": "llr.milestone.v1", "workspace": spec["workspace"],
                     "session": "mission-" + mission, "task": "learning",
                     "milestone": "item-" + str(item["ordinal"]), "title": question["goal"],
                     "kind": "code_inference", "outcome": "PARTIAL" if item_state == "PARTIAL" else "INCONCLUSIVE",
                     "conditions": {"source_scope": "recorded file identities; no runtime attestation"},
                     "operations": [{"kind": question["kind"]}], "observation": result,
                     "checks": [], "anchors": [], "uncertainties": [
                         "Static inventory/model interpretation must not be promoted to runtime proof."]}
            ingest(store, event)
    except (RuntimeErrorBase, OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        result = {"reason": sanitize(str(exc), privacy_key(store))[0]}
        item_state, state = "BLOCKED", "WAITING_USER"
    spec["active_seconds"] += time.monotonic() - started
    with store.transaction():
        store.db.execute("UPDATE mission_items SET state=?,result=?,updated_at=? WHERE mission=? AND ordinal=?",
                         (item_state, canonical(result), now(), mission, item["ordinal"]))
        existing_state = store.db.execute("SELECT state FROM missions WHERE id=?", (mission,)).fetchone()[0]
        if existing_state != "RUNNING":
            state = existing_state
        store.db.execute("UPDATE missions SET state=?,body=?,updated_at=? WHERE id=?",
                         (state, canonical(spec), now(), mission))
    return mission_status(store, mission)

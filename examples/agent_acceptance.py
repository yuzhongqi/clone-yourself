"""Reproducible v0.4 packaged-host acceptance; all data is synthetic and private."""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_learning_runtime.core import Store, canonical, digest, file_digest, now, require_private_location


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    require_private_location(out)
    if out.exists():
        raise RuntimeError("Choose a new acceptance output directory")
    out.mkdir(parents=True, mode=0o700)
    workspace = out / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("synthetic baseline\n")
    (workspace / "b.txt").write_text("synthetic baseline\n")
    home = out / "state"
    command = [sys.executable, str(args.entry), "--home", str(home)]
    events = []

    def run(arguments, expected=0):
        result = subprocess.run(command + arguments, cwd=workspace, capture_output=True,
                                text=True, timeout=120)
        if result.returncode != expected:
            raise RuntimeError(result.stderr + result.stdout)
        value = json.loads(result.stdout)
        events.append({"command": arguments, "returncode": result.returncode, "result": value})
        return value

    def write(name, body):
        path = out / name
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2))
        return str(path)

    started = time.monotonic()
    try:
        run(["register", "acceptance/generic", "--root", str(workspace), "--platform", "generic"])
        run(["authorize-local", "--workspace", "acceptance/generic", "--approve"])
        health = run(["host-status"])
        fingerprint = file_digest(workspace / "a.txt")
        spec = {"schema": "llr.solution.v1", "id": "baseline", "workspace": "acceptance/generic",
                "problem": {"intent": "baseline", "goal": "Verify the synthetic baseline file",
                            "patterns": ["confirm baseline {path}"]},
                "parameters": {"path": {"type": "string", "enum": ["a.txt", "b.txt"]}},
                "preconditions": [{"kind": "file_sha256", "path": {"input": "path"}, "expected": fingerprint}],
                "steps": [{"id": "read", "op": "inspect_file", "path": {"input": "path"}}],
                "postconditions": [{"step": "read", "pointer": "/sha256", "expected": fingerprint}],
                "result": {"file": {"input": "path"}, "sha256": {"step": "read", "pointer": "/sha256"}},
                "boundaries": ["Only synthetic file identity"]}
        run(["solution-propose", write("solution.json", spec), "--expected-version", "0", "--approve"])
        inputs = write("a-input.json", {"path": "a.txt"})
        conditions = write("conditions.json", {})
        run(["solution-verify", "baseline", "--workspace", "acceptance/generic", "--session", "training",
             "--invocation", "first", "--parameters", inputs, "--conditions", conditions, "--approve"])
        route = run(["route", "--query", "confirm baseline a.txt", "--session", "fresh-session"])
        assert route["status"] == "LOCAL_READY"
        solved = run(["solution-execute", route["solution"], "--workspace", route["workspace"],
                      "--session", "fresh-session", "--invocation", "request-one",
                      "--parameters", inputs, "--conditions", conditions])
        assert solved["status"] == "LOCAL_SOLVED" and solved["model_calls"] == 0
        unverified = run(["route", "--query", "confirm baseline b.txt", "--session", "other"], expected=2)
        assert unverified["status"] == "NEEDS_EXPLORATION"
        (workspace / "a.txt").write_text("changed")
        blocked = run(["route", "--query", "confirm baseline a.txt", "--session", "changed"], expected=2)
        assert blocked["status"] == "BLOCKED"
        second_health = run(["host-status"])
        assert health["pid"] == second_health["pid"]
        # Large memory in this isolated acceptance store, never in a real project store.
        store = Store(home)
        try:
            with store.transaction():
                for index in range(10_000):
                    item = dict(spec, id="scale-" + str(index))
                    item["problem"] = {"intent": "scale", "goal": "Synthetic fingerprint baseline " + str(index),
                                       "patterns": ["scale " + str(index)]}
                    body = canonical(item)
                    store.db.execute("INSERT INTO solutions VALUES (?,?,?,?,?,?,?)",
                                     (item["id"], "acceptance/generic", 1, "candidate",
                                      body, digest(body.encode()), now()))
        finally:
            store.close()
        samples = []
        for index in range(3):
            before = time.monotonic()
            packet = run(["context", "--query", "fingerprint baseline", "--session", "scale-reader",
                          "--level", "2", "--budget", "6000"])
            samples.append(time.monotonic() - before)
            assert len(canonical(packet).encode()) <= 6000 and packet["omitted_count"] > 0
        job = {"schema": "llr.mission.v1", "id": "local-inspection", "workspace": "acceptance/generic",
               "goal": "Bounded local inventory, not semantic architecture understanding",
               "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
               "permissions": {"read_source": True, "run_validators": False, "allow_model": False},
               "budgets": {"max_items": 2, "max_seconds": 30, "max_files": 5,
                           "max_model_calls": 0, "max_packet_bytes": 4000, "max_spend_micro": 0},
               "questions": [{"kind": "inventory", "goal": "Inventory approved files", "glob": "*.txt"}]}
        run(["mission-create", write("mission.json", job)])
        run(["mission-control", job["id"], "approve", "--approve"])
        run(["mission-control", job["id"], "start"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = run(["mission-status", job["id"]])
            if state["state"] != "RUNNING":
                break
            time.sleep(0.5)
        assert state["state"] == "COMPLETED"
        report = {"status": "PASS", "packaged_entry": str(args.entry),
                  "shared_host_pid": health["pid"], "independent_client_reuse": "LOCAL_SOLVED",
                  "unverified_parameter": unverified["status"], "changed_condition": blocked["status"],
                  "memory_nodes": 10_001, "packet_bytes": len(canonical(packet).encode()),
                  "omitted_nodes": packet["omitted_count"], "context_seconds": samples,
                  "mission": state["state"], "model_calls": 0,
                  "elapsed_seconds": time.monotonic() - started,
                  "limits": ["Synthetic packaged acceptance, not a desktop UI session.",
                             "Local inventory is not full architecture understanding."]}
        write("report.json", report)
        write("events.json", events)
        print(json.dumps(report, indent=2))
    finally:
        subprocess.run(command + ["host-stop"], capture_output=True, timeout=15)


if __name__ == "__main__":
    main()

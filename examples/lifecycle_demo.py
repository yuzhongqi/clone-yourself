"""Synthetic lifecycle acceptance across real independent CLI processes; no App/model."""

import argparse
import json
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from local_learning_runtime.core import Store, canonical, file_digest, require_private_location
from local_learning_runtime.learning import authorize
from local_learning_runtime.lifecycle import configure
from local_learning_runtime.solutions import propose_solution, execute_solution, route


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.expanduser().resolve()
    require_private_location(out)
    if out.exists():
        raise RuntimeError("Choose a new acceptance directory")
    out.mkdir(parents=True, mode=0o700)
    workspace = out / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("synthetic identity fixture\n")
    home = out / "state"
    store = Store(home)
    try:
        store.register("demo/generic", workspace, "generic")
        authorize(store, ["demo/generic"], True)
        fingerprint = file_digest(workspace / "fixture.txt")
        spec = {"schema": "llr.solution.v1", "id": "fixture", "workspace": "demo/generic",
                "problem": {"intent": "identity", "goal": "Verify fixture file identity",
                            "patterns": ["check fixture"]},
                "parameters": {},
                "preconditions": [{"kind": "file_sha256", "path": "fixture.txt", "expected": fingerprint}],
                "steps": [{"id": "inspect", "op": "inspect_file", "path": "fixture.txt"}],
                "postconditions": [{"step": "inspect", "pointer": "/sha256", "expected": fingerprint}],
                "result": {"sha256": {"step": "inspect", "pointer": "/sha256"}},
                "boundaries": ["Synthetic file identity, no real App verification"]}
        old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        with ExitStack() as stack:
            for module in ("core", "solutions", "lifecycle"):
                stack.enter_context(patch("local_learning_runtime." + module + ".now", return_value=old))
            propose_solution(store, spec, 0, True)
            trained = execute_solution(store, "demo/generic", "fixture", "training", "first", {}, {}, True, True)
            assert trained["status"] == "LOCAL_SOLVED"
            for index in range(3):
                route(store, workspace, "check fixture", "prior-use-" + str(index))
        configure(store, "demo/generic", "solution", "fixture", {
            "knowledge_type": "source", "max_age_seconds": 100, "review_after_seconds": 50,
            "archive_after_seconds": 1000, "required_dynamic": []}, True)
    finally:
        store.close()
    command = [sys.executable, "-m", "local_learning_runtime", "--home", str(home)]
    steps = []

    def run(arguments, expected=0):
        result = subprocess.run(command + arguments, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != expected:
            raise RuntimeError(result.stderr + result.stdout)
        body = json.loads(result.stdout)
        steps.append({"arguments": arguments, "result": body, "exit_code": result.returncode})
        return body

    try:
        blocked = run(["route", "--cwd", str(workspace), "--query", "check fixture",
                       "--session", "fresh-client"], 2)
        assert blocked["status"] == "BLOCKED"
        first_queue = run(["maintenance", "--workspace", "demo/generic"])
        assert first_queue["items"] and first_queue["items"][0]["health"]["status"] == "EXPIRED"
        before = run(["memory-health", "--workspace", "demo/generic", "--kind", "solution", "--id", "fixture"])
        run(["context", "--cwd", str(workspace), "--query", "fixture",
             "--session", "reader-a", "--level", "2"])
        run(["context", "--cwd", str(workspace), "--query", "fixture",
             "--session", "reader-b", "--level", "2"])
        after_reads = run(["memory-health", "--workspace", "demo/generic", "--kind", "solution", "--id", "fixture"])
        assert before["last_verified_at"] == after_reads["last_verified_at"]
        assert after_reads["usage"]["counts"]["retrieved"] == before["usage"]["counts"]["retrieved"] + 2
        run(["export-portal", "--workspace", "demo/generic", "--output", str(out / "expired-portal")])
        inputs = out / "inputs.json"
        inputs.write_text("{}")
        # Explicitly approve revalidation: not an experiment silently launched by maintenance.
        revalidated = run(["solution-verify", "fixture", "--workspace", "demo/generic",
                           "--session", "approved-revalidation", "--invocation", "one",
                           "--parameters", str(inputs), "--conditions", str(inputs), "--approve"])
        assert revalidated["status"] == "LOCAL_SOLVED"
        ready = run(["route", "--cwd", str(workspace), "--query", "check fixture",
                     "--session", "new-after-validation"])
        assert ready["status"] == "LOCAL_READY"
        health = run(["memory-health", "--workspace", "demo/generic", "--kind", "solution", "--id", "fixture"])
        assert health["status"] == "FRESH" and health["usage"]["counts"]["verified_success"] == 2
        portal = run(["export-portal", "--workspace", "demo/generic", "--output", str(out / "fresh-portal")])
        report = {"status": "PASS", "fixture_clock": "Synthetic historical verification, not a real App run",
                  "fresh_process_expired_route": blocked["status"],
                  "retrieval_did_not_renew_verification": True,
                  "maintenance_reported_expiry_without_experiment": True,
                  "explicit_revalidation_result": revalidated["status"],
                  "new_process_after_revalidation": ready["status"],
                  "usage": health["usage"], "portal": portal, "model_calls": 0, "app_actions": 0}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (out / "steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        subprocess.run(command + ["host-stop"], capture_output=True, timeout=10)


if __name__ == "__main__":
    main()

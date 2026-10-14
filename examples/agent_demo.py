"""Synthetic v0.4 complete memo -> shared Host -> independent-session reuse."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from local_learning_runtime.core import Store, file_digest
from local_learning_runtime.learning import authorize
from local_learning_runtime.solutions import propose_solution, execute_solution


def main():
    with tempfile.TemporaryDirectory(prefix="llr-agent-demo-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "baseline.txt").write_text("synthetic fixture\n")
        home = root / "private-state"
        store = Store(home)
        try:
            store.register("demo/generic", workspace, "generic")
            authorize(store, ["demo/generic"], True)
            fingerprint = file_digest(workspace / "baseline.txt")
            spec = {
                "schema": "llr.solution.v1", "id": "baseline", "workspace": "demo/generic",
                "problem": {"intent": "baseline", "goal": "Verify synthetic baseline identity",
                            "patterns": ["check baseline"]},
                "parameters": {},
                "preconditions": [{"kind": "file_sha256", "path": "baseline.txt", "expected": fingerprint}],
                "steps": [{"id": "read", "op": "inspect_file", "path": "baseline.txt"}],
                "postconditions": [{"step": "read", "pointer": "/sha256", "expected": fingerprint}],
                "result": {"sha256": {"step": "read", "pointer": "/sha256"}},
                "boundaries": ["Synthetic file identity only"]}
            propose_solution(store, spec, 0, True)
            trial = execute_solution(store, "demo/generic", "baseline", "training", "first",
                                     {}, {}, True, True)
            assert trial["status"] == "LOCAL_SOLVED"
        finally:
            store.close()
        command = [sys.executable, "-m", "local_learning_runtime", "--home", str(home)]
        try:
            health_a = json.loads(subprocess.check_output(command + ["host-status"], text=True))
            decision = json.loads(subprocess.check_output(command + [
                "route", "--cwd", str(workspace), "--query", "check baseline",
                "--session", "fresh-consumer"], text=True))
            assert decision["status"] == "LOCAL_READY"
            inputs = root / "inputs.json"
            inputs.write_text("{}")
            result = json.loads(subprocess.check_output(command + [
                "solution-execute", decision["solution"], "--workspace", decision["workspace"],
                "--session", "fresh-consumer", "--invocation", "one", "--parameters", str(inputs),
                "--conditions", str(inputs)], text=True))
            health_b = json.loads(subprocess.check_output(command + ["host-status"], text=True))
            assert health_a["pid"] == health_b["pid"]
            assert result["status"] == "LOCAL_SOLVED" and result["model_calls"] == 0
            print(json.dumps({"shared_host_pid": health_b["pid"], "new_session": result["status"],
                              "model_calls": result["model_calls"]}, indent=2))
        finally:
            subprocess.run(command + ["host-stop"], check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    main()

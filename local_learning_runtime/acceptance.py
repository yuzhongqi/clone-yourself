"""Explicit real-device acceptance with fresh-process recall and negative checks."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .core import RuntimeErrorBase, Store, canonical, digest, require
from .devices import run_android_recipe
from .knowledge import export_portal


def android_acceptance(store: Store, workspace: str, device: str, app: str,
                       resource_id: str, context: Dict[str, Any], output: Path,
                       approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required for real app restart and acceptance")
    require(not output.exists(), "Acceptance output exists; choose a new run directory")
    require(resource_id.startswith(app + ":id/"), "Select a resource ID owned by the test app")
    assertion = {"action": "assert_node", "resource_id": resource_id,
                 "attributes": {"enabled": "true"}}
    recipe = {"workspace": workspace, "device": device, "app": app, "context": context,
              "steps": [{"action": "launch"}, assertion, {"action": "restart"}, assertion],
              "restore": [assertion]}
    output.mkdir(parents=True, mode=0o700)
    (output / "recipe.json").write_text(json.dumps(recipe, indent=2), encoding="utf-8")
    journey = run_android_recipe(store, recipe, approve)
    (output / "journey.json").write_text(json.dumps(journey, indent=2), encoding="utf-8")
    require(journey["verdict"] == "PASS", "Real journey failed or is partial; inspect journey.json")
    observed = journey["conditions"]
    claim_id = "acceptance-" + digest(resource_id.encode())[:16]
    spec = {"id": claim_id, "workspace": workspace, "task_type": "app-restart-observation",
            "statement": "In this recorded run, the unique enabled UI node " + resource_id
                         + " was observed before and after app restart. This does not prove "
                         "account identity, full feature operation, or source execution.",
            "conditions": observed, "anchors": [], "dependencies": [],
            "checks": [{"kind": "json_equals", "evidence_id": journey["report_evidence_id"],
                        "pointer": "/verdict", "expected": "PASS"}]}
    for step in journey["steps"]:
        if step["action"] == "assert_node":
            spec["checks"].append({"kind": "ui_node", "evidence_id": step["evidence_id"],
                                   "selector": {"resource-id": resource_id, "package": app},
                                   "attributes": {"enabled": "true"}})
    candidate = store.propose(spec)
    current = store.snapshot().get(claim_id)
    published = store.accept(candidate["candidate_id"], current["version"] if current else 0,
                             store.revision(), approve)
    task = {"workspace": workspace, "task_type": spec["task_type"], "conditions": observed}
    (output / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")

    def fresh_recall(task_path: Path, session: str) -> Dict[str, Any]:
        result = subprocess.run([sys.executable, "-m", "local_learning_runtime", "--home",
                                 str(store.home), "recall", str(task_path), "--session", session],
                                capture_output=True, text=True, timeout=30)
        require(result.returncode in (0, 2), "Fresh CLI failed: " + result.stderr)
        packet = json.loads(result.stdout)
        require((result.returncode == 0) == (packet["status"] == "LOCAL_READY"),
                "Fresh CLI exit status disagrees with recall")
        return packet

    session = "acceptance-" + journey["run_id"]
    positive = fresh_recall(output / "task.json", session)
    require(positive["status"] == "LOCAL_READY", "Fresh process could not recall real observation")
    altered = json.loads(canonical(task))
    altered["conditions"]["artifact_sha256"] = "0" * 64
    require(altered["conditions"]["artifact_sha256"] != observed["artifact_sha256"],
            "Negative fixture must differ from actual artifact")
    (output / "changed-task.json").write_text(json.dumps(altered, indent=2), encoding="utf-8")
    negative = fresh_recall(output / "changed-task.json", session + "-negative")
    require(negative["status"] == "BLOCKED", "Changed artifact was not blocked")
    wrong = json.loads(canonical(spec))
    wrong["id"] = claim_id + "-negative"
    wrong["checks"][0]["expected"] = "FAIL"
    bad_candidate = store.propose(wrong)
    before = store.revision()
    try:
        store.accept(bad_candidate["candidate_id"], 0, before, approve)
    except RuntimeErrorBase as exc:
        require("Local checks failed" in str(exc), "Negative candidate failed for unrelated reason")
    else:
        raise RuntimeErrorBase("Failed predicate was accepted")
    require(store.revision() == before, "Rejected candidate changed published revision")
    portal = export_portal(store, workspace, output / "portal")
    report = {"status": "PASS", "real_device_run": True, "mocked_device_calls": False,
              "journey": journey, "publication": published, "fresh_process_recall": positive["status"],
              "changed_artifact_recall": negative["status"], "failed_candidate_rejected": True,
              "model_requests_by_runtime": 0, "portal": portal}
    (output / "acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report

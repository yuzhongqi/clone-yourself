import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_learning_runtime.core import Store, RuntimeErrorBase, canonical, file_digest
from local_learning_runtime.context import packet, define_theme, refresh_context
from local_learning_runtime.host import AgentServer, raw_call
from local_learning_runtime.learning import authorize
from local_learning_runtime.missions import create_mission, control, tick, mission_status
from local_learning_runtime.solutions import (propose_solution, execute_solution, route,
                                            invalidate_solution, compile_pattern)
from local_learning_runtime.learning import register_runner, run_registered
from local_learning_runtime.clients import hook, setup_client
from local_learning_runtime.preferences import save_preference


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "data.json").write_text('{"ready":true}\n')
        (self.repo / "same.json").write_text('{"ready":true}\n')
        self.store = Store(self.root / "state")
        self.store.register("sample/android", self.repo, "android")
        authorize(self.store, ["sample/android"], True)
        self.spec = {
            "schema": "llr.solution.v1", "id": "file-integrity", "workspace": "sample/android",
            "problem": {"intent": "file-integrity", "goal": "Verify the approved file identity",
                        "patterns": ["check file {path}", "检查文件 {path}"]},
            "parameters": {"path": {"type": "string", "enum": ["data.json", "same.json"]}},
            "preconditions": [{"kind": "file_sha256", "path": {"input": "path"},
                               "expected": file_digest(self.repo / "data.json")}],
            "steps": [{"id": "inspect", "op": "inspect_file", "path": {"input": "path"}}],
            "postconditions": [{"step": "inspect", "pointer": "/sha256",
                                "expected": file_digest(self.repo / "data.json")}],
            "result": {"path": {"input": "path"}, "sha256": {"step": "inspect", "pointer": "/sha256"}},
            "boundaries": ["Only the approved file identity, not business functionality"]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def publish(self):
        propose_solution(self.store, self.spec, 0, True)
        result = execute_solution(self.store, "sample/android", "file-integrity", "training",
                                  "first", {"path": "data.json"}, {}, True, True)
        self.assertEqual(result["status"], "LOCAL_SOLVED")
        return result

    def mission(self, questions=None, permissions=None):
        return {
            "schema": "llr.mission.v1", "id": "learn-example", "workspace": "sample/android",
            "goal": "Inspect a small synthetic source tree",
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "permissions": permissions or {"read_source": True, "run_validators": False, "allow_model": False},
            "budgets": {"max_items": 4, "max_seconds": 30, "max_files": 10,
                        "max_model_calls": 0, "max_packet_bytes": 4000, "max_spend_micro": 0},
            "questions": questions or [{"kind": "inventory", "goal": "Inventory files", "glob": "*.json"}]}

    def test_known_problem_reuses_complete_contract_without_model(self):
        self.publish()
        decision = route(self.store, self.repo, "check file data.json", "new-session")
        self.assertEqual(decision["status"], "LOCAL_READY")
        result = execute_solution(self.store, "sample/android", decision["solution"], "new-session",
                                  "one", decision["parameters"], {})
        self.assertEqual(result["status"], "LOCAL_SOLVED")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["result"]["path"], "data.json")
        self.assertTrue(result["assertions"][0]["passed"])

    def test_declared_parameter_not_automatically_verified(self):
        self.publish()
        self.assertEqual(route(self.store, self.repo, "check file same.json", "new")["status"],
                         "NEEDS_EXPLORATION")
        second = execute_solution(self.store, "sample/android", "file-integrity", "training",
                                  "second", {"path": "same.json"}, {}, True, True)
        self.assertEqual(second["status"], "LOCAL_SOLVED")
        self.assertEqual(route(self.store, self.repo, "check file same.json", "new")["status"], "LOCAL_READY")

    def test_parameter_outside_scope_is_blocked(self):
        self.publish()
        self.assertEqual(route(self.store, self.repo, "check file ../private", "new")["status"], "BLOCKED")

    def test_changed_source_blocks_route_and_execution(self):
        self.publish()
        (self.repo / "data.json").write_text("changed")
        self.assertEqual(route(self.store, self.repo, "check file data.json", "new")["status"], "BLOCKED")
        result = execute_solution(self.store, "sample/android", "file-integrity", "new",
                                  "one", {"path": "data.json"}, {})
        self.assertEqual(result["status"], "BLOCKED")

    def test_unknown_condition_does_not_pass(self):
        self.spec["preconditions"].append({"kind": "condition_equals", "name": "sdk", "expected": 35})
        propose_solution(self.store, self.spec, 0, True)
        result = execute_solution(self.store, "sample/android", "file-integrity", "test", "one",
                                  {"path": "data.json"}, {}, True, True)
        self.assertEqual(result["status"], "NEEDS_INPUT")

    def test_postcondition_failure_is_not_published(self):
        self.spec["postconditions"][0]["expected"] = "b" * 64
        propose_solution(self.store, self.spec, 0, True)
        result = execute_solution(self.store, "sample/android", "file-integrity", "test", "one",
                                  {"path": "data.json"}, {}, True, True)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.store.db.execute("SELECT lifecycle FROM solutions").fetchone()[0], "candidate")

    def test_no_arbitrary_step_or_unapproved_trial(self):
        self.spec["steps"][0]["op"] = "shell"
        with self.assertRaisesRegex(RuntimeErrorBase, "operation"):
            propose_solution(self.store, self.spec, 0, True)
        self.spec["steps"][0]["op"] = "inspect_file"
        propose_solution(self.store, self.spec, 0, True)
        with self.assertRaises(RuntimeErrorBase):
            execute_solution(self.store, "sample/android", "file-integrity", "test", "one",
                             {"path": "data.json"}, {}, True, False)

    def test_invocation_is_idempotent_and_interrupted_not_repeated(self):
        self.publish()
        first = execute_solution(self.store, "sample/android", "file-integrity", "new", "one",
                                 {"path": "data.json"}, {})
        second = execute_solution(self.store, "sample/android", "file-integrity", "new", "one",
                                  {"path": "data.json"}, {})
        self.assertEqual(first, second)
        self.store.db.execute("UPDATE solution_runs SET state='RUNNING' WHERE id=?", (first["run_id"],))
        self.store.db.commit()
        with self.assertRaisesRegex(RuntimeErrorBase, "Interrupted"):
            execute_solution(self.store, "sample/android", "file-integrity", "new", "one",
                             {"path": "data.json"}, {})

    def test_invalidation_prevents_reuse(self):
        self.publish()
        invalidate_solution(self.store, "sample/android", "file-integrity", 1, "conflicted", True)
        self.assertEqual(route(self.store, self.repo, "check file data.json", "new")["status"], "BLOCKED")

    def test_context_layers_pin_revision_and_expand_on_demand(self):
        self.publish()
        node = "solution:sample/android:file-integrity"
        define_theme(self.store, "sample/android", {
            "id": "integrity", "title": "File checks", "summary": "Approved source identity checks",
            "aliases": ["fingerprint", "文件校验"], "members": [node], "related": [],
            "gaps": ["No business operation claim"]}, True)
        l0 = packet(self.store, self.repo, "android", "new", 0)
        self.assertNotIn("steps", canonical(l0))
        l1 = packet(self.store, self.repo, "fingerprint", "new", 1)
        self.assertIn("integrity", canonical(l1))
        l2 = packet(self.store, self.repo, "fingerprint", "new", 2)
        self.assertEqual(l2["items"][0]["id"], node)
        detail = packet(self.store, self.repo, "android", "new", 3, node, l2["revision"])
        self.assertTrue(detail["items"])
        invalidate_solution(self.store, "sample/android", "file-integrity", 1, "stale", True)
        pinned = packet(self.store, self.repo, "fingerprint", "new", 2, revision=l2["revision"])
        self.assertEqual(pinned["revision"], l2["revision"])
        self.assertNotEqual(pinned["revision"], pinned["current_revision"])
        self.assertTrue(refresh_context(self.store, "new")["requires_reload"])

    def test_large_memory_is_bounded_and_reports_omissions(self):
        with self.store.transaction():
            for index in range(1000):
                spec = copy.deepcopy(self.spec)
                spec["id"] = "solution-" + str(index)
                spec["problem"]["goal"] = "Fingerprint check number " + str(index)
                body = canonical(spec)
                from local_learning_runtime.core import digest, now
                self.store.db.execute("INSERT INTO solutions VALUES (?,?,?,?,?,?,?)",
                                      (spec["id"], "sample/android", 1, "candidate",
                                       body, digest(body.encode()), now()))
        result = packet(self.store, self.repo, "Fingerprint check", "new", 2, budget=3000)
        self.assertLessEqual(len(canonical(result).encode()), 3000)
        self.assertGreater(result["omitted_count"], 0)
        self.assertIsNotNone(result["next_cursor"])

    def test_learning_requires_approval_and_can_pause_resume(self):
        created = create_mission(self.store, self.mission())
        self.assertEqual(created["state"], "DRAFT")
        with self.assertRaises(RuntimeErrorBase):
            control(self.store, "learn-example", "approve", False)
        control(self.store, "learn-example", "approve", True)
        control(self.store, "learn-example", "start")
        control(self.store, "learn-example", "pause")
        self.assertEqual(mission_status(self.store, "learn-example")["state"], "PAUSED")
        control(self.store, "learn-example", "resume", True)
        tick(self.store, "learn-example")
        final = tick(self.store, "learn-example")
        self.assertEqual(final["state"], "COMPLETED")
        self.assertEqual(final["items"][0]["result"]["kind"], "source_inventory")
        self.assertIsNone(self.store.revision())

    def test_model_without_verified_cost_gateway_is_blocked(self):
        spec = self.mission(questions=[{"kind": "model_explain", "goal": "Explain architecture", "input_from": []}],
                            permissions={"read_source": True, "run_validators": False, "allow_model": True})
        create_mission(self.store, spec)
        control(self.store, "learn-example", "approve", True)
        control(self.store, "learn-example", "start")
        result = tick(self.store, "learn-example")
        self.assertEqual(result["state"], "WAITING_USER")
        self.assertIn("gateway", result["items"][0]["result"]["reason"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM mission_model_calls").fetchone()[0], 0)

    def test_expired_mission_stops_before_work(self):
        create_mission(self.store, self.mission())
        control(self.store, "learn-example", "approve", True)
        control(self.store, "learn-example", "start")
        row = self.store.db.execute("SELECT body FROM missions").fetchone()[0]
        spec = json.loads(row)
        spec["deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.db.execute("UPDATE missions SET body=?", (canonical(spec),))
        self.store.db.commit()
        self.assertEqual(tick(self.store, "learn-example")["state"], "BUDGET_EXHAUSTED")
        self.assertEqual(self.store.db.execute("SELECT state FROM mission_items").fetchone()[0], "PENDING")

    def test_host_shares_identity_across_clients(self):
        server = AgentServer(self.store.home, idle_seconds=60)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            a = raw_call(self.store.home, {"method": "health"})
            b = raw_call(self.store.home, {"method": "health"})
            self.assertEqual(a["pid"], b["pid"])
            result = raw_call(self.store.home, {"method": "mcp", "payload": {
                "method": "tools/list"}})
            self.assertTrue(any(tool["name"] == "local_experience_route" for tool in result["tools"]))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_new_process_uses_same_host_and_known_solution(self):
        self.publish()
        launcher = Path(__file__).resolve().parents[1] / "tools/llr.py"
        command = [sys.executable, str(launcher), "--home", str(self.store.home)]
        try:
            first = subprocess.run(command + ["host-status"], capture_output=True, text=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr)
            pid = json.loads(first.stdout)["pid"]
            second = subprocess.run(command + ["route", "--cwd", str(self.repo), "--query",
                                               "check file data.json", "--session", "other"],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["status"], "LOCAL_READY")
            health = raw_call(self.store.home, {"method": "health"})
            self.assertEqual(health["pid"], pid)
            stopped = subprocess.run(command + ["host-stop"], capture_output=True, text=True, timeout=10)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            restarted = subprocess.run(command + ["host-status"], capture_output=True, text=True, timeout=20)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertNotEqual(json.loads(restarted.stdout)["pid"], pid)
        finally:
            subprocess.run(command + ["host-stop"], capture_output=True, timeout=10)

    def test_native_readonly_gate_blocks_model_request_after_local_solution(self):
        self.publish()
        policy = json.loads((self.store.home / "automation.json").read_text())
        policy["local_first_readonly"] = True
        (self.store.home / "automation.json").write_text(canonical(policy))
        response = hook(self.store, "codex-hooks", "userPromptSubmitted", {
            "session_id": "desktop-fresh", "cwd": str(self.repo),
            "turn_id": "one", "prompt": "check file data.json"})
        self.assertEqual(response["decision"], "block")
        self.assertIn("Solved locally", response["reason"])
        row = self.store.db.execute("SELECT body FROM client_receipts WHERE event='localSolved'").fetchone()
        self.assertTrue(json.loads(row[0])["model_request_blocked"])

    def test_runner_goal_automatically_compiles_candidate_not_verified_solution(self):
        script = self.root / "probe.py"
        script.write_text('print(\'{"ready":true}\')')
        register_runner(self.store, "sample/android", "ready", {
            "argv": [sys.executable, str(script)], "conditions": {"artifact_sha256": "a" * 64},
            "checks": [{"pointer": "/ready", "expected": True}], "title": "Ready check", "anchors": [],
            "problem": {"intent": "ready", "goal": "Only the approved ready predicate",
                        "patterns": ["check readiness"]}}, True)
        result = run_registered(self.store, "sample/android", "ready", "task", "one", "first")
        self.assertEqual(result["solution_candidate"]["status"], "CANDIDATE")
        self.assertEqual(self.store.db.execute("SELECT lifecycle FROM solutions").fetchone()[0], "candidate")

    def test_installation_can_use_distributed_package_entry(self):
        profile = self.root / "codex"
        result = setup_client(self.store, "sample/android", self.repo, "android",
                              "codex", profile, True)
        self.assertEqual(result["host"], "on-demand shared service")
        self.assertTrue((profile / "config.toml").is_file())

    def test_inferred_preference_does_not_grant_permission(self):
        result = save_preference(self.store, "test-first", "Usually inspect tests first",
                                 "inferred", 0, False)
        self.assertEqual(result["state"], "candidate")
        self.assertFalse(result["grants_authorization"])
        with self.assertRaises(RuntimeErrorBase):
            save_preference(self.store, "test-first", "Always run tests",
                            "explicit", 1, False)

    def test_gateway_reserves_budget_before_one_bounded_request(self):
        spec = self.mission(questions=[{"kind": "model_explain", "goal": "Explain the metadata",
                                       "input_from": []}],
                            permissions={"read_source": True, "run_validators": False, "allow_model": True})
        spec["budgets"].update(max_model_calls=1, max_spend_micro=10000)
        create_mission(self.store, spec)
        control(self.store, "learn-example", "approve", True)
        control(self.store, "learn-example", "start")
        gateway = {"endpoint": "http://127.0.0.1:12345/model", "model": "synthetic",
                   "token_env": "LLR_TEST_TOKEN", "input_micro_per_million": 1_000_000,
                   "output_micro_per_million": 1_000_000, "max_output_tokens": 16,
                   "currency": "USD", "external_hard_cap_verified": True,
                   "allowed_workspaces": ["sample/android"]}
        (self.store.home / "model-gateway.json").write_text(canonical(gateway))
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return canonical({"cost_enforced": True, "usage": {
                    "prompt_tokens": 10, "completion_tokens": 5},
                    "choices": [{"message": {"content": "Unverified static inference"}}]}).encode()
        def send(endpoint, headers, body, timeout):
            self.assertEqual(self.store.db.execute(
                "SELECT state FROM mission_model_calls").fetchone()[0], "RESERVED")
            return json.loads(Response().read(32000))
        with patch.dict("os.environ", {"LLR_TEST_TOKEN": "synthetic-only"}), \
                patch("local_learning_runtime.missions.send_gateway", side_effect=send) as transport:
            result = tick(self.store, "learn-example")
        transport.assert_called_once()
        self.assertTrue(result["items"][0]["result"]["not_runtime_verified"])
        self.assertEqual(self.store.db.execute(
            "SELECT state FROM mission_model_calls").fetchone()[0], "ACCOUNTED")

    def test_global_file_budget_not_reset_for_each_question(self):
        spec = self.mission(questions=[
            {"kind": "inspect_file", "goal": "first", "path": "data.json"},
            {"kind": "inspect_file", "goal": "second", "path": "same.json"}])
        spec["budgets"]["max_files"] = 1
        create_mission(self.store, spec)
        control(self.store, "learn-example", "approve", True)
        control(self.store, "learn-example", "start")
        tick(self.store, "learn-example")
        result = tick(self.store, "learn-example")
        self.assertEqual(result["state"], "WAITING_USER")
        self.assertIn("budget", result["items"][1]["result"]["reason"])

    def test_branch_selection_is_fixed_and_unverified_path_is_blocked(self):
        # A supplied condition is explicitly caller-attested; the branch uses a current file observation.
        self.spec["preconditions"] = [{"kind": "condition_equals", "name": "test_env", "expected": True}]
        self.spec["problem"]["goal"] = "Select the recorded file-size branch; no content-identity claim"
        self.spec["steps"] = [
            {"id": "inspect", "op": "inspect_file", "path": {"input": "path"}},
            {"id": "choose", "op": "branch", "step": "inspect", "pointer": "/bytes",
             "expected": (self.repo / "data.json").stat().st_size,
             "then": [{"id": "normal", "op": "select", "step": "inspect", "pointer": "/sha256"}],
             "else": [{"id": "other", "op": "select", "step": "inspect", "pointer": "/sha256"}]}]
        self.spec["postconditions"] = [{"step": "choose", "pointer": "/selected", "expected": "then"}]
        propose_solution(self.store, self.spec, 0, True)
        result = execute_solution(self.store, "sample/android", "file-integrity", "training", "one",
                                  {"path": "data.json"}, {"test_env": True}, True, True)
        self.assertEqual(result["status"], "LOCAL_SOLVED")
        repeat = execute_solution(self.store, "sample/android", "file-integrity", "new", "one",
                                  {"path": "data.json"}, {"test_env": True})
        self.assertEqual(repeat["outputs"]["choose"]["selected"], "then")
        (self.repo / "data.json").write_text("different size")
        blocked = execute_solution(self.store, "sample/android", "file-integrity", "new", "two",
                                   {"path": "data.json"}, {"test_env": True})
        self.assertEqual(blocked["status"], "FAILED")
        self.assertIn("not been verified", blocked["error"])

    def test_portal_includes_human_solution_not_only_machine_catalog(self):
        self.publish()
        from local_learning_runtime.knowledge import export_portal
        out = self.root / "portal"
        export_portal(self.store, "sample/android", out)
        page = (out / "index.html").read_text()
        self.assertIn("可复用解决流程", page)
        self.assertIn("Verify the approved file identity", page)
        self.assertIn("完整经验已验证", page)

    def test_literal_pattern_keeps_unicode_offsets_and_no_regex_wildcards(self):
        definitions = {"file": {"type": "string", "enum": ["文件.txt"]}}
        pattern = compile_pattern("CHECK 文件 {file}.", definitions)
        self.assertEqual(pattern.fullmatch("check 文件 文件.txt.").groupdict(), {"file": "文件.txt"})
        self.assertIsNone(pattern.fullmatch("check 文件 文件.txtX"))
        self.assertIsNone(pattern.fullmatch("x" * 4000))

    def test_adjacent_parameters_are_rejected_not_guessed(self):
        definitions = {"a": {"type": "string", "enum": ["a"]},
                       "b": {"type": "string", "enum": ["b"]}}
        with self.assertRaisesRegex(RuntimeErrorBase, "delimiter"):
            compile_pattern("check {a}{b}", definitions)


if __name__ == "__main__":
    unittest.main()

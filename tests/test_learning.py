import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

from local_learning_runtime.core import Store, RuntimeErrorBase, canonical
from local_learning_runtime.learning import (authorize, ingest, graph, search, register_runner,
                                             run_registered, forget_workspace, import_debugger_log,
                                             retry_milestone)
from local_learning_runtime.privacy import sanitize
from local_learning_runtime.clients import hook, install_copilot, install_codex, install_codex_hooks
from local_learning_runtime.mcp import respond
from local_learning_runtime.discovery import discover, register_alias


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = Store(self.root / "state")
        self.store.register("test/android", self.repo, "android")
        authorize(self.store, ["test/android"], True)
        self.event = {
            "schema": "llr.milestone.v1", "workspace": "test/android",
            "session": "session-a", "task": "task-one", "milestone": "m1",
            "title": "Profile panel offline navigation",
            "kind": "runtime_observation", "outcome": "PASS",
            "conditions": {"artifact_sha256": "a" * 64, "sdk": "35", "account_kind": "test"},
            "operations": [{"action": "assert_panel"}],
            "observation": {"visible": True, "count": 1}, "anchors": [],
            "checks": [{"pointer": "/visible", "expected": True}],
            "uncertainties": ["Only the specified panel"], "source": {"producer": "fixture"}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_auto_publication_requires_trusted_producer_not_assistant_success(self):
        result = ingest(self.store, self.event)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "CANDIDATE")
        self.assertIsNone(self.store.revision())

    def test_preapproved_observation_published_without_per_claim_click(self):
        result = ingest(self.store, self.event, trusted=True)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "PUBLISHED")
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_multiple_tasks_and_milestones_in_one_session(self):
        ingest(self.store, self.event, trusted=True)
        second = copy.deepcopy(self.event)
        second["milestone"] = "m2"
        ingest(self.store, second, trusted=True)
        third = copy.deepcopy(self.event)
        third.update(task="task-two", milestone="m3")
        ingest(self.store, third, trusted=True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM milestones").fetchone()[0], 3)

    def test_same_event_replay_deduplicates(self):
        first = ingest(self.store, self.event, trusted=True)
        revision = self.store.revision()
        second = ingest(self.store, self.event, trusted=True)
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)
        self.assertEqual(first["tasks"], second["tasks"])

    def test_changed_event_replay_rejected(self):
        ingest(self.store, self.event)
        self.event["observation"]["count"] = 2
        with self.assertRaisesRegex(RuntimeErrorBase, "replay changed"):
            ingest(self.store, self.event)

    def test_failed_and_partial_tasks_are_not_published(self):
        for index, outcome in enumerate(("FAIL", "PARTIAL", "INCONCLUSIVE")):
            event = dict(self.event, milestone="m" + str(index), outcome=outcome)
            result = ingest(self.store, event, trusted=True)
            self.assertEqual(result["tasks"][0]["milestones"][-1]["state"], "CANDIDATE")
        self.assertIsNone(self.store.revision())

    def test_wrong_predicate_no_false_success(self):
        self.event["observation"]["visible"] = False
        result = ingest(self.store, self.event, trusted=True)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "CANDIDATE")
        self.assertIsNone(self.store.revision())

    def test_required_evidence_missing_reports_failure(self):
        self.event["checks"][0]["pointer"] = "/missing"
        result = ingest(self.store, self.event, trusted=True)
        self.assertIn("error", result)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "SAVE_FAILED")
        self.assertIsNone(self.store.revision())

    def test_redaction_does_not_change_verified_value(self):
        self.event["observation"] = {"token": "synthetic-token"}
        self.event["checks"] = [{"pointer": "/token", "expected": "synthetic-token"}]
        result = ingest(self.store, self.event, trusted=True)
        self.assertIn("redacted", result["error"])
        self.assertIsNone(self.store.revision())
        row = self.store.db.execute("SELECT body FROM milestones").fetchone()[0]
        self.assertNotIn("synthetic-token", row)

    def test_unrelated_sensitive_data_redacted_while_predicate_preserved(self):
        self.event["observation"].update(email="fixture@example.com",
                                         token="secret-fixture",
                                         note="Bearer abcd.fixture")
        result = ingest(self.store, self.event, trusted=True)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "PUBLISHED")
        for row in self.store.db.execute("SELECT body FROM milestones"):
            self.assertNotIn("fixture@example.com", row[0])
            self.assertNotIn("secret-fixture", row[0])
            self.assertNotIn("abcd.fixture", row[0])
        for path in self.store.blobs.iterdir():
            raw = path.read_text()
            self.assertNotIn("secret-fixture", raw)

    def test_privacy_markers_preserve_distinct_conditions(self):
        first, _ = sanitize({"account_id": "one"}, b"x" * 32)
        second, _ = sanitize({"account_id": "two"}, b"x" * 32)
        self.assertNotEqual(first, second)

    def test_graph_and_memo_share_authority(self):
        ingest(self.store, self.event, trusted=True)
        projection = graph(self.store, "test/android")
        self.assertEqual(projection["revision"], self.store.revision())
        self.assertTrue(any(edge["relation"] == "supported_by" for edge in projection["edges"]))
        self.assertTrue(any(node["kind"] == "operation" for node in projection["nodes"]))

    def test_search_by_business_task_without_ids(self):
        ingest(self.store, self.event, trusted=True)
        result = search(self.store, self.repo, "offline profile navigation", "session-b")
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(result["matches"][0]["reuse"], "CONDITIONS_UNKNOWN")
        self.assertEqual(result["matches"][0]["title"], self.event["title"])
        changed = dict(self.event["conditions"], sdk="36")
        result = search(self.store, self.repo, "profile", "session-c", changed)
        self.assertEqual(result["matches"][0]["reuse"], "BLOCKED")

    def test_published_evidence_survives_source_session_cleanup(self):
        ingest(self.store, self.event, trusted=True)
        # No temporary source file is needed by the durable stored evidence.
        result = search(self.store, self.repo, "profile", "new", self.event["conditions"])
        self.assertEqual(result["matches"][0]["reuse"], "APPLICABLE")

    def test_publication_failure_can_retry_without_duplicate(self):
        self.store.db.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON snapshots "
                              "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        self.store.db.commit()
        failed = ingest(self.store, self.event, trusted=True)
        self.assertIn("error", failed)
        self.store.db.execute("DROP TRIGGER fail_publish")
        self.store.db.commit()
        retried = ingest(self.store, self.event, trusted=True)
        self.assertEqual(retried["tasks"][0]["milestones"][0]["state"], "PUBLISHED")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)

    def test_concurrent_store_replay(self):
        other = Store(self.store.home)
        try:
            ingest(self.store, self.event, trusted=True)
            ingest(other, self.event, trusted=True)
            self.assertEqual(other.db.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)
        finally:
            other.close()

    def test_registered_runner_auto_persists(self):
        script = self.root / "runtime.py"
        script.write_text('import json\nprint(json.dumps({"visible": True}))\n')
        spec = {"argv": [sys.executable, str(script)], "conditions": self.event["conditions"],
                "checks": [{"pointer": "/visible", "expected": True}],
                "title": "Novel runtime probe", "anchors": []}
        register_runner(self.store, "test/android", "novel", spec, True)
        result = run_registered(self.store, "test/android", "novel", "fresh", "task", "first")
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "PUBLISHED")
        script.write_text('print("changed")')
        with self.assertRaisesRegex(RuntimeErrorBase, "changed"):
            run_registered(self.store, "test/android", "novel", "fresh", "task", "second")

    def test_registered_tool_failure_does_not_publish(self):
        spec = {"argv": [sys.executable, "-c", 'import sys;print("{}");sys.exit(1)'],
                "conditions": self.event["conditions"], "checks": [],
                "title": "Failed runtime probe", "anchors": []}
        register_runner(self.store, "test/android", "failed", spec, True)
        result = run_registered(self.store, "test/android", "failed", "new", "task", "first")
        self.assertEqual(result["tasks"][0]["state"], "FAIL")
        self.assertIsNone(self.store.revision())

    def test_hooks_capture_task_boundaries_not_claim_success(self):
        base = {"sessionId": "hook-session", "cwd": str(self.repo), "timestamp": 1}
        hook(self.store, "copilot", "userPromptSubmitted", dict(base, prompt="First task"))
        hook(self.store, "copilot", "postToolUse", dict(base, toolName="bash",
             toolResult={"resultType": "success", "textResultForLlm": "I succeeded; token=private"}))
        hook(self.store, "copilot", "agentStop", base)
        self.assertIsNone(self.store.revision())
        body = self.store.db.execute("SELECT body FROM milestones").fetchone()[0]
        self.assertNotIn("token=private", body)
        hook(self.store, "copilot", "userPromptSubmitted", dict(base, timestamp=2, prompt="Second task"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM task_runs").fetchone()[0], 2)

    def test_session_start_context_is_bounded_and_relevant(self):
        ingest(self.store, self.event, trusted=True)
        output = hook(self.store, "copilot", "sessionStart",
                      {"sessionId": "new", "cwd": str(self.repo), "initialPrompt": "profile"})
        self.assertLess(len(output["additionalContext"].encode()), 8000)
        self.assertIn("CONDITIONS_UNKNOWN", output["additionalContext"])

    def test_mcp_search_has_actual_receipt(self):
        ingest(self.store, self.event, trusted=True)
        result = respond(self.store, {"method": "tools/call", "params": {
            "name": "local_learning_search", "arguments": {
                "cwd": str(self.repo), "query": "profile", "session": "codex-new"}}})
        self.assertIn("FOUND", result["content"][0]["text"])
        self.assertTrue(self.store.db.execute(
            "SELECT 1 FROM client_receipts WHERE session='codex-new'").fetchone())

    def test_installation_preserves_existing_notify(self):
        profile = self.root / "codex"
        profile.mkdir()
        (profile / "config.toml").write_text('model = "existing"\nnotify = ["/bin/echo", "existing"]\n')
        root = Path(__file__).resolve().parents[1]
        install_codex(self.store, root, profile, True)
        text = (profile / "config.toml").read_text()
        self.assertIn('model = "existing"', text)
        self.assertEqual(json.loads((self.store.home / "codex-notify-chain.json").read_text()),
                         ["/bin/echo", "existing"])
        install_codex(self.store, root, profile, True)
        self.assertEqual(text, (profile / "config.toml").read_text())
        install_copilot(self.store, root, self.root / "copilot", True)
        self.assertTrue((self.root / "copilot/hooks/local-learning-runtime.json").is_file())

    def test_debugger_minimization_does_not_import_all_text(self):
        log = self.root / "debug.log"
        log.write_text('private unrelated document\nmain[1] code = 200\n'
                       'main[1] params.get("token") = "synthetic-secret"\n')
        import_debugger_log(self.store, "test/android", log, "historical", "debug", "Callback", True)
        body = self.store.db.execute("SELECT body FROM milestones").fetchone()[0]
        self.assertNotIn("synthetic-secret", body)
        self.assertNotIn("private unrelated", body)
        self.assertIn("200", body)
        self.assertIsNone(self.store.revision())

    def test_forget_revokes_memo_graph_evidence_and_history(self):
        ingest(self.store, self.event, trusted=True)
        old_revision = self.store.revision()
        forget_workspace(self.store, "test/android", True)
        self.assertEqual(self.store.snapshot(), {})
        self.assertEqual(graph(self.store, "test/android")["nodes"], [])
        self.assertEqual(list(self.store.blobs.iterdir()), [])
        with self.assertRaises(RuntimeErrorBase):
            self.store.snapshot(old_revision)

    def test_desktop_parent_root_requires_task_platform(self):
        ingest(self.store, self.event, trusted=True)
        register_alias(self.store, "test/android", self.root, True, True)
        self.assertEqual(discover(self.store, self.root, "desktop")["status"], "AMBIGUOUS")
        result = search(self.store, self.root, "Android profile panel", "desktop-task")
        self.assertEqual(result["workspace"], "test/android")
        self.assertEqual(result["status"], "FOUND")
        wrong = search(self.store, self.root, "iOS profile panel", "ios-task")
        self.assertEqual(wrong["status"], "UNKNOWN_WORKSPACE")
        both = search(self.store, self.root, "Android and iOS profile", "both")
        self.assertEqual(both["status"], "AMBIGUOUS")

    def test_alias_does_not_change_source_anchor_root(self):
        primary = self.store.workspace("test/android")["root"]
        register_alias(self.store, "test/android", self.root, True, True)
        self.assertEqual(self.store.workspace("test/android")["root"], primary)
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaises(ValueError):
            register_alias(self.store, "test/android", outside, True, True)

    def test_codex_prompt_hook_injects_matches_without_model_tool_selection(self):
        ingest(self.store, self.event, trusted=True)
        register_alias(self.store, "test/android", self.root, True, True)
        output = hook(self.store, "codex-hooks", "userPromptSubmitted", {
            "session_id": "desktop-thread", "cwd": str(self.root), "turn_id": "turn-one",
            "prompt": "Android profile navigation"})
        context = output["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "UserPromptSubmit")
        self.assertIn("CONDITIONS_UNKNOWN", context["additionalContext"])
        self.assertIn("learned-", context["additionalContext"])

    def test_codex_hooks_install_requires_trust_not_bypass(self):
        root = Path(__file__).resolve().parents[1]
        result = install_codex_hooks(self.store, root, self.root / "codex", True)
        self.assertEqual(result["status"], "INSTALLED_REQUIRES_CLIENT_TRUST")
        config = (self.root / "codex/hooks.json").read_text()
        self.assertNotIn("bypass", config)
        self.assertIn("UserPromptSubmit", config)

    def test_mcp_validator_no_per_claim_approval_needed(self):
        script = self.root / "validator.py"
        script.write_text('print(\'{"visible":true,"identity":{"artifact_sha256":"' + "b" * 64 + '"}}\')')
        register_runner(self.store, "test/android", "panel", {
            "argv": [sys.executable, str(script)], "conditions": self.event["conditions"],
            "conditions_pointer": "/identity",
            "checks": [{"pointer": "/visible", "expected": True}],
            "title": "New panel observation", "anchors": []}, True)
        result = respond(self.store, {"method": "tools/call", "params": {
            "name": "local_learning_validate", "arguments": {
                "cwd": str(self.repo), "runner": "panel", "session": "desktop-new",
                "task": "panel", "milestone": "one"}}})
        body = json.loads(result["content"][0]["text"])
        self.assertEqual(body["tasks"][0]["milestones"][0]["state"], "PUBLISHED")
        claim = next(iter(self.store.snapshot().values()))
        self.assertEqual(claim["spec"]["conditions"]["artifact_sha256"], "b" * 64)

    def test_invalid_milestone_never_executes_app(self):
        with patch("local_learning_runtime.learning.subprocess.run") as command:
            with self.assertRaisesRegex(RuntimeErrorBase, "milestone"):
                run_registered(self.store, "test/android", "any", "session", "task", "错误")
            command.assert_not_called()

    def test_actual_parallel_replay_is_idempotent(self):
        def worker():
            store = Store(self.store.home)
            try:
                return ingest(store, self.event, trusted=True)
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertTrue(all("error" not in result for result in results))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM milestones").fetchone()[0], 1)
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_conflicting_candidate_blocks_reuse_without_auto_overwrite(self):
        first = ingest(self.store, self.event, trusted=True)
        event = copy.deepcopy(self.event)
        event["milestone"] = "counterexample"
        event["relations"] = [{"relation": "contradicts", "target": first["event_id"]}]
        result = ingest(self.store, event, trusted=True)
        self.assertEqual(result["tasks"][0]["milestones"][-1]["state"], "CANDIDATE")
        self.assertEqual(len(self.store.snapshot()), 1)
        response = search(self.store, self.repo, "profile", "recheck", self.event["conditions"])
        old = [card for card in response["matches"] if card["kind"] == "published"][0]
        self.assertEqual(old["reuse"], "BLOCKED")

    def test_screenshot_without_predicates_is_candidate(self):
        self.event["checks"] = []
        self.event["observation"] = {"screenshot_claim": "the screenshot looked successful"}
        result = ingest(self.store, self.event, trusted=True)
        self.assertEqual(result["tasks"][0]["milestones"][0]["state"], "CANDIDATE")
        self.assertIsNone(self.store.revision())

    def test_tampered_auto_evidence_blocks_search(self):
        ingest(self.store, self.event, trusted=True)
        claim = next(iter(self.store.snapshot().values()))
        evidence, _ = self.store.evidence(claim["spec"]["checks"][0]["evidence_id"])
        (self.store.blobs / evidence["sha256"]).write_text('{"visible":true}')
        response = search(self.store, self.repo, "profile", "tampered", self.event["conditions"])
        self.assertEqual(response["matches"][0]["reuse"], "BLOCKED")

    def test_automatic_storage_discards_unrelated_user_content(self):
        self.event["observation"]["unrelated"] = "Private document text unrelated to this assertion"
        ingest(self.store, self.event, trusted=True)
        body = self.store.db.execute("SELECT body FROM milestones").fetchone()[0]
        self.assertNotIn("Private document text", body)
        evidence = next(self.store.blobs.iterdir()).read_text()
        self.assertNotIn("Private document text", evidence)
        self.assertIn("source_pointer", evidence)

    def test_retry_does_not_reexecute_validator(self):
        script = self.root / "once.py"
        count = self.root / "count"
        script.write_text('from pathlib import Path\n'
                          'p=Path(' + repr(str(count)) + ')\n'
                          'p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                          'print(\'{"visible":true}\')')
        register_runner(self.store, "test/android", "once", {
            "argv": [sys.executable, str(script)], "conditions": self.event["conditions"],
            "checks": [{"pointer": "/visible", "expected": True}],
            "title": "Once", "anchors": []}, True)
        run_registered(self.store, "test/android", "once", "session", "task", "one")
        run_registered(self.store, "test/android", "once", "session", "task", "one")
        self.assertEqual(count.read_text(), "1")

    def test_persisted_retry_recovers_publication_without_app_action(self):
        self.store.db.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON snapshots "
                              "BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        self.store.db.commit()
        first = ingest(self.store, self.event, trusted=True)
        self.assertIn("error", first)
        self.store.db.execute("DROP TRIGGER fail_publish")
        self.store.db.commit()
        restored = retry_milestone(self.store, first["event_id"])
        self.assertEqual(restored["tasks"][0]["milestones"][0]["state"], "PUBLISHED")

    def test_untrusted_or_redacted_failure_cannot_retry_as_verified(self):
        self.event["observation"] = {"token": "fixture-secret"}
        self.event["checks"] = [{"pointer": "/token", "expected": "fixture-secret"}]
        result = ingest(self.store, self.event, trusted=True)
        with self.assertRaisesRegex(RuntimeErrorBase, "attestation"):
            retry_milestone(self.store, result["event_id"])

    def test_private_state_cannot_be_created_inside_git(self):
        (self.repo / ".git").mkdir()
        with self.assertRaisesRegex(RuntimeErrorBase, "outside Git"):
            Store(self.repo / "private-state")

    def test_parallel_invocations_cannot_repeat_runtime_action(self):
        script = self.root / "slow.py"
        counter = self.root / "executed"
        script.write_text('import time\nfrom pathlib import Path\n'
                          'with open(' + repr(str(counter)) + ', "a") as f: f.write("x")\n'
                          'time.sleep(0.2)\nprint(\'{"visible":true}\')')
        register_runner(self.store, "test/android", "slow", {
            "argv": [sys.executable, str(script)], "conditions": self.event["conditions"],
            "checks": [{"pointer": "/visible", "expected": True}],
            "title": "Slow probe", "anchors": []}, True)
        def worker():
            store = Store(self.store.home)
            try:
                try:
                    return run_registered(store, "test/android", "slow", "shared", "task", "one")
                except RuntimeErrorBase as exc:
                    return {"blocked": str(exc)}
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertTrue(any("tasks" in result for result in results))
        self.assertEqual(counter.read_text(), "x")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM validator_invocations").fetchone()[0], 1)
if __name__ == "__main__":
    unittest.main()

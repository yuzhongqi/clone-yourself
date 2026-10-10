import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_learning_runtime.core import RuntimeErrorBase, Store, canonical, digest, file_digest
from local_learning_runtime.devices import (android_capture, android_identity, android_launch, ios_capture,
                                           run_android_recipe)
from local_learning_runtime.knowledge import catalog, export_portal, import_graph
from local_learning_runtime.acceptance import android_acceptance


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "workspace"
        self.source.mkdir()
        (self.source / "handler.txt").write_text("example handler\n")
        self.home = self.root / "state"
        self.store = Store(self.home)
        self.store.register("example/android", self.source, "android")
        self.context = {"artifact_sha256": "a" * 64, "account_kind": "test", "os_api": "35"}
        result = self.store.observe("event-1", "example/android", self.context,
                                    b'{"theme":"dark","restored":true}', "application/json")
        self.evidence_id = result["evidence_id"]
        self.spec = {"id": "theme-dark", "workspace": "example/android", "task_type": "theme",
                     "statement": "Synthetic observation: theme is dark.",
                     "conditions": dict(self.context),
                     "anchors": [{"path": "handler.txt",
                                  "sha256": file_digest(self.source / "handler.txt")}],
                     "checks": [{"kind": "json_equals", "evidence_id": self.evidence_id,
                                 "pointer": "/theme", "expected": "dark"}],
                     "dependencies": []}
        self.task = {"workspace": "example/android", "task_type": "theme",
                     "conditions": dict(self.context)}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def publish(self, spec=None, version=0, revision=None):
        candidate = self.store.propose(spec or self.spec)
        return self.store.accept(candidate["candidate_id"], version, revision, True)

    def test_publish_recall_and_new_process(self):
        published = self.publish()
        packet = self.store.recall(self.task, "session-a")
        self.assertEqual(packet["status"], "LOCAL_READY")
        self.assertEqual(packet["revision"], published["revision"])
        task_path = self.root / "task.json"
        task_path.write_text(json.dumps(self.task))
        result = subprocess.run([sys.executable, "-m", "local_learning_runtime",
                                 "--home", str(self.home), "recall", str(task_path),
                                 "--session", "session-b"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        packet_b = json.loads(result.stdout)
        self.assertEqual(packet_b["claims"], packet["claims"])
        self.assertEqual(packet_b["model_calls"], 0)

    def test_failed_predicate_cannot_publish(self):
        self.spec["checks"][0]["expected"] = "light"
        with self.assertRaisesRegex(RuntimeErrorBase, "checks failed"):
            self.publish()
        self.assertIsNone(self.store.revision())

    def test_review_approval_required(self):
        candidate = self.store.propose(self.spec)
        with self.assertRaisesRegex(RuntimeErrorBase, "approval"):
            self.store.accept(candidate["candidate_id"], 0, None, False)

    def test_changed_anchor_blocks(self):
        self.publish()
        (self.source / "handler.txt").write_text("changed")
        packet = self.store.recall(self.task, "new-session")
        self.assertEqual(packet["status"], "BLOCKED")
        self.assertIn("Source anchor changed", packet["blocked"][0]["reason"])

    def test_missing_anchor_blocks(self):
        self.publish()
        (self.source / "handler.txt").unlink()
        self.assertEqual(self.store.recall(self.task, "new")["status"], "BLOCKED")

    def test_changed_artifact_blocks(self):
        self.publish()
        self.task["conditions"]["artifact_sha256"] = "b" * 64
        self.assertEqual(self.store.recall(self.task, "new")["status"], "BLOCKED")

    def test_unknown_conditions_do_not_match(self):
        self.publish()
        del self.task["conditions"]["account_kind"]
        self.assertEqual(self.store.recall(self.task, "new")["status"], "BLOCKED")

    def test_publication_cannot_drop_recorded_conditions(self):
        del self.spec["conditions"]["account_kind"]
        with self.assertRaisesRegex(RuntimeErrorBase, "retain every"):
            self.publish()

    def test_tampered_evidence_blocks(self):
        self.publish()
        row, _ = self.store.evidence(self.evidence_id)
        (self.store.blobs / row["sha256"]).write_bytes(b'{"theme":"dark"}')
        packet = self.store.recall(self.task, "new")
        self.assertEqual(packet["status"], "BLOCKED")
        self.assertIn("fingerprint", packet["blocked"][0]["reason"])

    def test_missing_evidence_blocks(self):
        self.publish()
        row, _ = self.store.evidence(self.evidence_id)
        (self.store.blobs / row["sha256"]).unlink()
        self.assertEqual(self.store.recall(self.task, "new")["status"], "BLOCKED")

    def test_event_replay_is_idempotent(self):
        result = self.store.observe("event-1", "example/android", self.context,
                                    b'{"theme":"dark","restored":true}', "application/json")
        self.assertEqual(result["evidence_id"], self.evidence_id)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM evidence").fetchone()[0], 1)

    def test_changed_replay_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase, "replay changed"):
            self.store.observe("event-1", "example/android", self.context,
                               b'{"theme":"light"}', "application/json")

    def test_revision_compare_and_swap(self):
        published = self.publish()
        changed = copy.deepcopy(self.spec)
        changed["statement"] = "Revised wording."
        candidate = self.store.propose(changed)
        with self.assertRaisesRegex(RuntimeErrorBase, "revision conflict"):
            self.store.accept(candidate["candidate_id"], 1, None, True)
        self.assertEqual(self.store.revision(), published["revision"])

    def test_version_compare_and_swap(self):
        published = self.publish()
        candidate = self.store.propose(self.spec)
        with self.assertRaisesRegex(RuntimeErrorBase, "version conflict"):
            self.store.accept(candidate["candidate_id"], 0, published["revision"], True)

    def test_identical_publication_is_noop(self):
        published = self.publish()
        self.assertEqual(self.publish(version=1, revision=published["revision"])["status"], "NOOP")

    def test_old_revision_immutable_and_session_refresh(self):
        first = self.publish()
        self.store.recall(self.task, "old-session")
        original = self.store.snapshot(first["revision"])
        changed = copy.deepcopy(self.spec)
        changed["statement"] = "New wording, same predicates."
        self.publish(changed, 1, first["revision"])
        self.assertEqual(self.store.snapshot(first["revision"]), original)
        self.assertEqual(self.store.refresh("old-session")["changed"], ["theme-dark"])

    def test_anchor_refresh_detects_drift_without_publication(self):
        self.publish()
        self.store.recall(self.task, "old-session")
        (self.source / "handler.txt").write_text("new contents")
        self.assertTrue(self.store.refresh("old-session")["requires_recall"])

    def test_invalidation_blocks_dependents(self):
        first = self.publish()
        dependent = copy.deepcopy(self.spec)
        dependent["id"] = "theme-dependent"
        dependent["task_type"] = "dependent"
        dependent["dependencies"] = ["theme-dark"]
        second = self.publish(dependent, 0, first["revision"])
        self.store.invalidate("theme-dark", "New conflicting observation", "conflicted",
                              1, second["revision"], True)
        task = dict(self.task, task_type="dependent")
        self.assertEqual(self.store.recall(task, "new")["status"], "BLOCKED")

    def test_dependency_change_requires_reacceptance(self):
        first = self.publish()
        dependent = copy.deepcopy(self.spec)
        dependent.update(id="dependent", task_type="dependent", dependencies=["theme-dark"])
        second = self.publish(dependent, 0, first["revision"])
        changed = copy.deepcopy(self.spec)
        changed["statement"] = "Updated claim"
        self.publish(changed, 1, second["revision"])
        self.assertEqual(self.store.recall(dict(self.task, task_type="dependent"), "new")["status"],
                         "BLOCKED")

    def test_missing_dependency_rejected(self):
        self.spec["dependencies"] = ["not-published"]
        with self.assertRaisesRegex(RuntimeErrorBase, "not published"):
            self.publish()

    def test_scope_not_shared_between_platforms(self):
        self.store.register("example/ios", self.source, "ios")
        published = self.publish()
        task = dict(self.task, workspace="example/ios")
        self.assertEqual(self.store.recall(task, "ios-session")["status"], "NEEDS_INVESTIGATION")
        self.assertEqual(self.store.revision(), published["revision"])

    def test_unknown_task_queued_once(self):
        self.store.recall(self.task, "first")
        self.store.recall(self.task, "second")
        self.assertEqual(len(self.store.questions()), 1)

    def test_packet_has_no_chat_history_and_rejects_oversize(self):
        packet = self.store.learning_packet(self.task, "What is missing?")
        self.assertEqual(packet["model_calls"], 0)
        self.assertNotIn("history", packet)
        with self.assertRaises(RuntimeErrorBase):
            self.store.learning_packet(self.task, "x" * 2001)

    def test_path_traversal_rejected(self):
        self.spec["anchors"][0]["path"] = "../outside"
        with self.assertRaisesRegex(RuntimeErrorBase, "escapes"):
            self.publish()

    def test_symlink_escape_rejected(self):
        outside = self.root / "outside"
        outside.write_text("outside")
        (self.source / "link").symlink_to(outside)
        self.spec["anchors"] = [{"path": "link", "sha256": file_digest(outside)}]
        with self.assertRaisesRegex(RuntimeErrorBase, "escapes"):
            self.publish()

    def test_json_pointer_boolean_not_integer(self):
        self.spec["checks"][0].update(pointer="/restored", expected=1)
        with self.assertRaisesRegex(RuntimeErrorBase, "checks failed"):
            self.publish()

    def test_unknown_validator_rejected(self):
        self.spec["checks"][0]["kind"] = "shell"
        with self.assertRaisesRegex(RuntimeErrorBase, "Unknown validator"):
            self.publish()

    def test_ui_selector_must_be_unique(self):
        raw = b'<hierarchy><node resource-id="theme"/><node resource-id="theme"/></hierarchy>'
        evidence = self.store.observe("xml-1", "example/android", self.context,
                                      raw, "application/xml")
        self.spec["checks"] = [{"kind": "ui_node", "evidence_id": evidence["evidence_id"],
                               "selector": {"resource-id": "theme"},
                               "attributes": {"text": "Dark"}}]
        with self.assertRaisesRegex(RuntimeErrorBase, "exactly one"):
            self.publish()

    def test_ui_predicate_passes(self):
        evidence = self.store.observe("xml-1", "example/android", self.context,
                                      b'<hierarchy><node resource-id="theme" text="Dark"/></hierarchy>',
                                      "application/xml")
        self.spec["checks"] = [{"kind": "ui_node", "evidence_id": evidence["evidence_id"],
                               "selector": {"resource-id": "theme"},
                               "attributes": {"text": "Dark"}}]
        self.assertEqual(self.publish()["status"], "PUBLISHED")

    def test_xml_entities_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase, "entities"):
            self.store.observe("xml", "example/android", self.context,
                               b'<!DOCTYPE x [<!ENTITY a "b">]><hierarchy/>', "application/xml")

    def test_device_exclusion_and_explicit_recovery(self):
        with self.store.device_lock("android:test"):
            other = Store(self.home)
            try:
                with self.assertRaisesRegex(RuntimeErrorBase, "locked"):
                    with other.device_lock("android:test"):
                        pass
            finally:
                other.close()
        with self.store.device_lock("android:test"):
            pass
        with self.assertRaisesRegex(RuntimeErrorBase, "Confirm"):
            self.store.unlock_device("android:test", False)

    def test_rollback_preserves_revision(self):
        first = self.publish()
        changed = copy.deepcopy(self.spec)
        changed["statement"] = "Unpublished"
        candidate = self.store.propose(changed)
        self.store.db.execute("CREATE TRIGGER stop_publish BEFORE INSERT ON snapshots "
                              "BEGIN SELECT RAISE(ABORT,'simulated publication failure'); END")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.accept(candidate["candidate_id"], 1, first["revision"], True)
        self.assertIn("simulated publication failure", str(caught.exception))
        self.assertEqual(self.store.revision(), first["revision"])
        self.assertEqual(self.store.db.execute("SELECT version FROM claims").fetchone()[0], 1)

    def test_android_capture_requires_approval_before_tools(self):
        with patch("local_learning_runtime.devices.run") as command:
            with self.assertRaises(RuntimeErrorBase):
                android_capture(self.store, "example/android", "test", "com.example.app", {}, False)
            command.assert_not_called()

    def test_launcher_resolved_and_started_without_monkey(self):
        with patch("local_learning_runtime.devices.run", side_effect=[
                b"com.example.app/.MainActivity\n", b"Status: ok\n"]) as command:
            android_launch(["adb", "-s", "test"], "com.example.app")
        self.assertEqual(command.call_count, 2)
        self.assertIn("resolve-activity", command.call_args_list[0].args[0])
        self.assertIn("start", command.call_args_list[1].args[0])

    def test_installed_apk_path_accepts_android_randomized_tilde(self):
        with patch("local_learning_runtime.devices.run", side_effect=[
                b"device\n", b"35\n", b"package:/data/app/~~random==/com.example.app/base.apk\n",
                ("a" * 64 + "  /data/app/~~random==/com.example.app/base.apk\n").encode()]):
            identity = android_identity(["adb", "-s", "test"], "com.example.app")
        self.assertEqual(identity["os_api"], "35")

    def test_launcher_cannot_resolve_another_package(self):
        with patch("local_learning_runtime.devices.run", return_value=b"other.app/.Main\n"):
            with self.assertRaisesRegex(RuntimeErrorBase, "resolve"):
                android_launch(["adb", "-s", "test"], "com.example.app")

    def test_android_capture_records_installed_identity(self):
        xml = b'<hierarchy><node package="com.example.app" resource-id="home"/></hierarchy>'
        with patch("local_learning_runtime.devices.android_identity", return_value={
                "artifact_sha256": "b" * 64, "os_api": "35", "package": "com.example.app"}), \
                patch("local_learning_runtime.devices.android_ui", return_value=xml), \
                patch("local_learning_runtime.devices.run", return_value=b"\x89PNG\r\n\x1a\nfixture"):
            report = android_capture(self.store, "example/android", "test", "com.example.app",
                                     {"account_kind": "test"}, True)
        self.assertEqual(report["conditions"]["artifact_sha256"], "b" * 64)
        self.store.evidence(report["ui"]["evidence_id"])

    def test_android_capture_rejects_wrong_foreground_app(self):
        with patch("local_learning_runtime.devices.android_identity", return_value={
                "artifact_sha256": "b" * 64, "os_api": "35", "package": "com.example.app"}), \
                patch("local_learning_runtime.devices.android_ui",
                      return_value=b'<hierarchy><node package="another.app"/></hierarchy>'):
            with self.assertRaisesRegex(RuntimeErrorBase, "not visible"):
                android_capture(self.store, "example/android", "test", "com.example.app", {}, True)

    def test_ios_simulator_capture_succeeds_without_promotion(self):
        self.store.register("example/ios", self.source, "ios")
        app = self.root / "Synthetic.app"
        app.mkdir()
        (app / "binary").write_bytes(b"fictional app")
        with patch("local_learning_runtime.devices.run",
                   side_effect=[str(app).encode(), b"\x89PNG\r\n\x1a\nfixture"]):
            result = ios_capture(self.store, "example/ios",
                                 "00000000-0000-0000-0000-000000000001",
                                 "com.example.app", {}, True)
        self.assertIsNone(self.store.revision())
        self.store.evidence(result["screenshot"]["evidence_id"])

    def test_second_store_cannot_overwrite_publication(self):
        other = Store(self.home)
        try:
            first_candidate = self.store.propose(self.spec)
            changed = copy.deepcopy(self.spec)
            changed["statement"] = "Concurrent proposal"
            second_candidate = other.propose(changed)
            self.store.accept(first_candidate["candidate_id"], 0, None, True)
            with self.assertRaisesRegex(RuntimeErrorBase, "revision conflict"):
                other.accept(second_candidate["candidate_id"], 0, None, True)
        finally:
            other.close()

    def test_ios_screenshot_is_not_a_claim(self):
        self.store.register("example/ios", self.source, "ios")
        with patch("local_learning_runtime.devices.run") as command:
            with self.assertRaises(RuntimeErrorBase):
                ios_capture(self.store, "example/ios", "bad-uuid", "com.example.app", {}, True)
            command.assert_not_called()

    def test_recipe_rejects_arbitrary_commands_before_tools(self):
        recipe = {"workspace": "example/android", "device": "test", "app": "com.example.app",
                  "context": {}, "steps": [{"action": "shell", "command": "anything"}], "restore": []}
        with patch("local_learning_runtime.devices.run") as command:
            with self.assertRaisesRegex(RuntimeErrorBase, "Unknown recipe"):
                run_android_recipe(self.store, recipe, True)
            command.assert_not_called()

    def test_recipe_goals_and_restoration_are_separate(self):
        recipe = {"workspace": "example/android", "device": "test", "app": "com.example.app",
                  "context": {}, "steps": [
                      {"action": "assert_node", "resource_id": "theme",
                       "attributes": {"text": "Dark"}}], "restore": [
                      {"action": "assert_node", "resource_id": "theme",
                       "attributes": {"text": "System"}}]}
        xml = b'<hierarchy><node package="com.example.app" resource-id="theme" text="Dark"/></hierarchy>'
        with patch("local_learning_runtime.devices.android_identity", return_value={
                "artifact_sha256": "a" * 64, "os_api": "35", "package": "com.example.app"}), \
                patch("local_learning_runtime.devices.android_ui", return_value=xml):
            report = run_android_recipe(self.store, recipe, True)
        self.assertEqual(report["goal_verdict"], "PASS")
        self.assertEqual(report["restoration"], "FAIL")
        self.assertEqual(report["verdict"], "PARTIAL")
        row, raw = self.store.evidence(report["report_evidence_id"])
        self.assertEqual(json.loads(raw)["restoration"], "FAIL")

    def test_legacy_import_preserves_partial_without_publishing(self):
        source = self.root / "graph.json"
        source.write_text(json.dumps({"scope": "test", "nodes": [
            {"id": "J05", "title": "Example journey", "status": "partial",
             "summary": "Not completely verified"}]}))
        first = import_graph(self.store, "example/android", source, ["J05"], True)
        second = import_graph(self.store, "example/android", source, ["J05"], True)
        self.assertEqual(first["ids"], second["ids"])
        data = catalog(self.store, "example/android")
        self.assertEqual(len(data["legacy"]), 1)
        self.assertEqual(data["legacy"][0]["source_node"]["status"], "partial")
        self.assertIsNone(data["revision"])
        self.assertEqual(self.store.recall(self.task, "new")["status"], "NEEDS_INVESTIGATION")

    def test_legacy_import_requires_approval(self):
        with self.assertRaisesRegex(RuntimeErrorBase, "approval"):
            import_graph(self.store, "example/android", self.root / "missing", ["J05"], False)

    def test_portal_and_json_share_revision_and_escape_html(self):
        self.spec["statement"] = '<script>alert("unsafe")</script>'
        published = self.publish()
        output = self.root / "portal"
        result = export_portal(self.store, "example/android", output)
        data = json.loads((output / "catalog.json").read_text())
        page = (output / "index.html").read_text()
        self.assertEqual(result["revision"], published["revision"])
        self.assertEqual(data["revision"], published["revision"])
        self.assertNotIn('<script>alert("unsafe")</script>', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Content-Security-Policy", page)
        self.assertIn(self.evidence_id, page)
        with self.assertRaisesRegex(RuntimeErrorBase, "already exists"):
            export_portal(self.store, "example/android", output)

    def test_portal_marks_missing_evidence_blocked(self):
        self.publish()
        row, _ = self.store.evidence(self.evidence_id)
        (self.store.blobs / row["sha256"]).unlink()
        output = self.root / "portal"
        export_portal(self.store, "example/android", output)
        data = json.loads((output / "catalog.json").read_text())
        self.assertEqual(data["claims"][0]["local_integrity"]["status"], "BLOCKED")

    def test_catalog_does_not_label_published_candidate_unpublished(self):
        self.publish()
        self.assertEqual(catalog(self.store, "example/android")["candidates"], [])

    def test_real_acceptance_requires_explicit_approval_before_device_calls(self):
        with patch("local_learning_runtime.devices.run") as command:
            with self.assertRaisesRegex(RuntimeErrorBase, "approval"):
                android_acceptance(self.store, "example/android", "test", "com.example.app",
                                   "com.example.app:id/home", {}, self.root / "out", False)
            command.assert_not_called()

    def test_real_acceptance_requires_app_owned_selector(self):
        with self.assertRaisesRegex(RuntimeErrorBase, "owned"):
            android_acceptance(self.store, "example/android", "test", "com.example.app",
                               "another.app:id/home", {}, self.root / "out", True)


if __name__ == "__main__":
    unittest.main()

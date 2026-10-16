import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from local_learning_runtime.core import Store, RuntimeErrorBase, canonical, digest, file_digest
from local_learning_runtime.lifecycle import configure, assess, usage, record, maintenance
from local_learning_runtime.learning import authorize, search, forget_workspace, ingest
from local_learning_runtime.context import packet, refresh_context
from local_learning_runtime.solutions import propose_solution, execute_solution, route, invalidate_solution
from local_learning_runtime.knowledge import export_portal


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "a.txt").write_text("fixture")
        (self.repo / "b.txt").write_text("fixture")
        self.store = Store(self.root / "state")
        self.store.register("sample/generic", self.repo, "generic")
        authorize(self.store, ["sample/generic"], True)
        self.start = datetime.now(timezone.utc)
        self.parameters = {"file": "a.txt"}
        self.case = digest(canonical(self.parameters).encode())
        self.spec = {"schema": "llr.solution.v1", "id": "identity", "workspace": "sample/generic",
                     "problem": {"intent": "file", "goal": "Verify fixture identity",
                                 "patterns": ["check {file}"]},
                     "parameters": {"file": {"type": "string", "enum": ["a.txt", "b.txt"]}},
                     "preconditions": [{"kind": "file_sha256", "path": {"input": "file"},
                                        "expected": file_digest(self.repo / "a.txt")}],
                     "steps": [{"id": "read", "op": "inspect_file", "path": {"input": "file"}}],
                     "postconditions": [{"step": "read", "pointer": "/sha256",
                                         "expected": file_digest(self.repo / "a.txt")}],
                     "result": {"sha256": {"step": "read", "pointer": "/sha256"}},
                     "boundaries": ["Fixture identity only"]}
        self.policy = {"knowledge_type": "source", "max_age_seconds": 100,
                       "review_after_seconds": 50, "archive_after_seconds": 200,
                       "required_dynamic": []}
        self.conditions = {"artifact_sha256": "a" * 64}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def clock(self, seconds):
        stack = ExitStack()
        value = (self.start + timedelta(seconds=seconds)).isoformat()
        for module in ("lifecycle", "core", "solutions", "learning"):
            stack.enter_context(patch("local_learning_runtime." + module + ".now", return_value=value))
        return stack

    def train(self):
        with self.clock(0):
            propose_solution(self.store, self.spec, 0, True)
            result = execute_solution(self.store, "sample/generic", "identity", "training", "first",
                                      self.parameters, {}, True, True)
            self.assertEqual(result["status"], "LOCAL_SOLVED")
            configure(self.store, "sample/generic", "solution", "identity", self.policy, True)

    def claim(self):
        with self.clock(0):
            proof = self.store.observe("fixture", "sample/generic", self.conditions,
                                       b'{"ready":true}', "application/json")
            spec = {"id": "ready", "workspace": "sample/generic", "task_type": "ready",
                    "statement": "Synthetic ready predicate", "conditions": self.conditions,
                    "anchors": [], "dependencies": [],
                    "checks": [{"kind": "json_equals", "evidence_id": proof["evidence_id"],
                                "pointer": "/ready", "expected": True}]}
            candidate = self.store.propose(spec)
            self.store.accept(candidate["candidate_id"], 0, None, True)
            configure(self.store, "sample/generic", "claim", "ready", self.policy, True)

    def health(self):
        return assess(self.store, "sample/generic", "solution", "identity", self.case)

    def test_exact_age_threshold_and_review_window(self):
        self.train()
        with self.clock(49):
            self.assertEqual(self.health()["status"], "FRESH")
            self.assertFalse(self.health()["review_due"])
        with self.clock(50):
            self.assertTrue(self.health()["review_due"])
        with self.clock(100):
            self.assertEqual(self.health()["status"], "EXPIRED")
            self.assertEqual(route(self.store, self.repo, "check a.txt", "new")["status"], "BLOCKED")

    def test_reads_never_renew_time_or_inflate_same_action(self):
        self.claim()
        with self.clock(80):
            for _ in range(10):
                search(self.store, self.repo, "ready", "same-session")
            health = assess(self.store, "sample/generic", "claim", "ready")
            self.assertEqual(health["usage"]["counts"]["retrieved"], 1)
            self.assertEqual(health["usage"]["counts"]["verified_success"], 0)
            self.assertEqual(health["last_verified_at"], self.start.isoformat())
        with self.clock(101):
            result = search(self.store, self.repo, "ready", "fresh-session", self.conditions)
            self.assertEqual(result["matches"][0]["reuse"], "BLOCKED")
            self.assertEqual(result["matches"][0]["memory"]["status"], "EXPIRED")

    def test_expiry_enforced_on_core_recall_and_pinned_context(self):
        self.claim()
        initial = packet(self.store, self.repo, "ready", "reader", 2)
        with self.clock(101):
            recall = self.store.recall({"workspace": "sample/generic", "task_type": "ready",
                                        "conditions": self.conditions}, "new")
            self.assertEqual(recall["status"], "BLOCKED")
            context = packet(self.store, self.repo, "ready", "reader", 2, revision=initial["revision"])
            self.assertEqual(context["items"][0]["reuse"], "BLOCKED")

    def test_expired_solution_never_executes_or_renews(self):
        self.train()
        with self.clock(101):
            before = self.store.db.execute("SELECT count(*) FROM solution_runs").fetchone()[0]
            result = execute_solution(self.store, "sample/generic", "identity", "new", "one",
                                      self.parameters, {})
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM solution_runs").fetchone()[0], before)
            self.assertEqual(self.health()["usage"]["counts"]["rejected"], 1)
            self.assertEqual(self.health()["last_verified_at"], self.start.isoformat())

    def test_approved_revalidation_renews_actual_case_only(self):
        self.train()
        with self.clock(0):
            execute_solution(self.store, "sample/generic", "identity", "training", "other",
                             {"file": "b.txt"}, {}, True, True)
        with self.clock(110):
            execute_solution(self.store, "sample/generic", "identity", "retest", "one",
                             self.parameters, {}, True, True)
            self.assertEqual(self.health()["status"], "FRESH")
            other = assess(self.store, "sample/generic", "solution", "identity",
                           digest(canonical({"file": "b.txt"}).encode()))
            self.assertEqual(other["status"], "EXPIRED")
            self.assertEqual(assess(self.store, "sample/generic", "solution", "identity")["status"], "EXPIRED")
            overview = maintenance(self.store, "sample/generic")
            self.assertTrue(any(item["case_key"] != self.case for item in overview["items"]))

    def test_new_version_does_not_inherit_old_usage_or_verification(self):
        self.train()
        with self.clock(10):
            execute_solution(self.store, "sample/generic", "identity", "new", "one",
                             self.parameters, {})
        propose_solution(self.store, self.spec, 1, True)
        self.assertEqual(self.health()["usage"]["counts"]["executed"], 0)
        self.assertEqual(self.health()["status"], "BLOCKED")

    def test_unknown_and_future_verification_time_fail_closed(self):
        self.train()
        self.store.db.execute("DELETE FROM memory_events")
        self.store.db.execute("UPDATE evidence SET created_at='not-a-time'")
        self.store.db.commit()
        self.assertEqual(self.health()["status"], "UNKNOWN")
        self.store.db.execute("UPDATE evidence SET created_at=?",
                              ((self.start + timedelta(days=2)).isoformat(),))
        self.store.db.commit()
        self.assertEqual(self.health()["status"], "UNKNOWN")

    def test_missing_dynamic_conditions_cannot_reuse(self):
        self.spec["preconditions"].append({"kind": "condition_equals",
                                           "name": "remote_config_revision", "expected": "r1"})
        with self.clock(0):
            propose_solution(self.store, self.spec, 0, True)
            execute_solution(self.store, "sample/generic", "identity", "training", "one",
                             self.parameters, {"remote_config_revision": "r1"}, True, True)
        policy = dict(self.policy, required_dynamic=["remote_config_revision"])
        configure(self.store, "sample/generic", "solution", "identity", policy, True)
        result = route(self.store, self.repo, "check a.txt", "new")
        self.assertEqual(result["status"], "NEEDS_INPUT")
        result = execute_solution(self.store, "sample/generic", "identity", "new", "one",
                                  self.parameters, {})
        self.assertEqual(result["status"], "NEEDS_INPUT")

    def test_failed_execution_requires_review_not_popularity(self):
        self.spec["preconditions"] = [{"kind": "condition_equals", "name": "test", "expected": True}]
        with self.clock(0):
            propose_solution(self.store, self.spec, 0, True)
            execute_solution(self.store, "sample/generic", "identity", "training", "one",
                             self.parameters, {"test": True}, True, True)
            configure(self.store, "sample/generic", "solution", "identity", self.policy, True)
        (self.repo / "a.txt").write_text("changed")
        with self.clock(10):
            result = execute_solution(self.store, "sample/generic", "identity", "new", "one",
                                      self.parameters, {"test": True})
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(self.health()["status"], "REVIEW_REQUIRED")
        with self.clock(11):
            for index in range(10):
                record(self.store, "sample/generic", "solution", "identity", 1, "retrieved",
                       "reader-" + str(index), "fixture", self.case)
            self.assertEqual(self.health()["status"], "REVIEW_REQUIRED")
        (self.repo / "a.txt").write_text("fixture")
        with self.clock(20):
            execute_solution(self.store, "sample/generic", "identity", "revalidate", "one",
                             self.parameters, {"test": True}, True, True)
            self.assertEqual(self.health()["status"], "FRESH")

    def test_execution_replay_does_not_inflate_counts(self):
        self.train()
        with self.clock(10):
            for _ in range(3):
                execute_solution(self.store, "sample/generic", "identity", "new", "one",
                                 self.parameters, {})
            health = self.health()
            self.assertEqual(health["usage"]["counts"]["executed"], 2)
            self.assertEqual(health["usage"]["counts"]["verified_success"], 2)
            self.assertEqual(health["usage"]["success_rate"], 1)

    def test_frequently_used_aging_memo_prioritized_without_new_experiment(self):
        self.train()
        with self.clock(60):
            for index in range(3):
                route(self.store, self.repo, "check a.txt", "chooser-" + str(index))
            with patch("subprocess.run") as command:
                result = maintenance(self.store, "sample/generic")
            command.assert_not_called()
            self.assertEqual(result["items"][0]["priority"], 60)
            self.assertIn("Frequently used", " ".join(result["items"][0]["reasons"]))
            self.assertEqual(self.health()["last_verified_at"], self.start.isoformat())

    def test_source_drift_detected_without_validator(self):
        self.train()
        (self.repo / "a.txt").write_text("changed")
        result = maintenance(self.store, "sample/generic")
        self.assertEqual(result["items"][0]["priority"], 100)
        self.assertIn("changed", " ".join(result["items"][0]["reasons"]))

    def test_long_inactive_is_advice_not_deletion(self):
        self.train()
        with self.clock(201):
            result = maintenance(self.store, "sample/generic")
            self.assertTrue(result["items"][0]["health"]["archive_suggested"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM solutions").fetchone()[0], 1)

    def test_expansion_is_observed_not_verified(self):
        self.train()
        context = packet(self.store, self.repo, "fixture", "reader", 2)
        node = "solution:sample/generic:identity"
        packet(self.store, self.repo, "fixture", "reader", 3, node, context["revision"])
        packet(self.store, self.repo, "fixture", "reader", 3, node, context["revision"])
        stats = usage(self.store, "sample/generic", "solution", "identity", 1)["counts"]
        self.assertEqual(stats["executed"], 1)
        self.assertEqual(stats["verified_success"], 1)
        self.assertGreater(stats["expanded"], 0)
        with self.clock(101):
            self.assertTrue(refresh_context(self.store, "reader")["requires_reload"])

    def test_policy_requires_approval_and_valid_scope(self):
        self.train()
        with self.assertRaises(RuntimeErrorBase):
            configure(self.store, "sample/generic", "solution", "identity", self.policy, False)
        with self.assertRaises(RuntimeErrorBase):
            configure(self.store, "sample/generic", "solution", "identity",
                      dict(self.policy, review_after_seconds=101), True)
        with self.assertRaises(RuntimeErrorBase):
            assess(self.store, "different/generic", "solution", "identity")
        with self.assertRaisesRegex(RuntimeErrorBase, "bound"):
            configure(self.store, "sample/generic", "solution", "identity",
                      dict(self.policy, required_dynamic=["undeclared_config"]), True)

    def test_missing_solution_proof_blocks_route(self):
        self.train()
        proof = self.store.db.execute("SELECT sha256 FROM evidence LIMIT 1").fetchone()[0]
        (self.store.blobs / proof).unlink()
        self.assertEqual(self.health()["status"], "BLOCKED")
        self.assertEqual(route(self.store, self.repo, "check a.txt", "new")["status"], "BLOCKED")

    def test_unresolved_interruption_requires_review(self):
        self.train()
        self.store.db.execute("UPDATE solution_runs SET state='RUNNING'")
        self.store.db.commit()
        self.assertEqual(self.health()["status"], "BLOCKED")
        # The missing valid success proof is also an explicit block, never a fresh result.
        self.assertIn("provenance", " ".join(self.health()["reasons"]))

    def test_type_defaults_are_distinct_and_overridable(self):
        self.train()
        self.claim()
        self.store.db.execute("DELETE FROM memory_policies")
        self.store.db.commit()
        solution = self.health()
        claim = assess(self.store, "sample/generic", "claim", "ready")
        self.assertEqual(solution["policy"]["max_age_seconds"], 30 * 86400)
        self.assertEqual(claim["policy"]["max_age_seconds"], 7 * 86400)
        remote = dict(self.policy, knowledge_type="remote", max_age_seconds=3600, review_after_seconds=1800)
        configure(self.store, "sample/generic", "claim", "ready", remote, True)
        with self.clock(3600):
            self.assertEqual(assess(self.store, "sample/generic", "claim", "ready")["status"], "EXPIRED")

    def test_new_real_observation_can_share_blob_without_reusing_its_old_capture_age(self):
        event = {"schema": "llr.milestone.v1", "workspace": "sample/generic",
                 "session": "producer", "task": "ready", "milestone": "first",
                 "title": "Ready observation", "kind": "runtime_observation", "outcome": "PASS",
                 "conditions": self.conditions, "operations": [{"action": "trusted_fixture"}],
                 "observation": {"ready": True}, "checks": [{"pointer": "/ready", "expected": True}],
                 "anchors": [], "uncertainties": ["Synthetic fixture"]}
        with self.clock(0):
            first = ingest(self.store, event, trusted=True)
        first_id = "learned-" + first["event_id"]
        with self.clock(200):
            second = ingest(self.store, dict(event, milestone="second"), trusted=True)
            second_id = "learned-" + second["event_id"]
            # A true new observation may deduplicate identical bytes, but has a new producer milestone.
            health = assess(self.store, "sample/generic", "claim", second_id)
            self.assertEqual(health["last_verified_at"], (self.start + timedelta(seconds=200)).isoformat())
            self.assertEqual(assess(self.store, "sample/generic", "claim", first_id)["last_verified_at"],
                             self.start.isoformat())
        with self.clock(300):
            ingest(self.store, dict(event, milestone="second"), trusted=True)
            self.assertEqual(assess(self.store, "sample/generic", "claim", second_id)["last_verified_at"],
                             (self.start + timedelta(seconds=200)).isoformat())

    def test_policy_update_changes_context_revision_but_not_verification_date(self):
        self.train()
        previous = packet(self.store, self.repo, "fixture", "reader", 2)
        before = self.health()["last_verified_at"]
        configure(self.store, "sample/generic", "solution", "identity",
                  dict(self.policy, max_age_seconds=200), True)
        updated = packet(self.store, self.repo, "fixture", "reader", 2)
        self.assertNotEqual(previous["revision"], updated["revision"])
        self.assertEqual(self.health()["last_verified_at"], before)

    def test_maintenance_paging_reports_omissions(self):
        self.train()
        with self.clock(0):
            execute_solution(self.store, "sample/generic", "identity", "training", "other",
                             {"file": "b.txt"}, {}, True, True)
        with self.clock(101):
            first = maintenance(self.store, "sample/generic", limit=1)
            self.assertEqual(first["total"], 2)
            self.assertEqual(first["omitted_count"], 1)
            second = maintenance(self.store, "sample/generic", limit=1, cursor=first["next_cursor"])
            self.assertNotEqual(first["items"][0]["case_key"], second["items"][0]["case_key"])

    def test_portal_shows_dates_outcomes_and_reasons(self):
        self.train()
        with self.clock(101):
            export_portal(self.store, "sample/generic", self.root / "portal")
        page = (self.root / "portal/index.html").read_text()
        self.assertIn("证据已过期", page)
        self.assertIn("知识维护建议", page)
        self.assertIn("最近有效验证", page)
        self.assertIn("检索不会刷新验证日期", page)
        self.assertIn("完整经验已验证 · 当前需复验", page)
        self.assertIn("已到建议复验时间", page)
        data = json.loads((self.root / "portal/catalog.json").read_text())
        self.assertTrue(data["maintenance"]["items"])

    def test_delete_workspace_removes_policy_and_usage(self):
        self.train()
        forget_workspace(self.store, "sample/generic", True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM memory_events").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM memory_policies").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

"""Synthetic offline learning round. Never accesses an app, account, or network."""

import json
import tempfile
from pathlib import Path

from local_learning_runtime.core import Store, file_digest


def main():
    with tempfile.TemporaryDirectory(prefix="llr-demo-") as directory:
        root = Path(directory)
        workspace = root / "example-repo"
        workspace.mkdir()
        source = workspace / "theme.txt"
        source.write_text("Synthetic theme implementation\n", encoding="utf-8")
        store = Store(root / "memory")
        try:
            store.register("demo/android", workspace, "android")
            conditions = {"artifact_sha256": "a" * 64, "account_kind": "synthetic"}
            evidence = store.observe("synthetic-event", "demo/android", conditions,
                                     b'{"selected":"Dark","restored":true}', "application/json")
            spec = {"id": "theme-dark", "workspace": "demo/android", "task_type": "theme",
                    "statement": "Synthetic fixture reports Dark; not a real app verification.",
                    "conditions": conditions,
                    "anchors": [{"path": "theme.txt", "sha256": file_digest(source)}],
                    "checks": [{"kind": "json_equals", "evidence_id": evidence["evidence_id"],
                                "pointer": "/selected", "expected": "Dark"}],
                    "dependencies": []}
            candidate = store.propose(spec)
            published = store.accept(candidate["candidate_id"], 0, None, approve=True)
            task = {"workspace": "demo/android", "task_type": "theme", "conditions": conditions}
            first = store.recall(task, "session-a")
        finally:
            store.close()
        new_session = Store(root / "memory")
        try:
            second = new_session.recall(task, "session-b")
            assert first["claims"] == second["claims"]
            source.write_text("Changed implementation\n", encoding="utf-8")
            stale = new_session.recall(task, "session-c")
            assert stale["status"] == "BLOCKED"
            print(json.dumps({"publication": published, "session_a": first["status"],
                              "session_b": second["status"], "after_source_change": stale["status"],
                              "model_calls": 0}, indent=2))
        finally:
            new_session.close()


if __name__ == "__main__":
    main()

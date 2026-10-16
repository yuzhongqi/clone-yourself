"""Deterministic validation, immutable evidence, and transactional knowledge."""

import hashlib
import json
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_PACKET_BYTES = 12_000
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RuntimeErrorBase(Exception):
    """Expected fail-closed error, reported by the CLI."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeErrorBase(message)


def object_fields(value: Any, required: set, optional: Optional[set] = None) -> None:
    require(isinstance(value, dict), "Expected a JSON object")
    require(required <= value.keys(), "Missing fields: " + ", ".join(sorted(required - value.keys())))
    require(value.keys() <= required | (optional or set()), "Unexpected object fields")


def identifier(value: Any, field: str) -> None:
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None,
            field + " must be a bounded identifier")


def conditions(value: Any) -> None:
    require(isinstance(value, dict) and bool(value), "Conditions must be a nonempty object")
    require(len(value) <= 32, "Too many condition fields")
    for key, item in value.items():
        identifier(key, "Condition key")
        require(type(item) in (str, bool, int) and (not isinstance(item, str) or len(item) <= 1000),
                "Conditions require bounded scalar values")
    require(isinstance(value.get("artifact_sha256"), str)
            and SHA256.fullmatch(value["artifact_sha256"]) is not None,
            "Conditions must include the observed artifact_sha256")


def match_conditions(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    return all(key in actual and type(actual[key]) is type(value) and actual[key] == value
               for key, value in expected.items())


def workspace_file(root: Path, relative: str) -> Path:
    require(isinstance(relative, str) and bool(relative) and not Path(relative).is_absolute(),
            "Anchor must be workspace-relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeErrorBase("Anchor escapes workspace") from exc
    require(path.is_file(), "Anchor is not an available file: " + relative)
    return path


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require_private_location(path: Path) -> None:
    resolved = path.expanduser().resolve()
    for parent in (resolved,) + tuple(resolved.parents):
        require(not (parent / ".git").exists(),
                "Private runtime data/output must be outside Git worktrees")


def json_pointer(document: Any, pointer: str) -> Any:
    require(isinstance(pointer, str) and (pointer == "" or pointer.startswith("/")),
            "Invalid JSON pointer")
    current = document
    for part in pointer.split("/")[1:] if pointer else []:
        require(re.search(r"~(?![01])", part) is None, "Invalid JSON pointer escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            require(key in current, "JSON pointer key missing: " + key)
            current = current[key]
        elif isinstance(current, list):
            require(re.fullmatch(r"0|[1-9][0-9]*", key) is not None, "Invalid array index")
            require(int(key) < len(current), "JSON pointer index out of range")
            current = current[int(key)]
        else:
            raise RuntimeErrorBase("JSON pointer crosses a scalar")
    return current


def check_evidence(check: Dict[str, Any], raw: bytes, media_type: str) -> bool:
    require(isinstance(check, dict), "Check must be an object")
    if check.get("kind") == "json_equals":
        object_fields(check, {"kind", "evidence_id", "pointer", "expected"})
        require(media_type == "application/json", "JSON check requires JSON evidence")
        value = json_pointer(json.loads(raw), check["pointer"])
        return canonical(value) == canonical(check["expected"])
    if check.get("kind") == "ui_node":
        object_fields(check, {"kind", "evidence_id", "selector", "attributes"})
        require(media_type == "application/xml", "UI check requires XML evidence")
        for field in ("selector", "attributes"):
            require(isinstance(check[field], dict) and bool(check[field])
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in check[field].items()), field + " must contain string attributes")
        require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
                "XML declarations with entities are prohibited")
        nodes = [node for node in ET.fromstring(raw).iter("node")
                 if all(node.get(k) == v for k, v in check["selector"].items())]
        require(len(nodes) == 1, "UI selector must match exactly one node")
        return all(nodes[0].get(k) == v for k, v in check["attributes"].items())
    raise RuntimeErrorBase("Unknown validator kind; arbitrary commands are not permitted")


class Store:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        require_private_location(self.home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.home / "evidence"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.home / "runtime.sqlite"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, root TEXT NOT NULL, platform TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                workspace TEXT NOT NULL REFERENCES workspaces(id), platform TEXT NOT NULL,
                conditions TEXT NOT NULL, media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                body TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                revision TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, revision TEXT,
                claim_ids TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS device_locks (
                device TEXT PRIMARY KEY, owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_intake (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                source_sha256 TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discoveries (
                session TEXT PRIMARY KEY, cwd TEXT NOT NULL,
                workspace TEXT, revision TEXT, manifest TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                session TEXT NOT NULL, task TEXT NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS milestones (
                id TEXT PRIMARY KEY, task_run TEXT NOT NULL REFERENCES task_runs(id),
                input_sha TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
                reason TEXT NOT NULL, claim_id TEXT, revision TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_relations (
                source TEXT NOT NULL REFERENCES milestones(id),
                relation TEXT NOT NULL, target TEXT NOT NULL REFERENCES milestones(id),
                PRIMARY KEY(source,relation,target));
            CREATE TABLE IF NOT EXISTS client_receipts (
                id TEXT PRIMARY KEY, client TEXT NOT NULL, session TEXT NOT NULL,
                event TEXT NOT NULL, workspace TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspace_aliases (
                workspace TEXT NOT NULL REFERENCES workspaces(id), root TEXT NOT NULL,
                requires_platform INTEGER NOT NULL,
                PRIMARY KEY(workspace,root));
            CREATE TABLE IF NOT EXISTS milestone_attestations (
                id TEXT PRIMARY KEY REFERENCES milestones(id), signature TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS validator_invocations (
                id TEXT PRIMARY KEY, task_run TEXT NOT NULL REFERENCES task_runs(id),
                state TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS solutions (
                id TEXT NOT NULL, workspace TEXT NOT NULL REFERENCES workspaces(id),
                version INTEGER NOT NULL, lifecycle TEXT NOT NULL,
                body TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace,id));
            CREATE TABLE IF NOT EXISTS solution_versions (
                workspace TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
                body TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(workspace,id,version));
            CREATE TABLE IF NOT EXISTS solution_runs (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, solution TEXT NOT NULL,
                version INTEGER NOT NULL, session TEXT NOT NULL, input_sha TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS solution_cases (
                workspace TEXT NOT NULL, solution TEXT NOT NULL, version INTEGER NOT NULL,
                parameters_sha TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES solution_runs(id),
                PRIMARY KEY(workspace,solution,version,parameters_sha));
            CREATE TABLE IF NOT EXISTS themes (
                workspace TEXT NOT NULL REFERENCES workspaces(id), id TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(workspace,id));
            CREATE TABLE IF NOT EXISTS context_views (
                revision TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_reads (
                session TEXT NOT NULL, workspace TEXT NOT NULL, revision TEXT NOT NULL,
                node TEXT NOT NULL, PRIMARY KEY(session,workspace,revision,node));
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                state TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mission_items (
                mission TEXT NOT NULL REFERENCES missions(id), ordinal INTEGER NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL, result TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(mission,ordinal));
            CREATE TABLE IF NOT EXISTS mission_model_calls (
                id TEXT PRIMARY KEY, mission TEXT NOT NULL REFERENCES missions(id),
                reserved_micro INTEGER NOT NULL, actual_micro INTEGER,
                state TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences (
                id TEXT PRIMARY KEY, workspace TEXT, version INTEGER NOT NULL,
                kind TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_policies (
                workspace TEXT NOT NULL REFERENCES workspaces(id), kind TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace,kind,subject));
            CREATE TABLE IF NOT EXISTS memory_events (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                kind TEXT NOT NULL, subject TEXT NOT NULL, version INTEGER NOT NULL,
                case_key TEXT NOT NULL, event TEXT NOT NULL, session TEXT NOT NULL,
                source TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_events_subject ON memory_events
                (workspace,kind,subject,version,case_key,occurred_at);
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def register(self, workspace: str, root: Path, platform: str) -> Dict[str, Any]:
        identifier(workspace, "Workspace")
        require(platform in ("android", "ios", "generic"), "Platform must be android, ios or generic")
        root = root.expanduser().resolve()
        require(root.is_dir(), "Workspace root must exist")
        with self.transaction():
            old = self.db.execute("SELECT * FROM workspaces WHERE id=?", (workspace,)).fetchone()
            require(old is None or (old["root"] == str(root) and old["platform"] == platform),
                    "Workspace identity already registered differently; choose a new ID")
            self.db.execute("INSERT OR IGNORE INTO workspaces VALUES (?,?,?)",
                            (workspace, str(root), platform))
        return {"workspace": workspace, "root": str(root), "platform": platform}

    def workspace(self, workspace: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM workspaces WHERE id=?", (workspace,)).fetchone()
        require(row is not None, "Workspace is not registered")
        return row

    def observe(self, event_id: str, workspace: str, observed: Dict[str, Any],
                raw: bytes, media_type: str) -> Dict[str, Any]:
        identifier(event_id, "Event ID")
        conditions(observed)
        scope = self.workspace(workspace)
        require(media_type in ("application/json", "application/xml", "image/png"),
                "Unsupported evidence type")
        require(0 < len(raw) <= MAX_EVIDENCE_BYTES, "Evidence must be nonempty and <=16 MiB")
        if media_type == "application/json":
            json.loads(raw)
        elif media_type == "application/xml":
            require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
                    "XML entities prohibited")
            ET.fromstring(raw)
        else:
            require(raw.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid PNG signature")
        fingerprint = digest(raw)
        evidence_id = digest(canonical({"workspace": workspace, "platform": scope["platform"],
                                       "conditions": observed, "media_type": media_type,
                                       "sha256": fingerprint}).encode())
        with self.transaction():
            old = self.db.execute("SELECT id FROM evidence WHERE event_id=?", (event_id,)).fetchone()
            require(old is None or old["id"] == evidence_id,
                    "Event replay changed content or conditions")
            blob = self.blobs / fingerprint
            if blob.exists():
                require(file_digest(blob) == fingerprint, "Existing evidence blob is corrupted")
            else:
                try:
                    with blob.open("xb") as stream:
                        stream.write(raw)
                except FileExistsError:
                    require(file_digest(blob) == fingerprint, "Concurrent evidence corruption")
            self.db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?,?)",
                            (evidence_id, event_id, workspace, scope["platform"], canonical(observed),
                             media_type, fingerprint, now()))
        return {"evidence_id": evidence_id, "sha256": fingerprint, "event_id": event_id}

    def evidence(self, evidence_id: str) -> tuple:
        row = self.db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        require(row is not None, "Evidence does not exist")
        require(SHA256.fullmatch(row["sha256"]) is not None, "Invalid evidence fingerprint")
        path = self.blobs / row["sha256"]
        require(path.is_file(), "Evidence blob missing")
        require(path.stat().st_size <= MAX_EVIDENCE_BYTES, "Evidence blob oversized")
        raw = path.read_bytes()
        require(digest(raw) == row["sha256"], "Evidence fingerprint mismatch")
        return row, raw

    def validate_spec(self, spec: Dict[str, Any]) -> None:
        object_fields(spec, {"id", "workspace", "task_type", "statement", "conditions",
                             "anchors", "checks", "dependencies"}, {"provenance"})
        for field in ("id", "workspace", "task_type"):
            identifier(spec[field], field)
        self.workspace(spec["workspace"])
        require(isinstance(spec["statement"], str) and 0 < len(spec["statement"]) <= 2000,
                "Statement must be nonempty and <=2000 characters")
        conditions(spec["conditions"])
        require(isinstance(spec["anchors"], list) and len(spec["anchors"]) <= 32,
                "Anchors must be a bounded list")
        for anchor in spec["anchors"]:
            object_fields(anchor, {"path", "sha256"})
            require(isinstance(anchor["sha256"], str)
                    and SHA256.fullmatch(anchor["sha256"]) is not None, "Invalid anchor SHA256")
        require(isinstance(spec["checks"], list) and 0 < len(spec["checks"]) <= 16,
                "A claim requires 1-16 local checks")
        require(isinstance(spec["dependencies"], list) and len(spec["dependencies"]) <= 32,
                "Dependencies must be a bounded list")
        for dependency in spec["dependencies"]:
            identifier(dependency, "Dependency")
        require(spec["id"] not in spec["dependencies"], "Claim cannot depend on itself")
        require(len(set(spec["dependencies"])) == len(spec["dependencies"]),
                "Duplicate dependencies")

    def verify(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        self.validate_spec(spec)
        scope = self.workspace(spec["workspace"])
        for anchor in spec["anchors"]:
            path = workspace_file(Path(scope["root"]), anchor["path"])
            require(file_digest(path) == anchor["sha256"], "Source anchor changed: " + anchor["path"])
        results = []
        for check in spec["checks"]:
            require(isinstance(check, dict) and isinstance(check.get("evidence_id"), str),
                    "Each check requires an evidence ID")
            evidence, raw = self.evidence(check["evidence_id"])
            require(evidence["workspace"] == spec["workspace"]
                    and evidence["platform"] == scope["platform"], "Evidence belongs to another scope")
            require(canonical(spec["conditions"]) == evidence["conditions"],
                    "Claim must retain every recorded evidence condition; do not broaden its scope")
            passed = check_evidence(check, raw, evidence["media_type"])
            results.append({"kind": check["kind"], "evidence_id": evidence["id"], "passed": passed})
        return {"verdict": "PASS" if all(item["passed"] for item in results) else "FAIL",
                "checks": results, "meaning": "Declared predicates passed; prose is not mechanically proven"}

    def propose(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        self.validate_spec(spec)
        body = canonical(spec)
        candidate_id = digest(body.encode())
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?)",
                            (candidate_id, body, now()))
        return {"candidate_id": candidate_id, "status": "CANDIDATE"}

    def candidate(self, candidate_id: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT body FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        require(row is not None, "Candidate does not exist")
        require(digest(row["body"].encode()) == candidate_id, "Candidate fingerprint mismatch")
        return json.loads(row["body"])

    def revision(self) -> Optional[str]:
        row = self.db.execute("SELECT value FROM state WHERE key='revision'").fetchone()
        return row["value"] if row else None

    def snapshot(self, revision: Optional[str] = None) -> Dict[str, Any]:
        revision = revision if revision is not None else self.revision()
        if revision is None:
            return {}
        row = self.db.execute("SELECT body FROM snapshots WHERE revision=?", (revision,)).fetchone()
        require(row is not None, "Knowledge revision missing")
        require(digest(row["body"].encode()) == revision, "Knowledge revision fingerprint mismatch")
        return json.loads(row["body"])

    def current_valid(self, claim: Dict[str, Any], snapshot: Dict[str, Any],
                      actual: Dict[str, Any], visiting: Optional[set] = None) -> None:
        visiting = set() if visiting is None else visiting
        claim_id = claim["spec"]["id"]
        require(claim_id not in visiting, "Dependency cycle")
        visiting.add(claim_id)
        try:
            stored = self.db.execute("SELECT version FROM claims WHERE id=?", (claim_id,)).fetchone()
            if stored and stored["version"] == claim["version"]:
                from .lifecycle import require_fresh
                require_fresh(self, claim["spec"]["workspace"], "claim", claim_id, actual=actual)
            require(claim.get("lifecycle") == "active",
                    "Claim is " + claim.get("lifecycle", "unknown"))
            require(match_conditions(claim["spec"]["conditions"], actual),
                    "Conditions changed or are missing")
            require(self.verify(claim["spec"])["verdict"] == "PASS", "Evidence check no longer passes")
            for dependency, version in claim["dependency_versions"].items():
                require(dependency in snapshot and snapshot[dependency]["version"] == version,
                        "Dependency changed or missing: " + dependency)
                self.current_valid(snapshot[dependency], snapshot, actual, visiting)
        finally:
            visiting.remove(claim_id)

    def accept(self, candidate_id: str, expected_version: int, expected_revision: Optional[str],
               approve: bool) -> Dict[str, Any]:
        require(approve, "Explicit local review approval is required")
        require(type(expected_version) is int and expected_version >= 0, "Invalid expected version")
        spec = self.candidate(candidate_id)
        with self.transaction():
            require(self.revision() == expected_revision, "Publication revision conflict; refresh first")
            snapshot = self.snapshot()
            old = snapshot.get(spec["id"])
            require((old["version"] if old else 0) == expected_version, "Claim version conflict")
            verification = self.verify(spec)
            require(verification["verdict"] == "PASS", "Local checks failed; candidate not accepted")
            versions = {}
            for dependency in spec["dependencies"]:
                require(dependency in snapshot, "Dependency not published: " + dependency)
                require(snapshot[dependency]["spec"]["workspace"] == spec["workspace"],
                        "Dependencies must stay within the same platform workspace")
                self.current_valid(snapshot[dependency], snapshot, spec["conditions"])
                versions[dependency] = snapshot[dependency]["version"]
            updated = {"spec": spec, "version": expected_version + 1,
                       "lifecycle": "active",
                       "dependency_versions": versions, "verification": verification}
            trial = dict(snapshot)
            trial[spec["id"]] = updated
            self.current_valid(updated, trial, spec["conditions"])
            if (old and old["lifecycle"] == "active" and old["spec"] == spec
                    and old["dependency_versions"] == versions):
                return {"status": "NOOP", "revision": self.revision(), "version": expected_version}
            self.db.execute("INSERT INTO claims VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                            "version=excluded.version,body=excluded.body,updated_at=excluded.updated_at",
                            (spec["id"], updated["version"], canonical(updated), now()))
            trial_body = canonical(trial)
            revision = digest(trial_body.encode())
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                            (revision, trial_body, now()))
            self.db.execute("INSERT INTO state VALUES ('revision',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (revision,))
        return {"status": "PUBLISHED", "revision": revision, "version": updated["version"]}

    def invalidate(self, claim_id: str, reason: str, lifecycle: str, expected_version: int,
                   expected_revision: str, approve: bool) -> Dict[str, Any]:
        require(approve, "Explicit approval required to invalidate knowledge")
        require(lifecycle in ("stale", "conflicted"), "Invalid invalidation lifecycle")
        require(isinstance(reason, str) and 0 < len(reason) <= 2000, "Provide a bounded reason")
        with self.transaction():
            require(self.revision() == expected_revision, "Publication revision conflict")
            snapshot = self.snapshot()
            require(claim_id in snapshot, "Claim not published")
            claim = snapshot[claim_id]
            require(claim["version"] == expected_version, "Claim version conflict")
            claim.update({"version": expected_version + 1, "lifecycle": lifecycle,
                          "invalidation_reason": reason})
            body = canonical(snapshot)
            revision = digest(body.encode())
            self.db.execute("UPDATE claims SET version=?,body=?,updated_at=? WHERE id=?",
                            (claim["version"], canonical(claim), now(), claim_id))
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                            (revision, body, now()))
            self.db.execute("UPDATE state SET value=? WHERE key='revision'", (revision,))
        return {"status": "PUBLISHED", "revision": revision, "version": claim["version"]}

    def validate_task(self, task: Dict[str, Any]) -> None:
        object_fields(task, {"workspace", "task_type", "conditions"})
        identifier(task["workspace"], "Workspace")
        identifier(task["task_type"], "Task type")
        self.workspace(task["workspace"])
        conditions(task["conditions"])

    def recall(self, task: Dict[str, Any], session: str) -> Dict[str, Any]:
        self.validate_task(task)
        identifier(session, "Session")
        with self.transaction():
            revision = self.revision()
            snapshot = self.snapshot(revision)
            matches, stale = [], []
            for claim_id, claim in sorted(snapshot.items()):
                spec = claim["spec"]
                if spec["workspace"] != task["workspace"] or spec["task_type"] != task["task_type"]:
                    continue
                try:
                    self.current_valid(claim, snapshot, task["conditions"])
                except (RuntimeErrorBase, OSError, ValueError, ET.ParseError) as exc:
                    stale.append({"id": claim_id, "reason": str(exc)})
                else:
                    matches.append(claim)
                    from .lifecycle import record
                    record(self, task["workspace"], "claim", claim_id, claim["version"],
                           "retrieved", session, "recall:" + str(revision))
            status = "BLOCKED" if stale else "LOCAL_READY" if matches else "NEEDS_INVESTIGATION"
            if status == "NEEDS_INVESTIGATION":
                question_id = digest(canonical(task).encode())
                self.db.execute("INSERT OR IGNORE INTO questions VALUES (?,?,?,?)",
                                (question_id, canonical(task), "pending", now()))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                            "workspace=excluded.workspace,revision=excluded.revision,"
                            "claim_ids=excluded.claim_ids,updated_at=excluded.updated_at",
                            (session, task["workspace"], revision,
                             canonical([claim["spec"]["id"] for claim in matches]), now()))
        return {"schema": "llr.recall.v1", "status": status, "revision": revision,
                "workspace": task["workspace"], "claims": matches, "blocked": stale,
                "model_calls": 0, "notice": "Only registered checks are verified; task completeness is not inferred"}

    def refresh(self, session: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
        require(row is not None, "Unknown session")
        with self.transaction():
            latest = self.revision()
            previous = self.snapshot(row["revision"]) if row["revision"] else {}
            current = self.snapshot(latest)
            ids = json.loads(row["claim_ids"])
            changed = [item for item in ids if previous.get(item) != current.get(item)]
            invalidated = []
            for item in ids:
                if item not in current:
                    invalidated.append({"id": item, "reason": "Claim removed"})
                    continue
                try:
                    self.current_valid(current[item], current, current[item]["spec"]["conditions"])
                except (RuntimeErrorBase, OSError, ValueError, ET.ParseError) as exc:
                    invalidated.append({"id": item, "reason": str(exc)})
        return {"previous_revision": row["revision"], "revision": latest, "changed": changed,
                "invalidated": invalidated, "requires_recall": bool(changed or invalidated)}

    def questions(self) -> List[Dict[str, Any]]:
        return [{"id": row["id"], "status": row["status"], "task": json.loads(row["task"])}
                for row in self.db.execute("SELECT * FROM questions ORDER BY created_at,id")]

    def learning_packet(self, task: Dict[str, Any], question: str) -> Dict[str, Any]:
        self.validate_task(task)
        require(isinstance(question, str) and 0 < len(question.encode()) <= 2000,
                "Question must be 1-2000 UTF-8 bytes")
        packet = {"schema": "llr.learning.v1", "task": task, "question": question,
                  "revision": self.revision(), "model_calls": 0,
                  "instructions": [
                      "Use a fresh session; do not include prior conversation history.",
                      "Inspect only evidence relevant to this question.",
                      "Return scoped candidates, evidence references and unresolved questions.",
                      "Do not publish knowledge or execute account-changing actions.",
                      "No automatic model requests or recurring learning are authorized."],
                  "candidate_fields": ["id", "workspace", "task_type", "statement", "conditions",
                                       "anchors", "checks", "dependencies"]}
        related = [claim for claim in self.snapshot().values()
                   if claim["spec"]["workspace"] == task["workspace"]
                   and claim["spec"]["task_type"] == task["task_type"]]
        packet["related"] = [
            {"id": claim["spec"]["id"], "statement": claim["spec"]["statement"],
             "conditions": claim["spec"]["conditions"], "lifecycle": claim["lifecycle"],
             "version": claim["version"], "evidence_ids":
             sorted({check["evidence_id"] for check in claim["spec"]["checks"]})}
            for claim in sorted(related, key=lambda item: item["spec"]["id"])[:4]]
        packet["omitted_count"] = max(0, len(related) - 4)
        require(len(canonical(packet).encode()) <= MAX_PACKET_BYTES,
                "Learning packet exceeds 12000 UTF-8 bytes")
        return packet

    @contextmanager
    def device_lock(self, device: str) -> Iterator[None]:
        owner = str(uuid.uuid4())
        with self.transaction():
            try:
                self.db.execute("INSERT INTO device_locks VALUES (?,?)", (device, owner))
            except sqlite3.IntegrityError as exc:
                raise RuntimeErrorBase("Device locked; do not run concurrent observations") from exc
        try:
            yield
        finally:
            with self.transaction():
                self.db.execute("DELETE FROM device_locks WHERE device=? AND owner=?", (device, owner))

    def unlock_device(self, device: str, approve: bool) -> Dict[str, Any]:
        require(approve, "Confirm the original process has stopped before unlocking")
        with self.transaction():
            count = self.db.execute("DELETE FROM device_locks WHERE device=?", (device,)).rowcount
        return {"device": device, "released": count == 1}

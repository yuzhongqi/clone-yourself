"""Freshness and observable use, never popularity-as-truth or automatic experiments."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .core import Store, RuntimeErrorBase, canonical, digest, identifier, now, object_fields, require

DEFAULTS = {
    "source": {"max_age_seconds": 30 * 86400, "review_after_seconds": 15 * 86400},
    "runtime": {"max_age_seconds": 7 * 86400, "review_after_seconds": 3 * 86400},
    "remote": {"max_age_seconds": 86400, "review_after_seconds": 12 * 3600},
}
OBSERVABLE = ("retrieved", "expanded", "selected", "executed", "verified_success", "failure", "rejected")


def parsed(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return timestamp.astimezone(timezone.utc) if timestamp.tzinfo else None


def reference(store: Store, workspace: str, kind: str, subject: str) -> Dict[str, Any]:
    require(kind in ("claim", "solution"), "Memory kind must be claim or solution")
    store.workspace(workspace)
    if kind == "claim":
        row = store.db.execute("SELECT body FROM claims WHERE id=?", (subject,)).fetchone()
        require(row is not None, "Published claim not found")
        item = json.loads(row["body"])
        require(item["spec"]["workspace"] == workspace, "Wrong memory workspace")
        times = []
        for check in item["spec"]["checks"]:
            evidence = store.db.execute("SELECT created_at FROM evidence WHERE id=?",
                                        (check["evidence_id"],)).fetchone()
            if evidence:
                times.append(evidence[0])
        baseline = min(times) if len(times) == len(item["spec"]["checks"]) else None
        recorded = item["spec"].get("provenance", {}).get("validated_at")
        if recorded is not None:
            captured = parsed(recorded)
            stored_time = parsed(baseline)
            baseline = (min(captured, stored_time).isoformat()
                        if captured and stored_time else None)
        return {"version": item["version"], "type": "source" if item["spec"].get(
                "provenance", {}).get("kind") == "code_inference" else "runtime",
                "lifecycle": item["lifecycle"], "baseline": baseline,
                "title": item["spec"].get("provenance", {}).get("title", item["spec"]["task_type"])}
    row = store.db.execute("SELECT * FROM solutions WHERE workspace=? AND id=?", (workspace, subject)).fetchone()
    require(row is not None, "Solution not found")
    spec = json.loads(row["body"])
    from .solutions import all_steps
    return {"version": row["version"], "type": "runtime" if any(
            step["op"] == "run_validator" for step in all_steps(spec)) else "source",
            "lifecycle": row["lifecycle"], "baseline": None, "title": spec["problem"]["goal"]}


def configure(store: Store, workspace: str, kind: str, subject: str,
              spec: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required to change freshness policy")
    reference(store, workspace, kind, subject)
    object_fields(spec, {"knowledge_type", "max_age_seconds", "review_after_seconds",
                         "archive_after_seconds", "required_dynamic"})
    require(spec["knowledge_type"] in DEFAULTS, "Unknown knowledge type")
    for name in ("max_age_seconds", "review_after_seconds", "archive_after_seconds"):
        require(type(spec[name]) is int and 1 <= spec[name] <= 3650 * 86400, "Invalid age limit: " + name)
    require(spec["review_after_seconds"] <= spec["max_age_seconds"], "Review cannot follow expiration")
    require(isinstance(spec["required_dynamic"], list) and len(spec["required_dynamic"]) <= 16,
            "Dynamic conditions must be a bounded list")
    for name in spec["required_dynamic"]:
        identifier(name, "Dynamic condition name")
    if kind == "claim":
        claim = json.loads(store.db.execute("SELECT body FROM claims WHERE id=?", (subject,)).fetchone()[0])
        declared = set(claim["spec"]["conditions"])
    else:
        solution = json.loads(store.db.execute("SELECT body FROM solutions WHERE workspace=? AND id=?",
                                               (workspace, subject)).fetchone()[0])
        declared = {item["name"] for item in solution["preconditions"] if item["kind"] == "condition_equals"}
    require(set(spec["required_dynamic"]) <= declared,
            "Dynamic conditions must be bound in the original condition contract; revalidate an expanded scope")
    with store.transaction():
        store.db.execute("INSERT INTO memory_policies VALUES (?,?,?,?,?) ON CONFLICT(workspace,kind,subject) "
                         "DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at",
                         (workspace, kind, subject, canonical(spec), now()))
    return {"status": "POLICY_UPDATED", "policy": spec,
            "notice": "Retrieval never renews evidence. No App or model action was started."}


def record(store: Store, workspace: str, kind: str, subject: str, version: int,
           event: str, session: str, source: str, case_key: str = "", reason: str = "",
           occurred_at: Optional[str] = None) -> None:
    require(event in OBSERVABLE, "Unknown memory usage event")
    identifier(session, "Session")
    require(type(version) is int and version > 0, "Invalid memory version")
    require(len(source) <= 300 and len(reason) <= 2000, "Memory receipt too large")
    require(occurred_at is None or parsed(occurred_at) is not None, "Invalid memory event timestamp")
    # ID keys the observed action, not the timestamp: replay cannot inflate counts.
    event_id = digest(canonical([workspace, kind, subject, version, case_key, event, session, source]).encode())
    from .learning import privacy_key
    from .privacy import sanitize
    safe_reason = sanitize(reason, privacy_key(store))[0]
    transaction = nullcontext() if store.db.in_transaction else store.transaction()
    with transaction:
        store.db.execute("INSERT OR IGNORE INTO memory_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (event_id, workspace, kind, subject, version, case_key, event,
                          session, source, safe_reason, occurred_at or now()))


def usage(store: Store, workspace: str, kind: str, subject: str,
          version: int, case_key: Optional[str] = None) -> Dict[str, Any]:
    args = [workspace, kind, subject, version]
    condition = "workspace=? AND kind=? AND subject=? AND version=?"
    if case_key is not None:
        condition += " AND case_key=?"
        args.append(case_key)
    rows = store.db.execute("SELECT event,count(*) AS total,max(occurred_at) AS last FROM memory_events "
                            "WHERE " + condition + " GROUP BY event", args).fetchall()
    counts = {event: 0 for event in OBSERVABLE}
    latest = {}
    for row in rows:
        counts[row["event"]] = row["total"]
        latest[row["event"]] = row["last"]
    attempts = counts["verified_success"] + counts["failure"]
    return {"counts": counts, "last_retrieved_at": latest.get("retrieved"),
            "last_expanded_at": latest.get("expanded"), "last_executed_at": latest.get("executed"),
            "last_success_at": latest.get("verified_success"), "last_failure_at": latest.get("failure"),
            "success_rate": counts["verified_success"] / attempts if attempts else None,
            "meaning": "Observed events only. Retrieval/injection is not understanding, adoption or success."}


def assess(store: Store, workspace: str, kind: str, subject: str,
           case_key: Optional[str] = None, actual: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ref = reference(store, workspace, kind, subject)
    row = store.db.execute("SELECT body FROM memory_policies WHERE workspace=? AND kind=? AND subject=?",
                           (workspace, kind, subject)).fetchone()
    policy = json.loads(row["body"]) if row else dict(DEFAULTS[ref["type"]],
        knowledge_type=ref["type"], archive_after_seconds=90 * 86400, required_dynamic=[])
    stats = usage(store, workspace, kind, subject, ref["version"], case_key)
    baseline = ref["baseline"]
    proof_errors = []
    if kind == "solution":
        query = ("SELECT r.body,c.parameters_sha FROM solution_cases c JOIN solution_runs r ON r.id=c.run_id "
                 "WHERE c.workspace=? AND c.solution=? AND c.version=?")
        args = [workspace, subject, ref["version"]]
        if case_key is not None:
            query += " AND c.parameters_sha=?"
            args.append(case_key)
        dates = []
        for result in store.db.execute(query, args):
            proof = json.loads(result[0]).get("evidence", {}).get("evidence_id")
            evidence = store.db.execute("SELECT created_at FROM evidence WHERE id=?", (proof,)).fetchone()
            try:
                store.evidence(proof)
            except (RuntimeErrorBase, OSError) as exc:
                proof_errors.append(str(exc))
            if evidence:
                first = parsed(evidence[0])
                latest = parsed(usage(store, workspace, kind, subject, ref["version"],
                                      result["parameters_sha"])["last_success_at"])
                if first:
                    dates.append(max(first, latest).isoformat() if latest else first.isoformat())
        # Aggregate overview is conservative: do not let a fresh case renew an old case.
        baseline = min(dates) if dates else None
    verified = parsed(baseline)
    success = parsed(stats["last_success_at"])
    if success and not (kind == "solution" and case_key is None) and (verified is None or success > verified):
        verified = success
    clock = parsed(now())
    age = (clock - verified).total_seconds() if verified and clock else None
    due = verified + timedelta(seconds=policy["max_age_seconds"]) if verified else None
    last_failure = parsed(stats["last_failure_at"])
    state = ("UNKNOWN" if age is None or age < -60 else
             "EXPIRED" if age >= policy["max_age_seconds"] else "FRESH")
    reasons = []
    # A new success renews only its own evidence; confirm the renewing run still exists.
    if kind == "solution":
        condition = "workspace=? AND kind='solution' AND subject=? AND version=? AND event='verified_success'"
        args = [workspace, subject, ref["version"]]
        if case_key is not None:
            condition += " AND case_key=?"
            args.append(case_key)
        rows = store.db.execute("SELECT source FROM memory_events WHERE " + condition
                                 + " ORDER BY occurred_at DESC LIMIT 1", args).fetchall()
        for event in rows:
            run = store.db.execute("SELECT body,state FROM solution_runs WHERE id=?", (event["source"],)).fetchone()
            try:
                require(run is not None and run["state"] == "LOCAL_SOLVED",
                        "Successful execution provenance missing")
                store.evidence(json.loads(run["body"]).get("evidence", {}).get("evidence_id"))
            except (RuntimeErrorBase, OSError) as exc:
                proof_errors.append(str(exc))
    if state == "UNKNOWN":
        reasons.append("Verification time missing or in the future; revalidation required")
    elif state == "EXPIRED":
        reasons.append("Maximum evidence age exceeded; retrieval does not renew verification")
    if ref["lifecycle"] not in ("active", "verified"):
        state = "BLOCKED"
        reasons.append("Knowledge lifecycle is " + ref["lifecycle"])
    if proof_errors:
        state = "BLOCKED"
        reasons.extend(proof_errors)
    if kind == "claim" and store.db.execute(
            "SELECT 1 FROM knowledge_relations r JOIN milestones m ON m.id=r.target "
            "WHERE m.claim_id=? AND r.relation IN ('contradicts','supersedes') LIMIT 1",
            (subject,)).fetchone():
        state = "BLOCKED"
        reasons.append("Conflicting or superseding observation requires reconciliation")
    if last_failure and state != "BLOCKED" and (verified is None or last_failure >= verified):
        state = "REVIEW_REQUIRED"
        reasons.append("A failed execution is newer than the last successful verification")
    if kind == "solution" and state != "BLOCKED" and store.db.execute(
            "SELECT 1 FROM solution_runs WHERE workspace=? AND solution=? AND version=? AND state='RUNNING' LIMIT 1",
            (workspace, subject, ref["version"])).fetchone():
        state = "REVIEW_REQUIRED"
        reasons.append("An execution is running or was interrupted; inspect outcome before reuse")
    missing = [name for name in policy["required_dynamic"] if actual is None or name not in actual]
    if missing and state == "FRESH":
        state = "CONDITIONS_UNKNOWN"
        reasons.append("Required current dynamic conditions missing: " + ", ".join(missing))
    review_due = age is not None and age >= policy["review_after_seconds"]
    if review_due:
        reasons.append("Scheduled evidence review is due")
    last_activity = parsed(stats["last_executed_at"]) or parsed(stats["last_expanded_at"]) or verified
    inactive = bool(last_activity and clock
                    and (clock - last_activity).total_seconds() >= policy["archive_after_seconds"])
    return {"status": state, "last_verified_at": verified.isoformat() if verified else None,
            "revalidate_by": due.isoformat() if due else None, "review_due": review_due,
            "archive_suggested": inactive, "policy": policy, "usage": stats, "reasons": reasons,
            "scope": "Current version" + ("/parameter case" if case_key is not None else "/overview")}


def require_fresh(store: Store, workspace: str, kind: str, subject: str,
                  case_key: Optional[str] = None, actual: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = assess(store, workspace, kind, subject, case_key, actual)
    require(result["status"] == "FRESH", "Memory " + result["status"] + ": "
            + "; ".join(result["reasons"]))
    return result


def maintenance(store: Store, workspace: str, limit: int = 50, cursor: int = 0) -> Dict[str, Any]:
    store.workspace(workspace)
    require(type(limit) is int and 1 <= limit <= 200 and type(cursor) is int and cursor >= 0,
            "Maintenance paging requires limit 1-200 and a nonnegative cursor")
    targets = [("claim", row["id"]) for row in store.db.execute("SELECT id FROM claims")]
    targets += [("solution", row["id"]) for row in store.db.execute(
        "SELECT id FROM solutions WHERE workspace=?", (workspace,))]
    items = []
    for kind, subject in targets:
        if kind == "claim":
            row = store.db.execute("SELECT body FROM claims WHERE id=?", (subject,)).fetchone()
            if json.loads(row[0])["spec"]["workspace"] != workspace:
                continue
        overview = assess(store, workspace, kind, subject)
        scopes = [("", overview)]
        if kind == "solution":
            version = reference(store, workspace, kind, subject)["version"]
            scopes = [(row[0], assess(store, workspace, kind, subject, row[0])) for row in store.db.execute(
                "SELECT parameters_sha FROM solution_cases WHERE workspace=? AND solution=? AND version=?",
                (workspace, subject, version))] or scopes
        for case, health in scopes:
            reasons = list(health["reasons"])
            integrity_failed = False
            # Detect present integrity/condition drift without launching a validator.
            try:
                if kind == "claim":
                    claim = store.snapshot()[subject]
                    store.current_valid(claim, store.snapshot(), claim["spec"]["conditions"])
                else:
                    from .solutions import get_solution, check_preconditions
                    solution = get_solution(store, workspace, subject)
                    if case:
                        run = store.db.execute("SELECT r.body FROM solution_cases c JOIN solution_runs r "
                            "ON r.id=c.run_id WHERE c.workspace=? AND c.solution=? AND c.version=? "
                            "AND c.parameters_sha=?", (workspace, subject, solution["version"], case)).fetchone()
                        params = json.loads(run[0])["parameters"]
                        checks = check_preconditions(store, solution, params, {})
                        if any(check["verdict"] == "FAIL" for check in checks):
                            integrity_failed = True
                            reasons.append("Current source, dependency or runner condition changed")
            except (RuntimeErrorBase, OSError, ValueError) as exc:
                integrity_failed = True
                reasons.append(str(exc))
            used = health["usage"]["counts"]["selected"] + health["usage"]["counts"]["executed"]
            if used >= 3 and health["review_due"]:
                reasons.append("Frequently used knowledge has aging evidence")
            if health["archive_suggested"]:
                reasons.append("Long inactive; consider archiving, not deleting evidence")
            if reasons:
                urgent = health["status"] != "FRESH" or integrity_failed
                items.append({"kind": kind, "id": subject, "case_key": case,
                              "title": reference(store, workspace, kind, subject)["title"],
                              "priority": 100 if urgent else 60 if used >= 3 else 30,
                              "reasons": list(dict.fromkeys(reasons)), "health": health})
    items.sort(key=lambda item: (-item["priority"], item["kind"], item["id"], item["case_key"]))
    page = items[cursor:cursor + limit]
    remaining = max(0, len(items) - cursor - len(page))
    return {"workspace": workspace, "items": page, "total": len(items),
            "omitted_count": remaining, "next_cursor": cursor + len(page) if remaining else None,
            "notice": "Recommendations only: no App action, deletion, model request or learning restart.",
            "model_calls": 0}

"""L0/L1/L2/L3 derived memory views with pinned revisions and bounded packets."""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .core import Store, RuntimeErrorBase, canonical, digest, identifier, now, object_fields, require
from .discovery import discover, infer_platform
from .learning import tokenize


def define_theme(store: Store, workspace: str, spec: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required to change a theme router")
    store.workspace(workspace)
    object_fields(spec, {"id", "title", "summary", "aliases", "members", "related", "gaps"})
    identifier(spec["id"], "Theme")
    for field in ("aliases", "members", "related", "gaps"):
        require(isinstance(spec[field], list) and len(spec[field]) <= 32, "Theme field exceeds limit")
    require(len(canonical(spec).encode()) <= 12_000, "Theme exceeds 12000 bytes")
    from .learning import privacy_key
    from .privacy import sanitize
    safe, redactions = sanitize(spec, privacy_key(store))
    require(not redactions, "Theme contains private literals; minimize before routing")
    with store.transaction():
        store.db.execute("INSERT INTO themes VALUES (?,?,?) ON CONFLICT(workspace,id) "
                         "DO UPDATE SET body=excluded.body", (workspace, spec["id"], canonical(safe)))
    return {"status": "THEME_REGISTERED", "theme": spec["id"]}


def knowledge_revision(store: Store) -> str:
    solutions = [list(row) for row in store.db.execute(
        "SELECT workspace,id,version,lifecycle,fingerprint FROM solutions ORDER BY workspace,id")]
    themes = [list(row) for row in store.db.execute("SELECT * FROM themes ORDER BY workspace,id")]
    legacy = [list(row) for row in store.db.execute(
        "SELECT id,workspace,source_sha256 FROM legacy_intake ORDER BY id")]
    milestones = [list(row) for row in store.db.execute(
        "SELECT id,state,input_sha FROM milestones ORDER BY id")]
    preferences = [list(row) for row in store.db.execute("SELECT * FROM preferences ORDER BY id")]
    cases = [list(row) for row in store.db.execute(
        "SELECT workspace,solution,version,parameters_sha FROM solution_cases "
        "ORDER BY workspace,solution,version,parameters_sha")]
    return digest(canonical([store.revision(), solutions, themes, legacy, milestones, preferences, cases]).encode())


def build_view(store: Store) -> Dict[str, Any]:
    with store.transaction():
        revision = knowledge_revision(store)
        existing = store.db.execute("SELECT body FROM context_views WHERE revision=?",
                                    (revision,)).fetchone()
        if existing:
            return json.loads(existing["body"])
        nodes = {}
        for claim_id, claim in store.snapshot().items():
            spec = claim["spec"]
            node_id = "claim:" + claim_id
            nodes[node_id] = {"id": node_id, "workspace": spec["workspace"], "kind": "claim",
                              "title": spec.get("provenance", {}).get("title", spec["task_type"]),
                              "summary": spec["statement"], "conditions": spec["conditions"],
                              "status": claim["lifecycle"], "detail": claim}
        for row in store.db.execute("SELECT * FROM solutions ORDER BY id"):
            spec = json.loads(row["body"])
            cases = [json.loads(case["body"]).get("parameters", {}) for case in store.db.execute(
                "SELECT r.body FROM solution_cases c JOIN solution_runs r ON r.id=c.run_id "
                "WHERE c.workspace=? AND c.solution=? AND c.version=?",
                (row["workspace"], row["id"], row["version"]))]
            node_id = "solution:" + row["workspace"] + ":" + row["id"]
            nodes[node_id] = {
                "id": node_id, "workspace": row["workspace"], "kind": "solution",
                "title": spec["problem"]["goal"], "summary": spec["problem"]["intent"],
                "conditions": spec["preconditions"], "verified_cases": cases, "status": row["lifecycle"],
                "detail": {"spec": spec, "version": row["version"], "lifecycle": row["lifecycle"],
                           "fingerprint": row["fingerprint"]}}
        for row in store.db.execute("SELECT * FROM legacy_intake ORDER BY id"):
            record = json.loads(row["body"])
            source = record["source_node"]
            node_id = "legacy:" + row["id"]
            nodes[node_id] = {"id": node_id, "workspace": row["workspace"], "kind": "legacy",
                              "title": source.get("title", source["id"]),
                              "summary": source.get("summary", ""),
                              "conditions": {"freshness": "unknown"}, "status": "UNVERIFIED_LEGACY",
                              "detail": record}
        for row in store.db.execute("SELECT m.*,t.workspace FROM milestones m JOIN task_runs t "
                                   "ON t.id=m.task_run WHERE m.state!='PUBLISHED'"):
            event = json.loads(row["body"])
            node_id = "milestone:" + row["id"]
            preview = canonical(event["observation"])
            nodes[node_id] = {"id": node_id, "workspace": row["workspace"], "kind": "candidate",
                              "title": event["title"], "summary": preview[:2000],
                              "summary_truncated": len(preview) > 2000,
                              "conditions": event["conditions"], "status": row["state"], "detail": event}
        themes = {}
        for row in store.db.execute("SELECT * FROM themes"):
            themes[row["workspace"] + ":" + row["id"]] = dict(
                json.loads(row["body"]), workspace=row["workspace"])
        for workspace in [row["id"] for row in store.db.execute("SELECT id FROM workspaces")]:
            for kind, title in (("claim", "运行观察"), ("solution", "解决流程"),
                                ("legacy", "历史线索"), ("candidate", "待验证记录")):
                members = [node_id for node_id, node in nodes.items()
                           if node["workspace"] == workspace and node["kind"] == kind]
                themes.setdefault(workspace + ":" + kind, {
                    "id": kind, "workspace": workspace, "title": title,
                    "summary": "Derived entry point; membership is not proof of applicability.",
                    "aliases": [], "members": members, "related": [], "gaps": []})
        body = {"revision": revision, "claim_revision": store.revision(),
                "nodes": nodes, "themes": themes,
                "preferences": [dict(row, body=json.loads(row["body"]))
                                for row in store.db.execute("SELECT * FROM preferences")]}
        store.db.execute("INSERT INTO context_views VALUES (?,?,?)",
                         (revision, canonical(body), now()))
    return body


def view_at(store: Store, revision: Optional[str]) -> Dict[str, Any]:
    if revision is None:
        return build_view(store)
    row = store.db.execute("SELECT body FROM context_views WHERE revision=?", (revision,)).fetchone()
    require(row is not None, "Context revision unavailable or revoked")
    body = json.loads(row["body"])
    require(body["revision"] == revision, "Context revision mismatch")
    return body


def packet(store: Store, cwd: Path, query: str, session: str, level: int = 2,
           node: Optional[str] = None, revision: Optional[str] = None,
           cursor: int = 0, budget: int = 6000) -> Dict[str, Any]:
    identifier(session, "Session")
    require(level in (0, 1, 2, 3), "Invalid memory layer")
    require(type(cursor) is int and cursor >= 0, "Invalid cursor")
    require(type(budget) is int and 1000 <= budget <= 12_000, "Budget must be 1000-12000 UTF-8 bytes")
    require(isinstance(query, str) and len(query.encode()) <= 4000, "Query too large")
    found = discover(store, cwd, session, infer_platform(query))
    if found["status"] != "DISCOVERED":
        return found
    workspace = found["workspace"]
    view = view_at(store, revision)
    workspace_nodes = {key: value for key, value in view["nodes"].items()
                       if value["workspace"] == workspace}
    themes = [value for value in view["themes"].values() if value["workspace"] == workspace]
    terms = tokenize(query)
    candidates = []
    if level == 0:
        candidates = [{"id": theme["id"], "title": theme["title"],
                       "count": sum(item in workspace_nodes for item in theme["members"])}
                      for theme in themes]
        candidates.extend({"id": "preference:" + item["id"], "kind": "preference",
                           "status": item["state"], "summary": item["body"]["text"],
                           "notice": item["body"]["notice"]}
                          for item in view["preferences"]
                          if item["workspace"] in (None, workspace))
    elif level == 1:
        selected = [theme for theme in themes if (theme["id"] == node if node else
                    terms & tokenize(theme["title"] + " " + " ".join(theme["aliases"])))]
        for theme in selected:
            candidates.append({"id": theme["id"], "title": theme["title"],
                               "summary": theme["summary"], "gaps": theme["gaps"],
                               "status": "ROUTER_ONLY_NOT_A_VERIFIED_CLAIM",
                               "related": theme["related"],
                               "members": [item for item in theme["members"] if item in workspace_nodes][:16],
                               "members_omitted": max(0, sum(item in workspace_nodes
                                                           for item in theme["members"]) - 16),
                               "unavailable_members": [item for item in theme["members"]
                                                       if item not in workspace_nodes]})
    elif level == 2:
        selected_ids = set()
        for theme in themes:
            if theme["id"] == node or terms & tokenize(" ".join(theme["aliases"])):
                selected_ids.update(theme["members"])
                for relative in theme["related"][:8]:
                    neighbor = view["themes"].get(workspace + ":" + relative)
                    if neighbor:
                        selected_ids.update(neighbor["members"])
        ranked = []
        for node_id, item in workspace_nodes.items():
            score = len(terms & tokenize(item["title"] + " " + item["summary"]))
            if node_id in selected_ids:
                score += 2
            if score:
                card = {key: value for key, value in item.items() if key != "detail"}
                card["reuse"] = ("CONDITIONS_UNKNOWN" if item["kind"] == "claim"
                                 else "PRECONDITIONS_REQUIRED" if item["kind"] == "solution"
                                 else item["status"])
                if item["kind"] == "claim":
                    try:
                        store.current_valid(item["detail"], store.snapshot(),
                                            item["detail"]["spec"]["conditions"])
                    except (RuntimeErrorBase, OSError, ValueError) as exc:
                        card["reuse"], card["reason"] = "BLOCKED", str(exc)
                ranked.append((score, node_id, card))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        candidates = [item[2] for item in ranked]
    else:
        require(node in workspace_nodes, "Unknown or wrong-workspace detail node")
        item = workspace_nodes[node]
        # Full conditions and scope remain together. Only evidence references are returned here.
        detail = item["detail"]
        if item["kind"] == "solution":
            parts = [{"section": name, "value": value} for name, value in detail["spec"].items()]
        elif item["kind"] == "claim":
            parts = [{"section": name, "value": value} for name, value in detail["spec"].items()]
        else:
            parts = [{"section": name, "value": value} for name, value in detail.items()]
        candidates = [{"id": node, "title": item["title"], "status": item["status"],
                       "conditions": item["conditions"], "detail": part} for part in parts]
    result = {"schema": "llr.context.v1", "workspace": workspace,
              "execution_pid": os.getpid(),
              "revision": view["revision"], "current_revision": knowledge_revision(store),
              "layer": level, "items": [], "omitted_count": 0, "next_cursor": None,
              "budget": {"utf8_bytes": budget, "unit": "bytes, not tokens"},
              "notice": "Pinned historical view. Conditions/status are not current runtime attestation. "
                        "Execution requires the complete contract, not this summary."}
    for candidate in candidates[cursor:]:
        result["items"].append(candidate)
        if len(canonical(result).encode()) + 100 > budget:
            result["items"].pop()
            break
    remaining = max(0, len(candidates) - cursor - len(result["items"]))
    result["omitted_count"] = remaining
    result["next_cursor"] = cursor + len(result["items"]) if remaining and result["items"] else None
    result["status"] = ("ITEM_TOO_LARGE" if remaining and not result["items"] else
                        "PARTIAL" if remaining else "OK")
    with store.transaction():
        for item in result["items"]:
            store.db.execute("INSERT OR IGNORE INTO context_reads VALUES (?,?,?,?)",
                             (session, workspace, view["revision"], item["id"]))
    require(len(canonical(result).encode()) <= budget, "Context envelope exceeds declared budget")
    return result


def refresh_context(store: Store, session: str) -> Dict[str, Any]:
    rows = store.db.execute("SELECT * FROM context_reads WHERE session=?", (session,)).fetchall()
    current = build_view(store)
    changed = set()
    invalidated = []
    for row in rows:
        try:
            previous = view_at(store, row["revision"])
        except RuntimeErrorBase:
            changed.add(row["node"])
            continue
        old_node = previous["nodes"].get(row["node"]) or previous["themes"].get(
            row["workspace"] + ":" + row["node"])
        new_node = current["nodes"].get(row["node"]) or current["themes"].get(
            row["workspace"] + ":" + row["node"])
        if old_node != new_node:
            changed.add(row["node"])
        if new_node and new_node.get("kind") == "solution":
            from .solutions import get_solution, check_preconditions
            try:
                record = get_solution(store, row["workspace"], new_node["detail"]["spec"]["id"])
                for parameters in new_node.get("verified_cases", []):
                    checks = check_preconditions(store, record, parameters, {})
                    if any(check["verdict"] == "FAIL" for check in checks):
                        invalidated.append({"id": row["node"], "reason": "Live precondition drift"})
                        break
            except (RuntimeErrorBase, OSError, ValueError) as exc:
                invalidated.append({"id": row["node"], "reason": str(exc)})
    return {"revision": current["revision"], "changed": sorted(changed),
            "invalidated": invalidated, "requires_reload": bool(changed or invalidated), "model_calls": 0}

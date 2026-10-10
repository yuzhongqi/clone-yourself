"""Reviewed legacy intake and a static, offline human/agent knowledge view."""

import base64
import html
import json
from pathlib import Path
from typing import Any, Dict, List
import xml.etree.ElementTree as ET

from .core import (MAX_PACKET_BYTES, RuntimeErrorBase, Store, canonical, digest,
                   identifier, now, require)


def import_graph(store: Store, workspace: str, source: Path, node_ids: List[str],
                 approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required to import private legacy knowledge")
    store.workspace(workspace)
    require(1 <= len(node_ids) <= 16 and len(set(node_ids)) == len(node_ids),
            "Select 1-16 distinct graph node IDs")
    require(source.stat().st_size <= 8 * 1024 * 1024, "Legacy graph exceeds 8 MiB")
    raw = source.read_bytes()
    graph = json.loads(raw)
    require(isinstance(graph, dict) and isinstance(graph.get("nodes"), list),
            "Legacy graph must have a nodes list")
    nodes = {}
    for node in graph["nodes"]:
        require(isinstance(node, dict) and isinstance(node.get("id"), str),
                "Invalid graph node")
        require(node["id"] not in nodes, "Duplicate legacy graph node ID")
        nodes[node["id"]] = node
    require(all(item in nodes for item in node_ids), "Requested graph node missing")
    fingerprint = digest(raw)
    prepared = []
    for node_id in node_ids:
        identifier(node_id, "Legacy node ID")
        selected = nodes[node_id]
        require(len(canonical(selected).encode()) <= MAX_PACKET_BYTES,
                "Legacy node exceeds 12000 UTF-8 bytes; do not silently truncate conditions")
        record = {"source_node": selected, "source_file": str(source.resolve()),
                  "source_sha256": fingerprint, "graph_scope": graph.get("scope"),
                  "source_head": graph.get("source_head"),
                  "source_working_changes": graph.get("source_working_changes"),
                  "source_schema": graph.get("schema"),
                  "status": "UNVERIFIED_LEGACY",
                  "notice": "Original status preserved as history; import grants no verification."}
        intake_id = digest(canonical({"workspace": workspace, "node_id": node_id,
                                     "source_sha256": fingerprint}).encode())
        prepared.append((intake_id, record))
    with store.transaction():
        for intake_id, record in prepared:
            store.db.execute("INSERT OR IGNORE INTO legacy_intake VALUES (?,?,?,?,?)",
                             (intake_id, workspace, fingerprint, canonical(record),
                              now()))
    return {"status": "IMPORTED_UNVERIFIED", "ids": [item[0] for item in prepared],
            "published_revision": store.revision(), "source_sha256": fingerprint}


def catalog(store: Store, workspace: str) -> Dict[str, Any]:
    store.workspace(workspace)
    with store.transaction():
        revision = store.revision()
        snapshot = store.snapshot(revision)
        claims = []
        for claim in snapshot.values():
            if claim["spec"]["workspace"] != workspace:
                continue
            card = json.loads(canonical(claim))
            try:
                store.current_valid(claim, snapshot, claim["spec"]["conditions"])
            except (RuntimeErrorBase, OSError, ValueError, ET.ParseError) as exc:
                card["local_integrity"] = {"status": "BLOCKED", "reason": str(exc)}
            else:
                card["local_integrity"] = {
                    "status": "PASS",
                    "notice": "Checks pass under recorded conditions; current environment not attested."}
            claims.append(card)
        legacy = []
        for row in store.db.execute("SELECT id,body FROM legacy_intake WHERE workspace=? ORDER BY id",
                                   (workspace,)):
            record = json.loads(row["body"])
            legacy.append(dict(record, intake_id=row["id"]))
        candidates = []
        for row in store.db.execute("SELECT id,body FROM candidates ORDER BY id"):
            spec = json.loads(row["body"])
            if spec["workspace"] == workspace:
                if spec["id"] in snapshot and snapshot[spec["id"]]["spec"] == spec:
                    continue
                candidates.append({"candidate_id": row["id"], "spec": spec,
                                   "status": "CANDIDATE_NOT_PUBLISHED"})
        questions = [item for item in store.questions() if item["task"]["workspace"] == workspace]
    return {"schema": "llr.catalog.v1", "workspace": workspace, "revision": revision,
            "claims": sorted(claims, key=lambda item: item["spec"]["id"]),
            "legacy": legacy, "candidates": candidates, "questions": questions}


def export_portal(store: Store, workspace: str, output: Path) -> Dict[str, Any]:
    output = output.expanduser().resolve()
    require(not output.exists(), "Portal output already exists; choose a new revision directory")
    data = catalog(store, workspace)
    sections = []
    for heading, items in (("Published claims", data["claims"]),
                           ("Unverified legacy intake", data["legacy"]),
                           ("Unpublished candidates", data["candidates"]),
                           ("Investigation queue", data["questions"])):
        cards = []
        for item in items:
            title = (item.get("spec", {}).get("id") or item.get("source_node", {}).get("title")
                     or item.get("id") or "Knowledge item")
            cards.append("<article><h3>" + html.escape(str(title)) + "</h3><pre>"
                         + html.escape(json.dumps(item, ensure_ascii=False, indent=2))
                         + "</pre></article>")
        sections.append("<section><h2>" + heading + "</h2>"
                        + ("".join(cards) if cards else "<p>No entries.</p>") + "</section>")
    evidence_sections = []
    total = 0
    evidence_ids = sorted({check["evidence_id"] for claim in data["claims"]
                           for check in claim["spec"]["checks"]})
    for evidence_id in evidence_ids:
        try:
            row, raw = store.evidence(evidence_id)
        except (RuntimeErrorBase, OSError) as exc:
            evidence_sections.append("<article><h3>" + html.escape(evidence_id)
                                     + "</h3><p>BLOCKED: " + html.escape(str(exc)) + "</p></article>")
            continue
        total += len(raw)
        require(total <= 32 * 1024 * 1024, "Portal evidence exceeds 32 MiB")
        if row["media_type"] == "image/png":
            body = '<img alt="Recorded evidence" src="data:image/png;base64,' \
                   + base64.b64encode(raw).decode("ascii") + '">'
        else:
            body = "<pre>" + html.escape(raw.decode("utf-8")) + "</pre>"
        evidence_sections.append("<article><h3>" + html.escape(evidence_id) + "</h3>" + body
                                 + "</article>")
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local Learning Knowledge</title><style>
body{font:16px system-ui;margin:2rem auto;max-width:1100px;padding:0 1rem}
article{border:1px solid #aaa;border-radius:8px;padding:1rem;margin:1rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%}
</style></head><body><h1>Local Learning Knowledge</h1>"""
    page += "<p>Workspace: " + html.escape(workspace) + "</p><p>Revision: " \
            + html.escape(str(data["revision"])) + "</p>"
    page += ("<p>Private offline snapshot, not a live dashboard. PASS checks recorded conditions "
             "only. Legacy notes are not verified. Re-export after publication or drift.</p>"
             '<p><a href="catalog.json">Agent-readable JSON catalog</a></p>')
    page += "".join(sections) + "<section><h2>Evidence</h2>" + "".join(evidence_sections) \
            + "</section></body></html>"
    output.mkdir(parents=True, mode=0o700)
    (output / "catalog.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (output / "index.html").write_text(page, encoding="utf-8")
    return {"status": "EXPORTED", "html": str(output / "index.html"),
            "json": str(output / "catalog.json"), "revision": data["revision"],
            "claim_count": len(data["claims"]), "legacy_count": len(data["legacy"])}

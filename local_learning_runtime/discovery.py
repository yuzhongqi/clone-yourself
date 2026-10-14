"""Checkout-boundary discovery, independent of chat history or caller cwd."""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .core import Store, canonical, identifier, now, require


def infer_platform(query: str) -> Optional[str]:
    import re
    platforms = set()
    if re.search(r"\bandroid\b|安卓", query, re.I):
        platforms.add("android")
    if re.search(r"\bios\b|苹果", query, re.I):
        platforms.add("ios")
    return next(iter(platforms)) if len(platforms) == 1 else None


def register_alias(store: Store, workspace: str, root: Path, require_platform: bool,
                   approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval needed for an additional discovery root")
    primary = Path(store.workspace(workspace)["root"]).resolve()
    root = root.expanduser().resolve()
    require(root.is_dir(), "Alias root must exist")
    try:
        primary.relative_to(root)
    except ValueError as exc:
        raise ValueError("Alias must contain the registered checkout; other checkouts require separate registration") from exc
    require(root != Path(root.anchor), "Filesystem root cannot be a knowledge alias")
    with store.transaction():
        store.db.execute("INSERT INTO workspace_aliases VALUES (?,?,?) ON CONFLICT(workspace,root) "
                         "DO UPDATE SET requires_platform=excluded.requires_platform",
                         (workspace, str(root), int(require_platform)))
    return {"workspace": workspace, "discovery_root": str(root),
            "source_anchor_root": str(primary), "requires_platform": require_platform}


def discover(store: Store, cwd: Path, session: str, platform: Optional[str] = None) -> Dict[str, Any]:
    identifier(session, "Session")
    require(platform in (None, "android", "ios", "generic"), "Unknown platform")
    cwd = cwd.expanduser().resolve()
    require(cwd.is_dir(), "Current working directory must exist")
    matches = []
    roots = [dict(row, requires_platform=0) for row in store.db.execute(
        "SELECT * FROM workspaces ORDER BY id")]
    roots.extend(dict(row) for row in store.db.execute(
        "SELECT w.id,a.root,w.platform,a.requires_platform FROM workspace_aliases a "
        "JOIN workspaces w ON w.id=a.workspace"))
    for row in roots:
        if platform is not None and row["platform"] != platform:
            continue
        root = Path(row["root"]).resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            continue
        matches.append(dict(row))
    if matches:
        specificity = max(len(Path(item["root"]).parts) for item in matches)
        matches = [item for item in matches if len(Path(item["root"]).parts) == specificity]
        matches = list({(item["id"], item["root"]): item for item in matches}.values())
    status = "DISCOVERED" if len(matches) == 1 else "AMBIGUOUS" if matches else "UNKNOWN_WORKSPACE"
    if any(item["requires_platform"] for item in matches) and platform is None:
        status = "AMBIGUOUS"
    result: Dict[str, Any] = {
        "schema": "llr.discovery.v1", "status": status, "cwd": str(cwd),
        "session": session, "state_home": str(store.home), "model_calls": 0,
        "notice": "Discovery locates knowledge; it does not attest current app conditions."}
    with store.transaction():
        revision = store.revision()
        result["revision"] = revision
        if status == "DISCOVERED":
            selected = matches[0]
            workspace = selected["id"]
            claims = [item for item in store.snapshot(revision).values()
                      if item["spec"]["workspace"] == workspace]
            legacy = []
            for row in store.db.execute("SELECT body FROM legacy_intake WHERE workspace=? ORDER BY id",
                                       (workspace,)):
                record = json.loads(row["body"])
                node = record["source_node"]
                legacy.append({"id": node["id"], "title": node.get("title", ""),
                               "status": "UNVERIFIED_LEGACY",
                               "original_status": node.get("status")})
            result.update({
                "workspace": workspace, "registered_root": selected["root"],
                "platform": selected["platform"],
                "knowledge_counts": {"published": len(claims), "legacy": len(legacy)},
                "task_types": sorted({item["spec"]["task_type"] for item in claims}),
                "claim_ids": sorted(item["spec"]["id"] for item in claims)[:16],
                "claims_omitted": max(0, len(claims) - 16),
                "legacy_outline": legacy[:16], "legacy_omitted": max(0, len(legacy) - 16),
                "next": "Use catalog for scoped knowledge; recall with the actual task and conditions "
                        "before reuse. Legacy notes are clues, not verified claims."})
        else:
            result["workspace"] = None
            result["candidates"] = [{"workspace": item["id"], "platform": item["platform"]}
                                    for item in matches]
            result["next"] = ("Specify platform or register an unambiguous project root."
                              if matches else "Register this checkout explicitly; do not guess another repo.")
        store.db.execute("INSERT INTO discoveries VALUES (?,?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET "
                         "cwd=excluded.cwd,workspace=excluded.workspace,revision=excluded.revision,"
                         "manifest=excluded.manifest,created_at=excluded.created_at",
                         (session, str(cwd), result["workspace"], revision, canonical(result), now()))
    return result

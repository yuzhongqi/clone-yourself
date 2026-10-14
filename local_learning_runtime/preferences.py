"""Explicit preferences and unconfirmed habit hypotheses, never authorization."""

from typing import Any, Dict, Optional

from .core import Store, canonical, identifier, require
from .learning import privacy_key
from .privacy import sanitize


def save_preference(store: Store, preference: str, text: str, kind: str,
                    expected_version: int, approve: bool, workspace: Optional[str] = None) -> Dict[str, Any]:
    identifier(preference, "Preference")
    require(kind in ("explicit", "inferred"), "Preference kind must be explicit/inferred")
    require(isinstance(text, str) and 0 < len(text) <= 2000, "Preference text must be bounded")
    require(kind != "explicit" or approve, "Explicit preference requires user confirmation")
    if workspace:
        store.workspace(workspace)
    safe, _ = sanitize(text, privacy_key(store))
    with store.transaction():
        old = store.db.execute("SELECT version FROM preferences WHERE id=?", (preference,)).fetchone()
        require((old["version"] if old else 0) == expected_version, "Preference version conflict")
        version = expected_version + 1
        state = "confirmed" if kind == "explicit" else "candidate"
        body = {"text": safe, "notice": "Preference is not an action permission or verified project fact"}
        store.db.execute("INSERT INTO preferences VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "workspace=excluded.workspace,version=excluded.version,kind=excluded.kind,"
                         "state=excluded.state,body=excluded.body",
                         (preference, workspace, version, kind, state, canonical(body)))
    return {"id": preference, "version": version, "state": state, "grants_authorization": False}

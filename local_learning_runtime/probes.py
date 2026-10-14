"""Bounded runtime producers for explicitly registered command validators."""

from typing import Any, Dict
import xml.etree.ElementTree as ET

from .core import require
from .devices import android_identity, android_launch, android_ui


def android_node(serial: str, package: str, resource_id: str, adb: str, approve: bool) -> Dict[str, Any]:
    import re
    require(approve, "Explicit probe approval or preapproved runner registration required")
    require(re.fullmatch(r"[A-Za-z0-9_.:-]+", serial) is not None, "Invalid serial")
    require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package)
            is not None, "Invalid package")
    require(resource_id.startswith(package + ":id/"), "Node must belong to the approved app")
    prefix = [adb, "-s", serial]
    before = android_identity(prefix, package)
    android_launch(prefix, package)
    raw = android_ui(prefix)
    nodes = [node for node in ET.fromstring(raw).iter("node")
             if node.get("package") == package and node.get("resource-id") == resource_id]
    after = android_identity(prefix, package)
    require(before == after, "Installed artifact changed during probe")
    return {"conditions": dict(before, account_state="not-attested",
                               source_mapping="not-attested", probe="unique-enabled-ui-node"),
            "resource_id": resource_id, "matches": len(nodes),
            "enabled": len(nodes) == 1 and nodes[0].get("enabled") == "true",
            "actions": ["fingerprint_installed_artifact", "launch_approved_app", "read_ui_node"],
            "privacy": "Only selected node identity and enabled state; no text or screenshot persisted."}

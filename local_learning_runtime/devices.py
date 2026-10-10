"""Explicit capture adapters; no navigation, login, or arbitrary shell actions."""

import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from .core import (MAX_EVIDENCE_BYTES, RuntimeErrorBase, Store, canonical, digest,
                   conditions, file_digest, object_fields, require)


def run(args: List[str], timeout: int = 30) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    require(result.returncode == 0,
            "Device command failed: " + args[0] + " (exit " + str(result.returncode) + ")")
    return result.stdout


def android_identity(prefix: List[str], package: str) -> Dict[str, str]:
    require(run(prefix + ["get-state"]).strip() == b"device", "Android device is unavailable")
    os_version = run(prefix + ["shell", "getprop", "ro.build.version.sdk"]).decode().strip()
    paths = run(prefix + ["shell", "pm", "path", package]).decode().splitlines()
    require(bool(paths), "Package is not installed")
    artifacts = []
    for line in paths:
        require(line.startswith("package:"), "Unexpected package path response")
        path = line[len("package:"):]
        require(re.fullmatch(r"/[A-Za-z0-9_./+=@~-]+\.apk", path) is not None,
                "Unsupported package path")
        parts = run(prefix + ["shell", "sha256sum", path]).decode().split()
        require(bool(parts) and re.fullmatch(r"[0-9a-f]{64}", parts[0]) is not None,
                "Cannot fingerprint installed APK")
        artifacts.append(parts[0])
    return {"artifact_sha256": digest(canonical(sorted(artifacts)).encode()),
            "os_api": os_version, "package": package}


def android_ui(prefix: List[str]) -> bytes:
    remote = "/data/local/tmp/llr-" + str(uuid.uuid4()) + ".xml"
    try:
        run(prefix + ["shell", "uiautomator", "dump", remote])
        raw = run(prefix + ["exec-out", "cat", remote])
        require(0 < len(raw) <= MAX_EVIDENCE_BYTES, "UI dump is empty or oversized")
        require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
                "XML entities prohibited")
        ET.fromstring(raw)
        return raw
    finally:
        run(prefix + ["shell", "rm", "-f", remote])


def android_launch(prefix: List[str], package: str) -> None:
    resolved = run(prefix + ["shell", "cmd", "package", "resolve-activity", "--brief",
                             "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", package]).decode().splitlines()
    components = [line.strip() for line in resolved
                  if re.fullmatch(re.escape(package) + r"/[A-Za-z0-9_.$]+", line.strip())]
    require(len(components) == 1, "Cannot uniquely resolve requested app launcher")
    response = run(prefix + ["shell", "am", "start", "-W",
                             "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER",
                             "-n", components[0]]).decode()
    require("Status: ok" in response and "Error:" not in response,
            "Activity manager did not confirm successful app launch")


def android_scope(store: Store, workspace: str, serial: str, package: str,
                  context: Dict[str, Any], approve: bool) -> List[str]:
    require(approve, "Device operations require explicit approval")
    require(store.workspace(workspace)["platform"] == "android", "Not an Android workspace")
    require(isinstance(serial, str) and re.fullmatch(r"[A-Za-z0-9_.:-]+", serial) is not None,
            "Invalid device serial")
    require(isinstance(package, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package)
            is not None, "Invalid Android package")
    require(isinstance(context, dict) and "artifact_sha256" not in context,
            "Do not supply an unverified artifact fingerprint")
    conditions(dict(context, artifact_sha256="0" * 64))
    return ["adb", "-s", serial]


def android_capture(store: Store, workspace: str, serial: str, package: str,
                    context: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    prefix = android_scope(store, workspace, serial, package, context, approve)
    with store.device_lock("android:" + serial):
        observed = dict(context)
        identity = android_identity(prefix, package)
        observed.update(identity)
        run_id = str(uuid.uuid4())
        xml = android_ui(prefix)
        require(any(node.get("package") == package for node in ET.fromstring(xml).iter("node")),
                "Requested app is not visible in UI dump")
        screenshot = run(prefix + ["exec-out", "screencap", "-p"])
        require(android_identity(prefix, package) == identity,
                "Installed artifact changed during capture; discard observation")
        ui = store.observe(run_id + "-ui", workspace, observed, xml, "application/xml")
        image = store.observe(run_id + "-screen", workspace, observed, screenshot, "image/png")
        return {"run_id": run_id, "conditions": observed, "ui": ui, "screenshot": image,
                "notice": "Separate sequential captures, not an atomic UI snapshot; no journey executed"}


def recipe_steps(steps: Any) -> None:
    require(isinstance(steps, list) and len(steps) <= 32, "Recipe steps must be a bounded list")
    for step in steps:
        require(isinstance(step, dict), "Step must be an object")
        action = step.get("action")
        if action in ("launch", "back", "restart"):
            object_fields(step, {"action"})
        elif action in ("tap_node", "assert_node"):
            fields = {"action", "resource_id"} | ({"attributes"} if action == "assert_node" else set())
            object_fields(step, fields)
            require(isinstance(step["resource_id"], str) and bool(step["resource_id"]),
                    "A stable resource ID is required")
            if action == "assert_node":
                require(isinstance(step["attributes"], dict) and bool(step["attributes"])
                        and all(isinstance(k, str) and isinstance(v, str)
                                for k, v in step["attributes"].items()), "Invalid assertion attributes")
        else:
            raise RuntimeErrorBase("Unknown recipe action")


def run_android_recipe(store: Store, recipe: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    object_fields(recipe, {"workspace", "device", "app", "context", "steps", "restore"})
    prefix = android_scope(store, recipe["workspace"], recipe["device"], recipe["app"],
                           recipe["context"], approve)
    recipe_steps(recipe["steps"])
    recipe_steps(recipe["restore"])
    require(any(step["action"] == "assert_node" for step in recipe["steps"]),
            "A journey must contain a goal assertion")
    require(not any(step["action"] == "tap_node" for step in recipe["steps"]) or recipe["restore"],
            "Recipes that tap UI require explicit restoration steps")
    package = recipe["app"]
    run_id = str(uuid.uuid4())
    reports = []
    observed = dict(recipe["context"])
    errors: Dict[str, str] = {}

    def execute(steps: List[Dict[str, Any]], phase: str) -> None:
        for index, step in enumerate(steps):
            action = step["action"]
            if action in ("launch", "restart"):
                if action == "restart":
                    run(prefix + ["shell", "am", "force-stop", package])
                android_launch(prefix, package)
            elif action == "back":
                run(prefix + ["shell", "input", "keyevent", "4"])
            else:
                selected = []
                raw = b""
                for attempt in range(5):
                    raw = android_ui(prefix)
                    selected = [node for node in ET.fromstring(raw).iter("node")
                                if node.get("resource-id") == step["resource_id"]
                                and node.get("package") == package]
                    if selected:
                        break
                    if attempt < 4:
                        time.sleep(1)
                require(len(selected) == 1, "Resource ID must match exactly one app node")
                node = selected[0]
                evidence = store.observe(run_id + "-" + phase + "-" + str(index),
                                         recipe["workspace"], observed, raw, "application/xml")
                if action == "assert_node":
                    require(all(node.get(key) == value for key, value in step["attributes"].items()),
                            "Goal UI assertion failed")
                else:
                    require(node.get("enabled") == "true", "Cannot tap disabled node")
                    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                                          node.get("bounds", ""))
                    require(bounds is not None, "Node bounds unavailable")
                    left, top, right, bottom = map(int, bounds.groups())
                    require(right > left and bottom > top, "Node bounds invalid")
                    run(prefix + ["shell", "input", "tap", str((left + right) // 2),
                                  str((top + bottom) // 2)])
                reports.append({"phase": phase, "index": index, "action": action,
                                "evidence_id": evidence["evidence_id"]})

    with store.device_lock("android:" + recipe["device"]):
        identity = android_identity(prefix, package)
        observed.update(identity)
        for phase, steps in (("goal", recipe["steps"]), ("restore", recipe["restore"])):
            try:
                execute(steps, phase)
            except (RuntimeErrorBase, OSError, ValueError, ET.ParseError,
                    subprocess.TimeoutExpired) as exc:
                errors[phase] = str(exc)
        if android_identity(prefix, package) != identity:
            errors["identity"] = "Installed artifact changed during journey"
        restoration = ("PASS" if any(step["action"] == "assert_node" for step in recipe["restore"])
                       else "NOT_VERIFIED") if "restore" not in errors else "FAIL"
        verdict = ("FAIL" if "goal" in errors else "INCONCLUSIVE" if "identity" in errors
                   else "PARTIAL" if "restore" in errors or restoration == "NOT_VERIFIED" else "PASS")
        report = {"run_id": run_id, "verdict": verdict,
                  "goal_verdict": "FAIL" if "goal" in errors else "PASS",
                  "restoration": restoration, "conditions": observed, "steps": reports,
                  "errors": errors}
        evidence = store.observe(run_id + "-report", recipe["workspace"], observed,
                                 canonical(report).encode(), "application/json")
        return dict(report, report_evidence_id=evidence["evidence_id"])

def bundle_digest(root: Path) -> str:
    require(root.is_dir(), "Simulator app bundle is unavailable")
    entries = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Symlinked bundle requires a separately verified artifact")
        if path.is_file():
            entries.append([path.relative_to(root).as_posix(), file_digest(path)])
    require(bool(entries), "App bundle is empty")
    return digest(canonical(entries).encode())


def ios_capture(store: Store, workspace: str, udid: str, bundle_id: str,
                context: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    require(approve, "Device capture requires explicit approval")
    require(store.workspace(workspace)["platform"] == "ios", "Not an iOS workspace")
    require(isinstance(udid, str)
            and re.fullmatch(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}", udid)
            is not None, "Use an explicit simulator UUID")
    require(isinstance(bundle_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", bundle_id) is not None,
            "Invalid bundle ID")
    require(isinstance(context, dict) and "artifact_sha256" not in context,
            "Do not supply an unverified artifact fingerprint")
    conditions(dict(context, artifact_sha256="0" * 64))
    with store.device_lock("ios:" + udid):
        bundle = Path(run(["xcrun", "simctl", "get_app_container", udid, bundle_id, "app"])
                      .decode().strip())
        observed = dict(context)
        observed.update({"artifact_sha256": bundle_digest(bundle), "bundle_id": bundle_id,
                         "simulator_udid": udid})
        run_id = str(uuid.uuid4())
        image = run(["xcrun", "simctl", "io", udid, "screenshot", "--type=png", "-"])
        require(bundle_digest(bundle) == observed["artifact_sha256"],
                "Simulator app bundle changed during capture")
        evidence = store.observe(run_id, workspace, observed, image, "image/png")
        return {"run_id": run_id, "conditions": observed, "screenshot": evidence,
                "notice": "Screenshot is observation only. Import XCTest predicate results separately."}

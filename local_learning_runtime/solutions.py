"""Versioned problem contracts and bounded P/R/Q execution without model planning."""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import (Store, RuntimeErrorBase, canonical, digest, file_digest, identifier,
                   json_pointer, now, object_fields, require, workspace_file)
from .discovery import discover
from .learning import load_policy, privacy_key, run_registered
from .privacy import sanitize
from .lifecycle import assess, require_fresh, record as record_usage


def all_steps(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for step in spec["steps"]:
        result.append(step)
        if step["op"] == "branch":
            result.extend(step["then"])
            result.extend(step["else"])
    return result


def validate_spec(store: Store, spec: Dict[str, Any]) -> None:
    object_fields(spec, {"schema", "id", "workspace", "problem", "parameters",
                         "preconditions", "steps", "postconditions", "result", "boundaries"})
    require(spec["schema"] == "llr.solution.v1", "Unknown solution schema")
    identifier(spec["id"], "Solution ID")
    store.workspace(spec["workspace"])
    object_fields(spec["problem"], {"intent", "goal", "patterns"})
    require(isinstance(spec["problem"]["goal"], str) and 0 < len(spec["problem"]["goal"]) <= 2000,
            "A bounded explicit goal is required")
    identifier(spec["problem"]["intent"], "Intent")
    require(isinstance(spec["parameters"], dict) and len(spec["parameters"]) <= 16,
            "Parameters must be a bounded object")
    for name, definition in spec["parameters"].items():
        require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is not None,
                "Parameter name must be a valid bounded capture name")
        object_fields(definition, {"type", "enum"})
        require(definition["type"] in ("string", "integer", "boolean"), "Unknown parameter type")
        require(isinstance(definition["enum"], list) and 0 < len(definition["enum"]) <= 32,
                "First release requires explicit validated parameter values")
        expected_type = {"string": str, "integer": int, "boolean": bool}[definition["type"]]
        require(all(type(value) is expected_type for value in definition["enum"]),
                "Parameter enum values must have the declared type")
    require(isinstance(spec["problem"]["patterns"], list)
            and 0 < len(spec["problem"]["patterns"]) <= 16, "Provide bounded deterministic patterns")
    for pattern in spec["problem"]["patterns"]:
        compile_pattern(pattern, spec["parameters"])
    def valid_path(value):
        if isinstance(value, str):
            require(bool(value) and not Path(value).is_absolute() and ".." not in Path(value).parts,
                    "Solution paths must stay relative to approved workspace")
        else:
            object_fields(value, {"input"})
            require(value["input"] in spec["parameters"]
                    and spec["parameters"][value["input"]]["type"] == "string",
                    "Path must reference a declared string parameter")
    require(isinstance(spec["preconditions"], list) and 0 < len(spec["preconditions"]) <= 32,
            "Declare 1-32 preconditions")
    for condition in spec["preconditions"]:
        require(isinstance(condition, dict), "Invalid precondition")
        if condition.get("kind") == "condition_equals":
            object_fields(condition, {"kind", "name", "expected"})
            from .privacy import SENSITIVE_KEY
            require(isinstance(condition["name"], str) and not SENSITIVE_KEY.search(condition["name"]),
                    "Secret identifiers cannot become reusable condition literals")
        elif condition.get("kind") == "file_sha256":
            object_fields(condition, {"kind", "path", "expected"})
            valid_path(condition["path"])
            require(re.fullmatch(r"[0-9a-f]{64}", condition["expected"]) is not None,
                    "Invalid source fingerprint")
        elif condition.get("kind") == "solution_dependency":
            object_fields(condition, {"kind", "id", "version"})
            identifier(condition["id"], "Dependency")
            require(type(condition["version"]) is int and condition["version"] > 0,
                    "Invalid dependency version")
        else:
            raise RuntimeErrorBase("Unknown precondition collector")
    require(isinstance(spec["steps"], list) and 0 < len(spec["steps"]) <= 16,
            "Declare 1-16 steps")
    step_ids = []
    for step in spec["steps"]:
        require(isinstance(step, dict), "Invalid step")
        identifier(step.get("id"), "Step ID")
        require(step["id"] not in step_ids, "Duplicate step ID")
        if step.get("op") == "inspect_file":
            object_fields(step, {"id", "op", "path"})
            valid_path(step["path"])
        elif step.get("op") == "run_validator":
            object_fields(step, {"id", "op", "runner"}, {"approval_sha256"})
            identifier(step["runner"], "Runner")
        elif step.get("op") == "select":
            object_fields(step, {"id", "op", "step", "pointer"})
            require(step["step"] in step_ids, "Step reference must point backward")
            require(isinstance(step["pointer"], str) and (step["pointer"] == ""
                    or step["pointer"].startswith("/")), "Invalid selection pointer")
        elif step.get("op") == "branch":
            object_fields(step, {"id", "op", "step", "pointer", "expected", "then", "else"})
            require(step["step"] in step_ids, "Branch input must reference an earlier step")
            require(isinstance(step["pointer"], str) and (step["pointer"] == ""
                    or step["pointer"].startswith("/")), "Invalid branch pointer")
            child_ids = set()
            for side in ("then", "else"):
                require(isinstance(step[side], list) and len(step[side]) <= 8, "Branch exceeds 8 steps")
                available = set(step_ids)
                for child in step[side]:
                    identifier(child.get("id"), "Branch step")
                    require(child["id"] not in child_ids | set(step_ids) | {step["id"]},
                            "Duplicate branch step")
                    child_ids.add(child["id"])
                    if child.get("op") == "inspect_file":
                        object_fields(child, {"id", "op", "path"})
                        valid_path(child["path"])
                    elif child.get("op") == "run_validator":
                        object_fields(child, {"id", "op", "runner"}, {"approval_sha256"})
                        identifier(child["runner"], "Runner")
                    elif child.get("op") == "select":
                        object_fields(child, {"id", "op", "step", "pointer"})
                        require(child["step"] in available, "Branch reference must point backward in same path")
                        require(isinstance(child["pointer"], str) and (child["pointer"] == ""
                                or child["pointer"].startswith("/")), "Invalid branch selection pointer")
                    else:
                        raise RuntimeErrorBase("Nested/unapproved branch operation")
                    available.add(child["id"])
        else:
            raise RuntimeErrorBase("Unknown/unapproved operation; no arbitrary commands")
        step_ids.append(step["id"])
    flat_ids = [step["id"] for step in all_steps(spec)]
    require(len(flat_ids) <= 32 and len(set(flat_ids)) == len(flat_ids), "Too many/duplicate flattened steps")
    require(isinstance(spec["postconditions"], list) and 0 < len(spec["postconditions"]) <= 16,
            "Declare 1-16 success assertions")
    for check in spec["postconditions"]:
        object_fields(check, {"step", "pointer", "expected"})
        require(check["step"] in step_ids, "Unknown postcondition step")
        require(isinstance(check["pointer"], str) and (check["pointer"] == ""
                or check["pointer"].startswith("/")), "Invalid postcondition pointer")
    require(isinstance(spec["boundaries"], list) and bool(spec["boundaries"]),
            "Declare unverified scope and invalidators")
    require(isinstance(spec["result"], dict), "Result must be structured, not regenerated prose")
    def validate_reference(value):
        if isinstance(value, dict):
            if set(value) == {"input"}:
                require(value["input"] in spec["parameters"], "Undeclared input reference")
            elif set(value) == {"step", "pointer"}:
                require(value["step"] in step_ids and isinstance(value["pointer"], str)
                        and (value["pointer"] == "" or value["pointer"].startswith("/")),
                        "Invalid output reference")
            else:
                for item in value.values():
                    validate_reference(item)
        elif isinstance(value, list):
            for item in value:
                validate_reference(item)
    validate_reference(spec["result"])
    for check in spec["postconditions"]:
        validate_reference(check["expected"])
    for step in spec["steps"]:
        if step["op"] == "branch":
            validate_reference(step["expected"])
    require(len(canonical(spec).encode()) <= 32_000, "Solution exceeds 32000 UTF-8 bytes")


class PatternMatch:
    def __init__(self, captures):
        self.captures = captures

    def groupdict(self):
        return self.captures


class LiteralPattern:
    def __init__(self, literals, names):
        self.literals, self.names = literals, names

    @staticmethod
    def folded(text):
        # ASCII case folding preserves character offsets for Unicode paths and labels.
        return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))

    def fullmatch(self, query):
        folded = self.folded(query)
        if not folded.startswith(self.folded(self.literals[0])):
            return None
        position, captures = len(self.literals[0]), {}
        for index, name in enumerate(self.names):
            delimiter = self.literals[index + 1]
            if index == len(self.names) - 1:
                if not folded.endswith(self.folded(delimiter)):
                    return None
                end = len(query) - len(delimiter) if delimiter else len(query)
            else:
                end = folded.find(self.folded(delimiter), position)
                if end < 0:
                    return None
            if end <= position:
                return None
            captures[name] = query[position:end]
            position = end + len(delimiter)
        if position != len(query):
            return None
        return PatternMatch(captures)


def compile_pattern(pattern: str, parameters: Dict[str, Any]) -> LiteralPattern:
    require(isinstance(pattern, str) and 0 < len(pattern) <= 1000, "Pattern must be bounded")
    parts, names, position, used = [], [], 0, set()
    for match in re.finditer(r"\{([A-Za-z0-9_.-]+)\}", pattern):
        name = match.group(1)
        require(name in parameters and name not in used, "Unknown/repeated pattern parameter")
        used.add(name)
        literal = pattern[position:match.start()]
        require(not names or bool(literal), "Adjacent placeholders require a literal delimiter")
        parts.append(literal)
        names.append(name)
        position = match.end()
    parts.append(pattern[position:])
    require("{" not in re.sub(r"\{([A-Za-z0-9_.-]+)\}", "", pattern),
            "Invalid pattern placeholder")
    return LiteralPattern(parts, names)


def bind(value: Any, parameters: Dict[str, Any], outputs: Optional[Dict[str, Any]] = None) -> Any:
    if isinstance(value, dict):
        if set(value) == {"input"}:
            require(value["input"] in parameters, "Unbound parameter")
            return parameters[value["input"]]
        if set(value) == {"step", "pointer"}:
            require(outputs is not None and value["step"] in outputs, "Unknown result step")
            return json_pointer(outputs[value["step"]], value["pointer"])
        return {name: bind(item, parameters, outputs) for name, item in value.items()}
    if isinstance(value, list):
        return [bind(item, parameters, outputs) for item in value]
    return value


def parameters_match(spec: Dict[str, Any], values: Dict[str, Any]) -> List[str]:
    missing = [name for name in spec["parameters"] if name not in values]
    require(values.keys() <= spec["parameters"].keys(), "Unexpected input parameters")
    for name, value in values.items():
        definition = spec["parameters"][name]
        expected_type = {"string": str, "integer": int, "boolean": bool}[definition["type"]]
        require(type(value) is expected_type, "Wrong parameter type: " + name)
        require(any(type(value) is type(item) and value == item for item in definition["enum"]),
                "Parameter outside verified/approved range: " + name)
    return missing


def propose_solution(store: Store, spec: Dict[str, Any], expected_version: int,
                     approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required for solution scope and operations")
    spec = json.loads(canonical(spec))
    validate_spec(store, spec)
    load_policy(store, spec["workspace"])
    for step in all_steps(spec):
        if step["op"] == "run_validator":
            path = store.home / "runners.json"
            runners = json.loads(path.read_text()) if path.exists() else {}
            key = spec["workspace"] + ":" + step["runner"]
            require(key in runners, "Solution references an unapproved runner")
            fingerprint = digest(canonical(runners[key]).encode())
            require(step.get("approval_sha256", fingerprint) == fingerprint,
                    "Declared runner approval changed")
            step["approval_sha256"] = fingerprint
    safe, redactions = sanitize(spec, privacy_key(store))
    require(not redactions, "Solution contract contains private literals; use nonsecret parameters")
    body = canonical(safe)
    with store.transaction():
        old = store.db.execute("SELECT version FROM solutions WHERE workspace=? AND id=?",
                               (spec["workspace"], spec["id"])).fetchone()
        require((old["version"] if old else 0) == expected_version, "Solution version conflict")
        version = expected_version + 1
        store.db.execute("INSERT INTO solutions VALUES (?,?,?,?,?,?,?) ON CONFLICT(workspace,id) "
                         "DO UPDATE SET version=excluded.version,lifecycle=excluded.lifecycle,"
                         "body=excluded.body,fingerprint=excluded.fingerprint,updated_at=excluded.updated_at",
                         (spec["id"], spec["workspace"], version, "candidate", body, digest(body.encode()), now()))
        store.db.execute("INSERT INTO solution_versions VALUES (?,?,?,?,?)",
                         (spec["workspace"], spec["id"], version, body, digest(body.encode())))
    return {"status": "CANDIDATE", "solution": spec["id"], "version": version}


def get_solution(store: Store, workspace: str, solution: str) -> Dict[str, Any]:
    row = store.db.execute("SELECT * FROM solutions WHERE workspace=? AND id=?",
                           (workspace, solution)).fetchone()
    require(row is not None, "Solution not found")
    require(digest(row["body"].encode()) == row["fingerprint"], "Solution fingerprint mismatch")
    return dict(row, spec=json.loads(row["body"]))


def check_preconditions(store: Store, record: Dict[str, Any], parameters: Dict[str, Any],
                        actual: Dict[str, Any], visiting: Optional[set] = None) -> List[Dict[str, Any]]:
    results = []
    visiting = set() if visiting is None else set(visiting)
    key = (record["workspace"], record["id"])
    if key in visiting:
        return [{"index": "dependency", "verdict": "FAIL", "detail": "Solution dependency cycle"}]
    visiting.add(key)
    root = Path(store.workspace(record["workspace"])["root"])
    for index, condition in enumerate(record["spec"]["preconditions"]):
        condition = bind(condition, parameters)
        verdict, detail = "UNKNOWN", "Condition could not be attested"
        if condition["kind"] == "condition_equals":
            if condition["name"] in actual:
                expected, value = condition["expected"], actual[condition["name"]]
                verdict = "PASS" if canonical(expected) == canonical(value) else "FAIL"
                detail = "Compared supplied condition; caller attestation required"
        elif condition["kind"] == "file_sha256":
            try:
                value = file_digest(workspace_file(root, condition["path"]))
            except (RuntimeErrorBase, OSError) as exc:
                verdict, detail = "FAIL", str(exc)
            else:
                verdict = "PASS" if value == condition["expected"] else "FAIL"
                detail = "Current workspace file fingerprint"
        else:
            row = store.db.execute("SELECT version,lifecycle FROM solutions WHERE workspace=? AND id=?",
                                   (record["workspace"], condition["id"])).fetchone()
            verdict = "PASS" if (row and row["version"] == condition["version"]
                                  and row["lifecycle"] == "verified") else "FAIL"
            detail = "Pinned solution dependency"
            if verdict == "PASS":
                dependency = get_solution(store, record["workspace"], condition["id"])
                try:
                    require_fresh(store, record["workspace"], "solution", condition["id"], actual=actual)
                except RuntimeErrorBase as exc:
                    verdict, detail = "FAIL", str(exc)
                if verdict == "PASS" and dependency["spec"]["parameters"]:
                    verdict, detail = "UNKNOWN", "Dependency requires its own parameter binding"
                elif verdict == "PASS":
                    dependent_checks = check_preconditions(store, dependency, {}, actual, visiting)
                    verdict = ("FAIL" if any(c["verdict"] == "FAIL" for c in dependent_checks)
                               else "UNKNOWN" if any(c["verdict"] == "UNKNOWN" for c in dependent_checks)
                               else "PASS")
        results.append({"index": index, "verdict": verdict, "detail": detail})
    for step in all_steps(record["spec"]):
        if step["op"] == "run_validator":
            path = store.home / "runners.json"
            runners = json.loads(path.read_text()) if path.exists() else {}
            registered = runners.get(record["workspace"] + ":" + step["runner"])
            matches = registered is not None and digest(canonical(registered).encode()) == step["approval_sha256"]
            results.append({"index": "runner:" + step["id"], "verdict": "PASS" if matches else "FAIL",
                            "detail": "Pinned runner authorization"})
    return results


def route(store: Store, cwd: Path, query: str, session: str,
          supplied: Optional[Dict[str, Any]] = None,
          actual: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .discovery import infer_platform
    require(isinstance(query, str) and 0 < len(query.encode()) <= 4000, "Invalid task query")
    scope = discover(store, cwd, session, infer_platform(query))
    if scope["status"] != "DISCOVERED":
        return scope
    candidates = []
    for row in store.db.execute("SELECT id FROM solutions WHERE workspace=?", (scope["workspace"],)):
        record = get_solution(store, scope["workspace"], row["id"])
        spec = record["spec"]
        for pattern in spec["problem"]["patterns"]:
            match = compile_pattern(pattern, spec["parameters"]).fullmatch(query.strip())
            if match:
                values = dict(match.groupdict())
                for name, value in list(values.items()):
                    kind = spec["parameters"][name]["type"]
                    if kind == "integer":
                        require(re.fullmatch(r"-?\d+", value) is not None, "Invalid integer parameter")
                        values[name] = int(value)
                    elif kind == "boolean":
                        require(value.lower() in ("true", "false"), "Invalid boolean parameter")
                        values[name] = value.lower() == "true"
                for name, value in (supplied or {}).items():
                    require(name not in values or values[name] == value, "Conflicting input parameters")
                    values[name] = value
                candidates.append((record, values))
                break
    if len(candidates) != 1:
        return {"status": "NEEDS_EXPLORATION" if not candidates else "AMBIGUOUS",
                "workspace": scope["workspace"], "candidates": [r["id"] for r, _ in candidates],
                "model_calls": 0}
    record, values = candidates[0]
    try:
        missing = parameters_match(record["spec"], values)
    except RuntimeErrorBase as exc:
        return {"status": "BLOCKED", "reason": str(exc), "model_calls": 0}
    if missing:
        return {"status": "NEEDS_INPUT", "missing": missing, "model_calls": 0}
    checks = check_preconditions(store, record, values, actual or {})
    case_key = digest(canonical(values).encode())
    freshness = assess(store, record["workspace"], "solution", record["id"], case_key, actual)
    case = store.db.execute("SELECT 1 FROM solution_cases WHERE workspace=? AND solution=? AND "
                            "version=? AND parameters_sha=?", (record["workspace"], record["id"],
                            record["version"], digest(canonical(values).encode()))).fetchone()
    state = ("BLOCKED" if record["lifecycle"] not in ("verified", "candidate")
             or any(check["verdict"] == "FAIL" for check in checks)
             or (record["lifecycle"] == "verified" and case
                 and freshness["status"] not in ("FRESH", "CONDITIONS_UNKNOWN"))
             else "NEEDS_INPUT" if any(check["verdict"] == "UNKNOWN" for check in checks)
             or (case and freshness["status"] == "CONDITIONS_UNKNOWN")
             else "LOCAL_READY" if record["lifecycle"] == "verified" and case else "NEEDS_EXPLORATION")
    record_usage(store, record["workspace"], "solution", record["id"], record["version"],
                 "selected" if state == "LOCAL_READY" else "rejected", session,
                 "route:" + digest(canonical([query, values, actual]).encode()), case_key, state)
    return {"status": state, "workspace": record["workspace"], "solution": record["id"],
            "freshness": freshness,
            "version": record["version"], "parameters": values, "checks": checks,
            "goal": record["spec"]["problem"]["goal"], "boundaries": record["spec"]["boundaries"],
            "model_calls": 0}


def execute_solution(store: Store, workspace: str, solution: str, session: str,
                     invocation: str, parameters: Dict[str, Any], actual: Dict[str, Any],
                     verify_candidate: bool = False, approve: bool = False) -> Dict[str, Any]:
    identifier(session, "Session")
    identifier(invocation, "Invocation")
    load_policy(store, workspace)
    record = get_solution(store, workspace, solution)
    spec = record["spec"]
    require(record["lifecycle"] == "verified" or (verify_candidate and approve
            and record["lifecycle"] == "candidate"), "Solution not verified/approved for trial")
    require(not parameters_match(spec, parameters), "Missing parameters")
    run_id = digest(canonical([workspace, solution, record["version"], session, invocation]).encode())
    input_sha = digest(canonical([parameters, actual]).encode())
    previous_run = store.db.execute("SELECT state,input_sha FROM solution_runs WHERE id=?", (run_id,)).fetchone()
    if previous_run:
        require(previous_run["input_sha"] == input_sha, "Invocation replay changed inputs")
        require(previous_run["state"] != "RUNNING", "Interrupted/running solution; review before repeating")
    if not verify_candidate:
        require(store.db.execute("SELECT 1 FROM solution_cases WHERE workspace=? AND solution=? "
                                "AND version=? AND parameters_sha=?", (workspace, solution,
                                record["version"], digest(canonical(parameters).encode()))).fetchone()
                is not None, "Parameter combination has not been verified")
    else:
        require(approve, "Explicit approval required to validate a new parameter combination")
    checks = check_preconditions(store, record, parameters, actual)
    case_key = digest(canonical(parameters).encode())
    if not verify_candidate:
        health = assess(store, workspace, "solution", solution, case_key, actual)
        if health["status"] != "FRESH":
            record_usage(store, workspace, "solution", solution, record["version"], "rejected",
                         session, invocation, case_key, "Memory " + health["status"])
            return {"status": "NEEDS_INPUT" if health["status"] == "CONDITIONS_UNKNOWN" else "BLOCKED",
                    "freshness": health, "model_calls": 0}
    if any(check["verdict"] != "PASS" for check in checks):
        record_usage(store, workspace, "solution", solution, record["version"], "rejected",
                     session, invocation, case_key, "Preconditions changed or unknown")
        return {"status": "NEEDS_INPUT" if any(c["verdict"] == "UNKNOWN" for c in checks) else "BLOCKED",
                "checks": checks, "model_calls": 0}
    with store.transaction():
        old = store.db.execute("SELECT * FROM solution_runs WHERE id=?", (run_id,)).fetchone()
        if old:
            require(old["input_sha"] == input_sha, "Invocation replay changed inputs")
            require(old["state"] != "RUNNING", "Interrupted/running solution; review before repeating")
            return json.loads(old["body"])
        store.db.execute("INSERT INTO solution_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, workspace, solution, record["version"], session, input_sha,
                          "RUNNING", "{}", now()))
        record_usage(store, workspace, "solution", solution, record["version"], "executed",
                     session, run_id, case_key)
    outputs, verdict, error = {}, "PASS", None
    started = time.monotonic()
    try:
        root = Path(store.workspace(workspace)["root"])
        def execute_step(step, context):
            require(time.monotonic() - started < 60, "Solution execution time budget exhausted")
            if step["op"] == "inspect_file":
                path = workspace_file(root, bind(step["path"], parameters))
                return {"sha256": file_digest(path), "bytes": path.stat().st_size}
            elif step["op"] == "run_validator":
                result = run_registered(store, workspace, step["runner"], session,
                                        "solution-" + digest(solution.encode())[:24],
                                        digest(canonical([invocation, step["id"]]).encode())[:24])
                require(not result.get("error"), "Validator did not complete")
                require(result.get("knowledge") is not None, "Validator predicates were not published")
                return result
            elif step["op"] == "select":
                return json_pointer(context[step["step"]], step["pointer"])
            observed = json_pointer(context[step["step"]], step["pointer"])
            expected = bind(step["expected"], parameters, context)
            side = "then" if canonical(observed) == canonical(expected) else "else"
            if not verify_candidate:
                previous_runs = store.db.execute("SELECT body FROM solution_runs WHERE workspace=? AND "
                    "solution=? AND version=? AND state='LOCAL_SOLVED'",
                    (workspace, solution, record["version"])).fetchall()
                require(any(json.loads(row["body"]).get("parameters") == parameters
                            and json.loads(row["body"]).get("outputs", {}).get(step["id"], {}).get("selected") == side
                            for row in previous_runs), "Selected branch path has not been verified")
            children = {}
            for child in step[side]:
                children[child["id"]] = execute_step(child, dict(context, **children))
            return {"selected": side, "outputs": children}
        for step in spec["steps"]:
            outputs[step["id"]] = execute_step(step, outputs)
        assertions = []
        for check in spec["postconditions"]:
            observed = json_pointer(outputs[check["step"]], check["pointer"])
            expected = bind(check["expected"], parameters, outputs)
            assertions.append({"step": check["step"], "pointer": check["pointer"],
                               "expected": expected, "observed": observed,
                               "passed": canonical(observed) == canonical(expected)})
        require(all(check["passed"] for check in assertions), "Solution goal assertions failed")
        require(all(c["verdict"] == "PASS" for c in check_preconditions(
            store, record, parameters, actual)), "Conditions changed during execution")
    except (RuntimeErrorBase, OSError, ValueError, KeyError) as exc:
        verdict, error, assertions = "FAIL", str(exc), []
    safe_outputs, redactions = sanitize(outputs, privacy_key(store))
    require(not redactions, "Execution evidence changed under redaction; cannot claim success")
    report = {"status": "LOCAL_SOLVED" if verdict == "PASS" else "FAILED", "run_id": run_id,
              "solution": solution, "version": record["version"], "parameters": parameters, "checks": checks,
              "assertions": assertions, "outputs": safe_outputs,
              "result": bind(spec["result"], parameters, safe_outputs) if verdict == "PASS" else None,
              "boundaries": spec["boundaries"], "error": error, "model_calls": 0}
    report["model_call_scope"] = (
        "Agent made no model requests; external validator internals are not independently attested"
        if any(step["op"] == "run_validator" for step in all_steps(spec))
        else "Pure local file/selection steps; no model provider invoked")
    if verdict == "PASS":
        provenance = {"source": "solution-execution", "run_id": run_id, "checks": checks,
                      "assertions": assertions, "conditions": actual,
                      "parameters": parameters, "validated_at": now()}
        used_conditions = {condition["name"]: actual[condition["name"]]
                           for condition in spec["preconditions"]
                           if condition["kind"] == "condition_equals" and condition["name"] in actual}
        provenance["conditions"], private_changes = sanitize(used_conditions, privacy_key(store))
        require(not private_changes, "Required proof condition was redacted; success cannot be published")
        evidence_conditions = {"artifact_sha256": record["fingerprint"],
                               "solution_version": record["version"], "proof_kind": "local_solution"}
        report["evidence"] = store.observe(run_id, workspace, evidence_conditions,
                                           canonical(provenance).encode(), "application/json")
    with store.transaction():
        store.db.execute("UPDATE solution_runs SET state=?,body=?,updated_at=? WHERE id=?",
                         (report["status"], canonical(report), now(), run_id))
        if verdict == "PASS" and verify_candidate:
            current = get_solution(store, workspace, solution)
            require(current["version"] == record["version"], "Solution changed during verification")
            store.db.execute("UPDATE solutions SET lifecycle='verified',updated_at=? WHERE workspace=? "
                             "AND id=?", (now(), workspace, solution))
            store.db.execute("INSERT OR IGNORE INTO solution_cases VALUES (?,?,?,?,?)",
                             (workspace, solution, record["version"],
                              digest(canonical(parameters).encode()), run_id))
        record_usage(store, workspace, "solution", solution, record["version"],
                     "verified_success" if verdict == "PASS" else "failure",
                     session, run_id, case_key, error or "")
    return report


def invalidate_solution(store: Store, workspace: str, solution: str, version: int,
                        lifecycle: str, approve: bool) -> Dict[str, Any]:
    require(approve, "Explicit approval required to invalidate a solution")
    require(lifecycle in ("stale", "conflicted", "superseded"), "Invalid lifecycle")
    with store.transaction():
        record = get_solution(store, workspace, solution)
        require(record["version"] == version, "Solution version conflict")
        store.db.execute("UPDATE solutions SET lifecycle=?,updated_at=? WHERE workspace=? AND id=?",
                         (lifecycle, now(), workspace, solution))
    return {"status": lifecycle, "solution": solution, "version": version}


def compile_from_milestone(store: Store, event_id: str, problem: Dict[str, Any],
                           approve: bool) -> Dict[str, Any]:
    """Build a candidate P/R/Q contract, never promote an observation to a solution."""
    require(approve, "Declared problem/operation scope must be approved")
    row = store.db.execute("SELECT * FROM milestones WHERE id=?", (event_id,)).fetchone()
    require(row is not None and row["state"] == "PUBLISHED", "Published milestone required")
    event = json.loads(row["body"])
    runner = event.get("source", {}).get("runner")
    require(isinstance(runner, str), "No registered runtime runner in provenance")
    spec = {"schema": "llr.solution.v1", "id": "compiled-" + event_id[:24],
            "workspace": event["workspace"], "problem": problem, "parameters": {},
            "preconditions": [{"kind": "condition_equals", "name": name, "expected": value}
                              for name, value in event["conditions"].items()],
            "steps": [{"id": "verify", "op": "run_validator", "runner": runner}],
            "postconditions": [
                {"step": "verify", "pointer": "/knowledge/verified_predicates/" + str(index) + "/expected",
                 "expected": check["expected"]} for index, check in enumerate(event["checks"])],
            "result": {"goal": problem["goal"], "evidence": {"step": "verify", "pointer": "/knowledge"}},
            "boundaries": ["Only the declared runner predicates and recorded conditions.",
                           "This is a candidate; a complete solution trial is still required."]}
    registry = json.loads((store.home / "runners.json").read_text())
    key = event["workspace"] + ":" + runner
    require(key in registry, "Runner authorization is no longer available")
    spec["steps"][0]["approval_sha256"] = digest(canonical(registry[key]).encode())
    existing = store.db.execute("SELECT version,body FROM solutions WHERE workspace=? AND id=?",
                                (spec["workspace"], spec["id"])).fetchone()
    if existing and json.loads(existing["body"]) == spec:
        return {"status": "NOOP", "solution": spec["id"], "version": existing["version"]}
    return propose_solution(store, spec, existing["version"] if existing else 0, approve)

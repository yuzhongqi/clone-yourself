"""Reviewed legacy intake and a static, offline human/agent knowledge view."""

import base64
import html
import json
from pathlib import Path
from typing import Any, Dict, List
import xml.etree.ElementTree as ET

from .core import (MAX_PACKET_BYTES, RuntimeErrorBase, Store, canonical, digest,
                   identifier, now, require, require_private_location)


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
        from .learning import status, graph
        tasks = [item for item in status(store)["tasks"] if item["workspace"] == workspace]
        projection = graph(store, workspace)
        for row in store.db.execute("SELECT m.* FROM milestones m JOIN task_runs t ON t.id=m.task_run "
                                   "WHERE t.workspace=? AND m.state!='PUBLISHED'", (workspace,)):
            event = json.loads(row["body"])
            candidates.append({"candidate_id": row["id"], "spec": {
                "id": row["id"], "task_type": event["title"],
                "statement": canonical(event["observation"]), "conditions": event["conditions"]},
                "status": row["state"], "reason": row["reason"], "epistemic_kind": event["kind"]})
    return {"schema": "llr.catalog.v1", "workspace": workspace, "revision": revision,
            "claims": sorted(claims, key=lambda item: item["spec"]["id"]),
            "legacy": legacy, "candidates": candidates, "questions": questions,
            "tasks": tasks, "graph": projection}


def escaped(value: Any) -> str:
    return html.escape(str(value))


def technical_details(item: Dict[str, Any]) -> str:
    return ('<details class="technical"><summary>技术记录（ID、版本与原始数据）</summary><pre>'
            + escaped(json.dumps(item, ensure_ascii=False, indent=2)) + "</pre></details>")


def condition_list(values: Dict[str, Any]) -> str:
    labels = {"artifact_sha256": "安装包指纹", "os_api": "Android API",
              "account_state": "账户核验", "package": "应用包名",
              "purpose": "验证用途", "account_kind": "账户类型",
              "locale": "语言", "bundle_id": "应用标识"}
    translations = {"not-attested": "未独立核验", "read-only-acceptance": "只读验收"}
    return "<dl>" + "".join("<dt>" + escaped(labels.get(key, key)) + "</dt><dd>"
                            + escaped(translations.get(value, value)) + "</dd>"
                            for key, value in values.items()) + "</dl>"


def published_card(claim: Dict[str, Any], evidence_labels: Dict[str, str]) -> str:
    spec = claim["spec"]
    passed = claim["local_integrity"]["status"] == "PASS"
    title = spec["statement"]
    explanation = "已登记的检查支持下面这条结论。"
    if spec.get("provenance", {}).get("kind") == "runtime_observation":
        title = spec["provenance"].get("title", title)
        explanation = ("已批准的本地验证器实际运行后，系统自动保存并核验了这组限定断言。"
                       "结论只适用于记录的对象和条件，不代表整个功能或所有版本正确。")
    # Only the constrained acceptance producer owns this exact statement format.
    prefix = "In this recorded run, the unique enabled UI node "
    suffix = (" was observed before and after app restart. This does not prove "
              "account identity, full feature operation, or source execution.")
    if (spec["task_type"] == "app-restart-observation"
            and title.startswith(prefix) and title.endswith(suffix)):
        title = "应用重启后，指定界面节点仍存在且可用"
        explanation = ("这次运行在重启前、重启后都找到了同一个唯一且启用的界面节点。"
                       "这只能证明该节点的可见结构与启用状态，不能证明整个功能正常。")
    links = []
    for evidence_id in dict.fromkeys(check["evidence_id"] for check in spec["checks"]):
        links.append('<a href="#evidence-' + escaped(evidence_id) + '">'
                     + escaped(evidence_labels[evidence_id]) + "</a>")
    status = "已发布 · 记录条件下检查通过" if passed else "暂停复用 · 需要检查"
    reason = ("新任务仍需核对当前构建、账户和配置，不能把历史结果当作现在的保证。"
              if passed else claim["local_integrity"]["reason"])
    return ('<article class="card"><span class="badge ' + ("pass" if passed else "warn") + '">'
            + status + "</span><h3>" + escaped(title) + "</h3><p>" + escaped(explanation)
            + '</p><div class="callout"><strong>怎么使用？</strong><p>' + escaped(reason)
            + '</p></div><p class="muted">未证明：账户身份、全部业务流程、具体源码调用链。'
            + '</p><h4>验证依据</h4><p class="links">' + " · ".join(links)
            + '</p><details><summary>适用条件与复验提示</summary>'
            + condition_list(spec["conditions"])
            + '<p>构建或条件变化后，用相同操作与断言重新验证；agent 使用前调用 recall。</p>'
            + "</details>" + technical_details(claim) + "</article>")


def legacy_card(item: Dict[str, Any]) -> str:
    node = item["source_node"]
    status = {"partial": "原记录只完成了一部分", "observed": "原记录包含运行观察",
              "needs_user_decision": "需要用户决定", "candidate": "原记录仍为候选"}
    return ('<article class="card"><span class="badge warn">历史笔记 · 尚未重新验证</span><h3>'
            + escaped(node.get("title", node["id"])) + "</h3><p>"
            + escaped(node.get("summary", "原记录未提供摘要。")) + '</p><p class="muted">'
            + escaped(status.get(node.get("status"), "原状态：" + str(node.get("status", "未知"))))
            + '</p><div class="callout">可用于寻找线索，不能据此声明当前 app 已验证通过。'
            + "需要按原条件复验后才能发布。</div>" + technical_details(item) + "</article>")


def export_portal(store: Store, workspace: str, output: Path) -> Dict[str, Any]:
    output = output.expanduser().resolve()
    require_private_location(output)
    require(not output.exists(), "Portal output already exists; choose a new revision directory")
    data = catalog(store, workspace)
    evidence_sections = []
    total = 0
    evidence_ids = sorted({check["evidence_id"] for claim in data["claims"]
                           for check in claim["spec"]["checks"]})
    evidence_labels = {item: "证据 " + str(index + 1)
                       for index, item in enumerate(evidence_ids)}
    for evidence_id in evidence_ids:
        try:
            row, raw = store.evidence(evidence_id)
        except (RuntimeErrorBase, OSError) as exc:
            evidence_sections.append('<article id="evidence-' + escaped(evidence_id) + '"><h3>'
                                     + evidence_labels[evidence_id]
                                     + "</h3><p>证据不可用：" + escaped(exc) + "</p></article>")
            continue
        total += len(raw)
        require(total <= 32 * 1024 * 1024, "Portal evidence exceeds 32 MiB")
        if row["media_type"] == "image/png":
            body = '<img alt="Recorded evidence" src="data:image/png;base64,' \
                   + base64.b64encode(raw).decode("ascii") + '">'
        else:
            if row["media_type"] == "application/xml":
                nodes = list(ET.fromstring(raw).iter("node"))
                relevant = [check for claim in data["claims"] for check in claim["spec"]["checks"]
                            if check["evidence_id"] == evidence_id and check["kind"] == "ui_node"]
                rows = []
                for check in relevant:
                    selected = [node for node in nodes
                                if all(node.get(k) == v for k, v in check["selector"].items())]
                    for node in selected:
                        rows.append("<tr><td>" + escaped(node.get("resource-id", "")) + "</td><td>"
                                    + escaped(node.get("text") or "无文本") + "</td><td>"
                                    + ("是" if node.get("enabled") == "true" else "否")
                                    + "</td></tr>")
                body = ("<p>来自 app 的界面结构快照。下表只展示与断言相关的节点，"
                        "不是整个界面的功能测试。</p><div class=\"table-scroll\"><table>"
                        "<tr><th>目标节点</th><th>显示文字</th><th>是否启用</th></tr>"
                        + "".join(dict.fromkeys(rows)) + "</table></div>")
            else:
                result = json.loads(raw)
                body = "<p>来自运行报告的结构化证据；完整内容可在下方展开。</p>"
                if isinstance(result, dict) and "goal_verdict" in result:
                    labels = {"PASS": "通过", "FAIL": "失败", "PARTIAL": "部分完成",
                              "INCONCLUSIVE": "无法确定", "NOT_VERIFIED": "未验证"}
                    body += condition_list({
                        "目标断言": labels.get(result["goal_verdict"], result["goal_verdict"]),
                        "观测后置条件": labels.get(result.get("restoration"), result.get("restoration")),
                        "整体结果": labels.get(result.get("verdict"), result.get("verdict"))})
            body += "<details><summary>查看原始证据</summary><pre>" \
                    + escaped(raw.decode("utf-8")) + "</pre></details>"
        evidence_sections.append('<article class="card" id="evidence-' + escaped(evidence_id)
                                 + '"><h3>' + evidence_labels[evidence_id] + "</h3>" + body
                                 + '<details><summary>证据来源与指纹</summary>'
                                 + condition_list(dict(json.loads(row["conditions"]),
                                                       sha256=row["sha256"]))
                                 + "</details></article>")
    sections = [
        '<section id="verified"><h2>已经验证了什么</h2>'
        + ("".join(published_card(item, evidence_labels) for item in data["claims"])
           or "<p>还没有发布知识。先完成一个有证据的运行验证。</p>") + "</section>",
        '<section id="history"><h2>以前积累的线索</h2><p class="muted">'
        + "保留旧笔记的范围和未完成项；导入不代表验证。</p>"
        + ("".join(legacy_card(item) for item in data["legacy"]) or "<p>没有导入历史记录。</p>")
        + "</section>"]
    pending = []
    for item in data["candidates"]:
        task_name = {"app-restart-observation": "应用重启观察"}.get(
            item["spec"]["task_type"], item["spec"]["task_type"])
        pending.append('<article class="card"><span class="badge warn">待审阅 · 未发布</span><h3>'
                       + escaped(task_name) + '：候选结论尚未发布</h3><p>'
                       + '候选不等于事实。检查证据、断言和适用范围后，再决定是否发布。</p>'
                       + '<details><summary>查看候选原文</summary><p>'
                       + escaped(item["spec"]["statement"]) + '</p></details>'
                       + technical_details(item) + "</article>")
    for item in data["questions"]:
        pending.append('<article class="card"><h3>待调查：'
                       + escaped(item["task"]["task_type"]) + '</h3><p>'
                       + "这个任务尚未找到可直接复用的知识，需要调查或复验。</p>"
                       + technical_details(item) + "</article>")
    sections.append('<section id="pending"><h2>还需要做什么</h2>'
                    + ("".join(pending) or "<p>没有登记待处理项；这不表示已覆盖全部功能。</p>")
                    + "</section>")
    page = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>本地学习 · 知识与验证</title><style>
:root{color-scheme:light}*{box-sizing:border-box}
body{font:16px/1.7 system-ui,sans-serif;margin:0;background:#f4f6fa;color:#24324a}
main{max-width:1050px;margin:auto;padding:32px 20px}
h1{font-size:32px;margin:0}h2{margin:36px 0 12px}h3{font-size:21px;margin:12px 0}
h4{margin-bottom:8px}.muted{color:#58677b}
.card{background:white;border:1px solid #dce3ee;border-radius:14px;padding:24px;margin:16px 0}
.badge{font-size:13px;font-weight:650;border-radius:20px;padding:5px 10px;display:inline-block}
.pass{background:#e3f3ec;color:#176343}.warn{background:#fff0d5;color:#795000}
.callout{background:#f2f5fa;border-left:3px solid #5378b0;padding:12px 16px;margin:16px 0}
.callout p{margin:4px 0}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}
.stat{background:white;border:1px solid #dce3ee;border-radius:12px;padding:16px 24px}
.stat b{display:block;font-size:28px}nav{display:flex;gap:18px;flex-wrap:wrap}
a{color:#245bb0}details{border-top:1px solid #e1e7ef;padding:12px 0;margin-top:12px}
summary{cursor:pointer;color:#345b89}pre{font:13px/1.6 monospace;white-space:pre-wrap;overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:140px 1fr;gap:8px}dt{color:#58677b}dd{margin:0;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:10px;border-bottom:1px solid #e1e7ef}
.table-scroll{overflow-x:auto}td{overflow-wrap:anywhere}img{max-width:100%}
@media(max-width:600px){h1{font-size:24px}.card{padding:16px}dl{grid-template-columns:1fr}dd{margin-bottom:8px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.stat{padding:10px;font-size:12px}.stat b{font-size:24px}}
</style></head><body><main><h1>本地学习 · 知识与验证</h1>"""
    page += '<p class="muted">' + escaped(workspace) + " · 私人离线知识快照</p>"
    page += ("<p>这里回答三个问题：我们验证了什么？哪些只是历史线索？下一次能否直接使用？</p>"
             '<div class="stats"><div class="stat"><b>' + str(len(data["claims"]))
             + '</b>已发布知识</div><div class="stat"><b>' + str(len(data["legacy"]))
             + '</b>待复验历史</div><div class="stat"><b>'
             + str(len(data["candidates"]) + len(data["questions"])) + '</b>待处理项</div></div>'
             '<nav><a href="#verified">已验证知识</a><a href="#history">历史线索</a>'
             '<a href="#pending">下一步</a><a href="#evidence">验证证据</a></nav>'
             '<div class="callout"><strong>这不是当前 app 的实时保证。</strong>'
             '检查通过只针对记录的条件；换构建或环境后需要重新核对。'
             '页面导出后不会自动刷新，也不代替 agent 的 recall 检查。</div>')
    page += "".join(sections) + '<section id="evidence"><h2>用什么证明</h2>' + "".join(evidence_sections) \
            + '</section><footer><details><summary>给 agent 与开发者的入口</summary>' \
            + '<p><a href="catalog.json">同源 JSON 知识目录</a></p><p>知识版本：' \
            + escaped(data["revision"]) + "</p></details></footer></main>" \
            + "</body></html>"
    output.mkdir(parents=True, mode=0o700)
    (output / "catalog.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (output / "index.html").write_text(page, encoding="utf-8")
    return {"status": "EXPORTED", "html": str(output / "index.html"),
            "json": str(output / "catalog.json"), "revision": data["revision"],
            "claim_count": len(data["claims"]), "legacy_count": len(data["legacy"])}

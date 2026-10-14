# Local Learning Runtime

A client-neutral, offline first implementation of evidence-backed app learning.
It captures observations, checks explicit predicates, publishes reviewed claims,
and recalls applicable knowledge in later sessions.

**This is a working local core, not a promise that an LLM will understand every
fact or that a plugin can intercept every VS Code/Codex/OpenClaw operation.**
No model API, recurring job, private memo import, or account login is started.

## 想了解本地知识：按需生成 HTML

**仓库交付生成能力，不交付私人知识数据。** 当用户说“让我看看本地已经学到了什么”
或“生成人能读的知识页面”时，使用本仓库的本地生成器，而不是上传数据库，也不是
让模型凭聊天记忆重新编写结论。

```text
本机 runtime.sqlite + evidence/
    → catalog：读取指定 workspace 的本地记录并检查完整性
    → export-portal：确定性渲染中文知识卡片
    → 本机 index.html（人阅读）+ catalog.json（agent 读取）
```

从本仓库目录运行，替换下面的示例路径和 workspace：

```sh
python3 -m local_learning_runtime \
  --home /absolute/private/runtime-state \
  export-portal --workspace sample/android \
  --output /absolute/private/knowledge-pages/view-001
```

无需安装额外依赖，不启动模型、网络服务或 app。`--home` 必须放在子命令前；
也可以用 `LLR_HOME` 指定本地状态目录。默认状态目录为
`~/.local-learning-runtime`。

命令返回本地 HTML 路径、JSON 路径、知识 revision，以及发布知识和历史记录数量。
在浏览器打开返回的 `index.html`，即可查看：

- 已发布知识：结论、适用边界、完整性检查结果、相关证据；
- 历史线索：保留原状态，明确标为尚未重新验证；
- 待处理项：候选结论和未解决的问题；
- 证据：可读的断言节点或结果摘要，原始内容按需展开。

页面不是重新“创造”知识。人类视图与 agent JSON 来自同一份本地数据和 revision；
未知内容不补写，检查失败不包装成成功。当前渲染器不调用 LLM：已有中文记录直接
展示，受控验收模板用固定中文说明，其他任意语言的结论保留原文，不保证自动翻译。

### Agent 按需展示协议

1. 确认用户要查看的本地状态目录和 workspace；存在歧义时询问，不默选其他项目。
2. 调用 `export-portal`，将输出放在 Git 仓库之外的新目录，不覆盖旧快照。
3. 向用户返回生成的本地 HTML 链接；有浏览器能力时可打开该文件。
4. 说明这是指定 revision 的离线快照；数据变化后重新生成，不返回旧页面冒充最新结果。
5. 文件缺失、校验失败或 workspace 未登记时明确报告；没有记录就显示空状态，
   不将历史笔记自动升级为已验证知识。
6. 不提交、推送或上传数据库、HTML、JSON、截图、日志和私人知识。
   不为了“展示知识”启动云端学习或额外 app 操作。

这个协议是客户端可以采用的显式调用流程，不是已经安装的自动触发 hook。
运行时完整性检查针对记录条件，不独立证明当前设备或后端环境仍然相同。

实现入口：[knowledge.py](local_learning_runtime/knowledge.py)；
CLI 入口：[cli.py](local_learning_runtime/cli.py)；
回归测试：[test_runtime.py](tests/test_runtime.py)。

## Architecture overview

Solid connections below represent implemented local operations or explicit
caller handoffs. Dashed connections represent planned integrations, not installed
client hooks or automatic model calls. Android has a narrow real-emulator
acceptance result as well as mock tests; iOS is still mock-tested only.

```mermaid
flowchart TD
    Human["Human: question, review, approval"] --> Task["Explicit task contract<br/>workspace + platform + conditions"]
    Clients["Supported client hooks / MCP / CLI<br/>explicit installation and trust required"] --> Task
    Task --> Recall["Local runtime: deterministic recall"]
    Recall --> Gate{"Conditions, anchors,<br/>evidence and dependencies valid?"}
    Gate -->|Matching claims pass| Ready["LOCAL_READY<br/>JSON claims + conditions + revision"]
    Ready --> Consumer["Human or agent consumes scoped knowledge"]
    Gate -->|Stale or conflicted| Blocked["BLOCKED<br/>explain why reuse is unsafe"]
    Gate -->|No match| Gap["NEEDS_INVESTIGATION<br/>deduplicated local question"]
    Blocked --> Investigate["Explicit bounded investigation"]
    Gap --> Investigate
    Investigate --> Android["Android: approved journey runner<br/>UI assertions + restoration report"]
    Investigate --> iOS["iOS simulator: screenshot capture<br/>no journey runner yet"]
    Investigate --> Import["Explicit import<br/>trusted test JSON / UI XML / source anchors"]
    Investigate --> Packet["Bounded learning packet<br/>no conversation history or model requests"]
    Packet -. separately authorized, gateway planned .-> Model["Approved server model<br/>proposes scoped candidates"]
    Model -. explicit submission .-> Candidate["Candidate claim"]
    Android --> Evidence["Local content-addressed evidence"]
    iOS --> Evidence
    Import --> Evidence
    Evidence --> Candidate
    Candidate --> Verify["Registered local predicate checks<br/>JSON equality / unique UI node"]
    Verify --> Policy["Preauthorized trusted producer<br/>automatic scoped publication"]
    Policy --> Publish
    Verify --> Review["Explicit human approval<br/>checks do not prove arbitrary prose"]
    Human --> Review
    Review --> Publish["Transactional publication<br/>expected revision + claim version"]
    Publish --> DB["Private local SQLite<br/>claims + immutable snapshots + sessions"]
    DB --> Recall
    Evidence --> Recall
    Next["New session / task boundary"] --> Recall
    DB --> Portal["Offline read-only HTML + JSON catalog"]
    Legacy["Existing private memo / knowledge graph<br/>not included in this repository"] --> Intake["Approved selected-node import<br/>UNVERIFIED_LEGACY; original status retained"]
    Intake --> Portal
    Intake -. requires new evidence and explicit proposal .-> Candidate
```

### What is delivered and where knowledge lives

| Layer | Delivered now | Boundary |
|---|---|---|
| Client interface | JSON CLI, stdio MCP, Copilot lifecycle adapter, trust-reviewed Codex desktop hooks | OpenClaw and unsupported clients use explicit APIs; no universal interception |
| App observation | Android capture and bounded journey runner; iOS simulator capture | No real-device acceptance run yet; iOS navigation is not implemented |
| Verification | Exact predicates, retained observation conditions, source/evidence fingerprints | Screenshots alone and model confidence cannot verify a claim |
| Knowledge publication | Preauthorized trusted predicates publish automatically; other evidence remains candidate; version checks and invalidation | No automatic interpretation or promotion of legacy notes |
| Cross-session use | Task search, recall, refresh; supported trusted hooks inject bounded context before each prompt | Callers supply current conditions; task completeness is not inferred |
| Learning escalation | History-free, byte-bounded packet | No model gateway, spend enforcement or recurring scheduler |
| Human access | Offline static HTML knowledge cards and matching JSON catalog | Export is a snapshot, not a live or hosted service |

The GitHub repository delivers **generic software and synthetic tests**, not a
project knowledge corpus. Persistent knowledge stays under `LLR_HOME` (default
`~/.local-learning-runtime`):

```text
private runtime home/
  runtime.sqlite   # observations, candidates, claims, snapshots, sessions, questions
  evidence/        # immutable content-addressed observation blobs
```

Initialization creates this store; the demo does not populate it with real app
knowledge. Explicit graph import copies selected notes as unverified history
without modifying the original memo. Android and iOS retain independent workspace scopes.

### Evidence-to-knowledge delivery workflow

```mermaid
sequenceDiagram
    actor Human
    participant Agent as Human or agent CLI caller
    participant Runtime as Local runtime
    participant App as Authorized app / trusted test
    participant Store as Private knowledge store
    Human->>Agent: Define question, scope, permitted actions and expected result
    Agent->>Runtime: recall(task, session)
    Runtime->>Store: Read published revision; check evidence and dependencies
    Runtime-->>Agent: LOCAL_READY / BLOCKED / NEEDS_INVESTIGATION
    opt Explicitly approved investigation or revalidation
        Agent->>Runtime: Execute Android recipe or import trusted results
        Runtime->>App: Bounded actions and declared assertions
        App-->>Runtime: UI or test observations
        Runtime->>Store: Store evidence with artifact and environment conditions
        Runtime-->>Agent: Goal result and separate restoration result
        Agent->>Runtime: Propose scoped claim referencing evidence
        Runtime-->>Agent: Candidate ID and local verification result
        Human->>Agent: Review statement, evidence scope and approval
        Agent->>Runtime: accept(candidate, expected version, expected revision)
        Runtime->>Store: Atomically publish complete knowledge revision
        Runtime-->>Agent: Published revision
    end
    Agent->>Runtime: New session: recall current task and conditions
    Runtime->>Store: Recheck applicability, anchors, evidence and dependencies
    Runtime-->>Agent: Applicable knowledge, or explicit refusal to reuse
```

The investigation sequence illustrates the Android/trusted-test path. iOS
capture only records a screenshot; trusted XCTest/XCUITest predicate results
must be imported separately. A failed restoration cannot be presented as a
fully successful journey, even if a narrower goal assertion passed.

## Requirements and quick start

Python 3.9+; the runtime has no third-party dependencies. Run from this checkout:

```sh
python3 -m unittest discover -s tests -v
python3 -m examples.demo
python3 -m local_learning_runtime --home /absolute/private/state init
```

The demo uses fictional data in an automatically removed temporary directory.
It publishes a claim, recalls it across separate store instances, and proves
that changing a source anchor blocks reuse. Unit tests also cover a fresh CLI
process, publication rollback, replay deduplication, conflicts, tampered evidence,
platform separation, device locks, and failed restoration.

Optional installation: `python3 -m pip install -e .`, then use `llr` instead of
`python3 -m local_learning_runtime`.

Use an absolute `--home` before each command, or set `LLR_HOME`. The default is
`~/.local-learning-runtime`, outside the code repository. Keep state, screenshots,
logs, account data, and app artifacts out of Git, even in a private repository.
Local OS permissions/encryption remain the owner's responsibility.

## Learning lifecycle

```text
explicit question + environment
  -> deterministic recall
  -> local observation / bounded investigation
  -> proposed claim
  -> predicate checks + human review approval
  -> compare-and-swap publication in one SQLite transaction
  -> next session recall and applicability checks
```

Evidence is copied to content-addressed local blobs. Candidates and snapshots
are immutable and fingerprinted. SQLite transactions publish the complete
snapshot and current revision together; interrupted publication cannot expose
a partial revision. Failed checks cannot promote a candidate.

Every claim is scoped to one registered workspace/platform and an observed
artifact fingerprint. Conditions are exact, typed, conjunctive matches, not
semantic similarity. Source anchors are workspace-relative and checked on
publication and recall. Dependencies pin claim versions; updating or invalidating
a dependency blocks dependent claims until reviewed again.

Publication requires retaining every recorded evidence condition, not merely
the artifact fingerprint. Dropping an account, flag, or OS condition would
broaden the observation's scope and is rejected. Multiple observations can
support a claim only when their recorded conditions agree exactly.

## Register a workspace

```sh
python3 -m local_learning_runtime register sample/android \
  --root /absolute/path/to/android-checkout --platform android
python3 -m local_learning_runtime register sample/ios \
  --root /absolute/path/to/ios-checkout --platform ios
python3 -m local_learning_runtime anchor sample/android relative/source.txt
```

Registration stores paths locally; it does not scan, upload, or mutate the repo.
Android verification never grants iOS verification. A registered ID cannot be
silently rebound to another directory or platform.

## Evidence and claims

Create a local conditions file containing at least `artifact_sha256` (64
lowercase hex digits), and relevant scalar conditions such as account category,
locale, known flags, and OS version. Never store passwords or tokens.

Import a result explicitly:

```sh
python3 -m local_learning_runtime observe --workspace sample/android \
  --event unique-run-result --conditions /private/conditions.json \
  --file /private/results.json --type application/json
```

Supported evidence: JSON, Android UI XML, and PNG observations. JSON must be
produced by a trusted test or reviewed by a human; this runtime cannot prove that
an arbitrary imported JSON assertion genuinely came from an app. PNG alone
cannot pass a deterministic verification check.

A candidate specification:

```json
{
  "id": "theme-dark",
  "workspace": "sample/android",
  "task_type": "theme",
  "statement": "Under the recorded conditions, the selected theme is Dark.",
  "conditions": {
    "artifact_sha256": "<replace with real 64-character fingerprint>",
    "account_kind": "test"
  },
  "anchors": [],
  "checks": [
    {
      "kind": "json_equals",
      "evidence_id": "<returned evidence ID>",
      "pointer": "/selected_theme",
      "expected": "Dark"
    }
  ],
  "dependencies": []
}
```

Use nonempty source anchors when claiming a relationship with code. An empty
anchor list is allowed for a scoped runtime observation; it does not establish
which source revision produced the app.

`json_equals` compares a JSON pointer with an exact typed expected value.
`ui_node` requires exactly one matching node:

```json
{
  "kind": "ui_node",
  "evidence_id": "<UI evidence ID>",
  "selector": {"resource-id": "com.example.app:id/theme"},
  "attributes": {"text": "Dark", "selected": "true"}
}
```

Predicates do not mechanically prove arbitrary prose. Review the statement's
scope and relationship to its checks before approval. UI/source correlation
does not prove a specific call stack executed.

```sh
python3 -m local_learning_runtime propose /private/claim.json
python3 -m local_learning_runtime verify CANDIDATE_ID
python3 -m local_learning_runtime accept CANDIDATE_ID \
  --expected-version 0 --expected-revision none --approve
```

For later publication, use the current revision from recall and the existing
claim version (or 0 for a new claim). Stale compare-and-swap values fail instead
of silently overwriting another session. Identical reviewed publication is NOOP.

Contradictory observations do not automatically refute existing claims. Submit
and review their scope, then explicitly mark the old claim conflicted or stale:

```sh
python3 -m local_learning_runtime invalidate theme-dark \
  --lifecycle conflicted --reason "Describe the conflicting evidence" \
  --expected-version 1 --expected-revision REVISION --approve
```

Historical snapshots remain readable; invalidated claims cannot satisfy recall.

## Running Android journeys

Requires `adb`, an explicit device serial, an installed test app, and approval.
Do not use a production account. This runtime never signs in for you.

Read-only capture of an already visible app:

```sh
python3 -m local_learning_runtime capture-android \
  --workspace sample/android --device SERIAL --app com.example.app \
  --context /private/context.json --approve
```

The context file excludes `artifact_sha256`: the adapter calculates a sorted
multi-APK identity from installed APK hashes. It records API level and package,
checks the identity again afterward, captures XML/PNG, and requires the requested
package to appear in the XML. XML and screenshot are sequential observations,
not an atomic snapshot. Capture does not execute a user journey.

Approved recipe execution:

```sh
python3 -m local_learning_runtime run-android /private/recipe.json --approve
```

Recipe shape (IDs below are fictional):

```json
{
  "workspace": "sample/android",
  "device": "SERIAL",
  "app": "com.example.app",
  "context": {"account_kind": "test"},
  "steps": [
    {"action": "launch"},
    {"action": "assert_node", "resource_id": "com.example.app:id/home",
     "attributes": {"enabled": "true"}}
  ],
  "restore": [
    {"action": "assert_node", "resource_id": "com.example.app:id/home",
     "attributes": {"enabled": "true"}}
  ]
}
```

Allowed actions: `launch`, `restart` (force-stop and relaunch), `back`,
`tap_node`, `assert_node`. Tap uses a unique enabled app node's resource ID and
bounds, not model-provided shell commands. Node discovery retries at most five
times. Each command has a timeout; lists have at most 32 steps per phase.
This is a bounded runner, not an automatic explorer or a universal UI test tool.

**Review the entire recipe before approval.** A tap can alter privacy settings,
delete data, or initiate a paid request; semantic button safety cannot be inferred
from its ID. Such actions need separate user consent. No typing, arbitrary
commands, install, consent dismissal, or permission-grant operations are exposed.

Restoration runs even when goal execution fails. Tapping recipes require explicit
restoration steps. Restoration is PASS only with a successful restoration
assertion; otherwise it is NOT_VERIFIED or FAIL. Overall PARTIAL is not success.
Goal evidence remains separately usable after review, even when restoration fails.
App relaunch is not claimed to be a reboot or a full OS process-death experiment.

Device operations are mutually exclusive within one state directory. A killed
process leaves a lock; confirm it stopped before explicit recovery:

```sh
python3 -m local_learning_runtime unlock-device android:SERIAL --approve
```

Other tools/state directories are not covered by this lock. Use a dedicated
device or external exclusive runner for stronger isolation.

## iOS

`capture-ios` uses `xcrun simctl` with an explicit simulator UUID:

```sh
python3 -m local_learning_runtime capture-ios \
  --workspace sample/ios --device SIMULATOR_UUID --app com.example.app \
  --context /private/context.json --approve
```

It hashes the installed app bundle and captures a screenshot. It does not prove
the app is foreground, navigate UI, or promote the image to a verified claim.
Import JSON predicates from a trusted XCTest/XCUITest runner for verification.
Physical iOS devices and an XCUITest journey runner are not implemented yet.

## New sessions and learning gaps

Task file:

```json
{
  "workspace": "sample/android",
  "task_type": "theme",
  "conditions": {
    "artifact_sha256": "<current artifact fingerprint>",
    "account_kind": "test"
  }
}
```

```sh
python3 -m local_learning_runtime recall /private/task.json --session session-b
python3 -m local_learning_runtime refresh --session session-b
python3 -m local_learning_runtime questions
python3 -m local_learning_runtime learning-packet /private/task.json \
  --question "Which exact condition remains unverified?"
```

Recall returns LOCAL_READY only for matching published claims whose evidence,
anchors, and dependencies still pass. Any stale matching claim makes the result
BLOCKED, even when another claim passes. No match returns NEEDS_INVESTIGATION and
deduplicates a local queue item. This proves registered predicate applicability,
not complete coverage of the user's task or deterministic natural-language understanding.

`refresh` reports changed versions and anchor/evidence drift; it does not silently
switch an existing session's knowledge packet. Recall again to adopt a revision
and recheck the actual environment. Callers supply environment conditions; this
core does not independently attest account state or remote flags.

Learning packets contain one bounded question and at most four matching claim
summaries, with omission counts. They exclude conversation history and reject
packets above 12000 UTF-8 bytes. This is a byte cap, not a token or spend cap.
Use an explicitly authorized fresh model session only when needed. Review and
redact any internal data before sending it to an approved provider.

## VS Code / Codex / OpenClaw integration

### 同一 checkout，不同 VS Code session 如何找到同一份知识

知识不存放在聊天历史中。每个 session 都通过相同的 `LLR_HOME` 读取本地数据库，
用当前目录匹配已登记的 workspace，再获取同一 published revision。

从任意工作目录调用仓库自带 launcher，无需先 `cd` 到 runtime 或安装 Python 包：

```sh
python3 /absolute/path/local-learning-runtime/tools/llr.py \
  discover --cwd /absolute/path/to/project --session unique-session-id
```

`discover` 按真实目录边界匹配，优先最具体的登记根目录。相同目录对应多个平台时
返回 `AMBIGUOUS`，可用 `--platform android` 或 `--platform ios` 消歧；未登记目录
返回 `UNKNOWN_WORKSPACE`，不会因为路径字符串前缀相似就读取其他项目。

结果包含 workspace、state_home、知识 revision、发布知识 ID、任务类型及未验证历史
大纲；每类最多显示 16 个条目并报告遗漏数。它不会把“找到了知识”当成“当前条件下
可直接执行”。下一步：

桌面 App 可能打开整个 monorepo/project 根目录，而不是登记的子模块目录。
可显式登记包含原 checkout 的父目录别名，保持源码锚点根目录不变：

```sh
python3 /absolute/path/local-learning-runtime/tools/llr.py register-alias \
  --workspace sample/android --root /absolute/path/to/project-root \
  --require-platform --approve
```

父目录别名建议要求平台。`search` 只在实际问题明确且唯一提到 Android/安卓或
iOS/苹果时消歧；未提平台、同时提两个平台时不会默选。别名不能指向无关目录或
文件系统根目录，也不自动登记其他 checkout。必须在真实桌面 App 中验证：
终端 CLI 从子目录成功，不代表 App 从项目根目录也能成功。

```sh
python3 /absolute/path/local-learning-runtime/tools/llr.py \
  catalog --workspace RETURNED_WORKSPACE
```

真正复用结论仍需 `recall` 和当前条件。每次发现会记录独立 session 读取凭据，
用于比较不同 session 是否访问了同一个知识库和 revision。

可复用的个人指令模板在
[adapters/vscode-session.instructions.md](adapters/vscode-session.instructions.md)。
替换模板路径后，安装到当前客户端实际支持的用户指令位置；不要提交本机路径或
私人数据。示例 Copilot CLI 用户入口为 `~/.copilot/instructions/`，其他 VS Code
provider/profile 的位置和加载状态需单独确认。

验收必须分开：

1. **数据层**：不同工作目录中的独立 CLI 进程返回相同 workspace/revision/知识 ID。
2. **入口层**：实际新 VS Code chat 能看到用户指令，并调用 discover/catalog。
3. **使用层**：新 session 引用正确知识且保留条件与未验证状态。

进程测试不等于真实 chat 验收；指令已保存也不等于每次必然调用。本地代码无法
保证模型自觉遵守，强制执行需要客户端 hook 或受控执行网关。

All three can invoke the same CLI and consume JSON. Client configurations are
only installed with explicit approval; never silently installed or modified.

1. Explicitly normalize the task into the task schema.
2. Run recall before planning or after compaction.
3. Use LOCAL_READY claims within their declared scope.
4. Stop controlled execution on BLOCKED; investigate NEEDS_INVESTIGATION.
5. For an authorized new question, pass only the learning packet to a fresh session.
6. Submit structured candidates, verify, review, and publish.
7. Refresh at task boundaries.

Do not let a model-provided summary or confidence score bypass checks. Plain
instructions/MCP availability cannot guarantee invocation. Global enforcement
requires a client-owned execution gateway or isolated worktree plus acceptance
gate; neither is supplied by this CLI. Source code in a trusted local process can
alter the database, so these checks are not an adversarial sandbox.

## Current boundaries

- No autonomous learning scheduler, model calls, spend gateway, or automatic
  authentication. Model budget remains paused unless separately approved.
- No semantic retrieval, complete dependency discovery, automatic source tracing,
  or dedicated VS Code extension. MCP retrieval and a preapproved validator
  registry are implemented; they do not authorize arbitrary commands.
- No automatic promotion or modification of existing private memo stores.
- No global guarantee that a server-hosted chat uses zero model calls; only this
  runtime's local operations make zero model requests.
- Android launch, capture, restart assertions and cross-process knowledge reuse
  have passed one authorized real emulator acceptance run. That narrow result
  does not validate every Android feature, device, account, or iOS adapter.

## Automatic task milestones (v0.2)

The normal supported path does **not** require a follow-up "save knowledge"
request. Use a previously approved runtime validator to do the task; its result
is minimized, mechanically checked, persisted and indexed as part of execution.

```mermaid
flowchart LR
    P["New task"] --> H["Trusted prompt hook<br/>bounded local search"]
    H --> A["Agent: approved runtime validator"]
    A --> E["Actual tested values + runtime identity"]
    E --> V{"Producer trusted,<br/>predicates pass,<br/>no conflict?"}
    V -->|yes, preauthorized| D["Publish local claim + revision"]
    V -->|no| C["Candidate / explicit failure reason"]
    D --> G["Graph projection from same database"]
    D --> N["Next session prompt hook"]
```

### Preauthorization, once per workspace and runner

```sh
python3 -m local_learning_runtime authorize-local --workspace sample/android --approve
python3 -m local_learning_runtime register-runner example /private/runner.json \
  --workspace sample/android --approve
```

An approved runner has an absolute executable, fixed argv, conditions, explicit
JSON pointer predicates, source anchors and a task title. Executable and file
arguments are fingerprinted. A changed executable or file is rejected before
running. Approval authorizes that exact runtime action, **not** new commands,
App purchases, consent changes, extra agents or model calls.

```json
{
  "argv": ["/absolute/python3", "/private/approved-runtime-check.py"],
  "title": "Example panel visible after launch",
  "conditions": {"artifact_sha256": "<recorded fingerprint>"},
  "conditions_pointer": "/conditions",
  "checks": [{"pointer": "/visible", "expected": true}],
  "anchors": []
}
```

`conditions_pointer` is optional. When supplied, the trusted producer returns
the actual installed artifact/environment identity, instead of reusing a static
registration-time identity. Missing or changed conditions never establish
compatibility with historical knowledge.

Clients discover `local_learning_validators`, then invoke
`local_learning_validate` with task/session/milestone identifiers. The tool
cannot register a new runner or broaden its scope. Direct CLI equivalent:

```sh
python3 -m local_learning_runtime run-validator example --workspace sample/android \
  --session session-a --task task-one --milestone checkpoint-one
```

Preauthorized `run-android` journeys also produce automatic milestones; pass
`--session` and `--task` to associate them with the current client task.
The generic `probe-android-node` producer supports fixed, approved launch and
unique/enabled-node checks; project/package/node values belong in private runner
configuration, never in the product implementation.

### Outcome, authority and retry semantics

Session, task and milestone are separate IDs. One session can have many tasks,
each with several checkpoints. Schema:
[milestone.v1.schema.json](schemas/milestone.v1.schema.json).

- Trusted runtime producers + passing scoped predicates + local preauthorization:
  automatic publication, no per-observation approval click.
- External structured events, code inference, user evidence, screenshots alone
  and hypotheses: durable candidates, not automatically verified facts.
- FAIL, PARTIAL, INCONCLUSIVE or a missing predicate: no success promotion.
- Sensitive predicate values changed by redaction: publication refused.
- Explicit contradictions/supersessions: candidate pending human reconciliation;
  related old knowledge is blocked from automatic search reuse.
- Assistant "done" text and tool transport success: never runtime success.
- Same task/milestone payload: idempotent. Changed payload: reject; use a new
  milestone rather than overwrite prior evidence.
- A publication failure leaves a SAVE_FAILED record and explicit reason;
  the same event can be retried after the error is fixed.
- Trusted eligible events have a private local integrity attestation. A repeated
  validator invocation reuses its existing result; `retry-milestone EVENT_ID`
  retries publication from stored evidence without re-executing the App.
  Concurrent invocation IDs are reserved before execution. An interrupted
  invocation with unknown outcome is reported, not silently rerun.
- SQLite publication is transactional. Candidate collection and publication
  are separately observable phases; a collected file is not a published claim.

Memo, conditions, operations and evidence relations are projected from the same
authoritative database. `graph` does not maintain another independently editable
fact store. Supported links include supports, contradicts, supersedes and
depends_on; relations require existing same-workspace observations.

```sh
python3 -m local_learning_runtime task-status
python3 -m local_learning_runtime graph --workspace sample/android
python3 -m local_learning_runtime search --cwd /project \
  --query "actual business question" --session session-b
```

Search returns at most four summaries within 8000 UTF-8 bytes. No supplied current
conditions means CONDITIONS_UNKNOWN, not unconditional reuse. It includes
provenance, evidence IDs and revision. Full evidence is expanded only as needed.
Runtime is the published-observation authority; Userbot is supplemental legacy
background. Reading only Userbot cannot count as reading newly published runtime
knowledge.

### Installed client adapters and trigger guarantees

```sh
python3 -m local_learning_runtime install-client copilot \
  --runtime-root /absolute/local-learning-runtime --profile ~/.copilot --approve
python3 -m local_learning_runtime install-client codex \
  --runtime-root /absolute/local-learning-runtime --profile ~/.codex --approve
python3 -m local_learning_runtime install-client codex-hooks \
  --runtime-root /absolute/local-learning-runtime --profile ~/.codex --approve
```

| Client path | Actual integration | Guarantee boundary |
|---|---|---|
| VS Code Copilot Agent Host / Copilot CLI | User lifecycle hooks + MCP search/approved validation | Verify the selected harness loads hooks; other VS Code providers have different schemas |
| Codex desktop with supported hooks | UserPromptSubmit injects bounded relevant knowledge; PostToolUse records minimal checkpoints; Stop reports status | New hooks require actual client trust review; never bypass trust |
| Older Codex CLI without hooks | MCP plus user instruction; chained existing notify preserved | Prompt retrieval selection is advisory; notify alone does not capture all validation output |
| Other clients | CLI/MCP and structured milestone API | No lifecycle guarantee until an adapter is installed and actually verified |

The supported hook path injects relevant knowledge before model reasoning,
rather than hoping the model chooses a recall command. Hooks can be disabled,
untrusted, or fail due to environment problems; these are explicit unsupported/
failure states, not successful recall. An LLM can still misinterpret evidence:
controlled execution and predicate checks enforce behavior, not universal
semantic correctness of every answer.

Ordinary PostToolUse capture keeps minimal tool status and at most 16 bounded
debugger assignments, not whole conversations or arbitrary raw output.
They remain candidates because arbitrary stdout is not trusted attestation.
Stop/notify never infer success from the assistant's last message, do not launch
experiments, and do not retry a model in a loop.

### Privacy, durable evidence and deletion

Automatic producers store only values actually used by registered predicates,
not unrelated stdout fields. Known token, receipt, signature, email, order,
device and account identifiers are redacted with private keyed markers; distinct
private conditions remain distinct. Evidence must retain its tested value:
if a predicate relies on a redacted field, it cannot be silently marked PASS.
This deterministic sanitizer is not a universal PII detector. Unstructured
text and new sensitive formats need reviewed minimization, not auto-promotion.

Historical debugger files can be imported as line-located assignments:

```sh
python3 -m local_learning_runtime import-debugger /private/old-debugger.log \
  --workspace sample/android --session source-session --task source-task \
  --title "Scoped historical observation" --approve
```

Only necessary assignments and provenance are retained locally; a historical
log, unknown artifact or old screenshot is not current runtime verification.
Runtime evidence is copied into private content-addressed blobs, independent
of temporary session files.

```sh
python3 -m local_learning_runtime forget-workspace --workspace sample/android --approve
```

Deletion revokes associated historical snapshots and removes authoritative
memo/graph/evidence. Separately exported HTML/JSON, original imports and OS
backups remain owner-managed. Deletion does not revoke runner authorization.
Generated portal JSON includes task-level states and the graph projection.

The runtime makes zero model requests and never starts recurring learning.
Existing client model use remains subject to user/provider authorization and
billing; this software does not implement a provider spend gateway.

### Actual closed-loop acceptance

On 2026-10-14, a new real VS Code Copilot task executed an approved Android
runtime validator. No "save knowledge" instruction was appended: the validation
tool automatically captured, checked and published a new object-specific claim.
A completely new **Codex desktop App** conversation then received that claim
from a trusted UserPromptSubmit hook, with no knowledge IDs, paths or recall
commands in the business question. It cited the exact runtime object,
conditions and evidence, while retaining CONDITIONS_UNKNOWN for the current
environment. The consumer made zero explicit retrieval tool calls: injection
was driven by the client lifecycle hook, not model selection.

This acceptance also caught and fixed parent-project directory discovery,
invalid milestone rejection before App actions, and insufficient object identity
in result summaries. CLI sessions and desktop sessions are separate acceptance
surfaces; their results are not interchangeable. The private report and raw
evidence stay on the local machine. The repository contains only generic code,
schemas, adapters, documentation and synthetic regression tests.

The guarantee applies to installed/trusted supported hooks and preauthorized
validator paths. Arbitrary tools, disabled hooks and unregistered workspaces
remain explicit gaps, not universal success.

## Selected legacy knowledge intake

```sh
python3 -m local_learning_runtime import-graph /private/graph.json \
  --workspace sample/android --node J05 --node F06 --approve
python3 -m local_learning_runtime catalog --workspace sample/android
```

The input is a JSON object with a `nodes` list; nodes have unique IDs. At most
16 selected nodes are copied per call, each at most 12000 UTF-8 bytes. Source
fingerprint, scope, recorded source revision, original status, and full selected
node body are retained. Repeated imports of the same selection/revision deduplicate.
Import never creates an active claim or a passing verification; a historical
`partial` or `observed` node stays `UNVERIFIED_LEGACY` in the new runtime.
New runtime evidence, predicate checks, a scoped proposal, and approval are
required for publication.

## Human knowledge portal and agent catalog

```sh
python3 -m local_learning_runtime export-portal --workspace sample/android \
  --output /private/portal/revision-1
```

Open the returned `index.html` locally in a browser. Its sibling `catalog.json`
contains the same claims, revision, unverified legacy intake, unpublished
candidates, and investigation queue for agent consumption. Published claims
include local integrity status. Conditions remain recorded conditions, not an
attestation of the current device. Referenced evidence is embedded in the page
after fingerprint checking, with a 32 MiB total evidence cap.

The human view uses Chinese knowledge cards: conclusions, reuse limits,
historical gaps and next actions appear first. Conditions, IDs and raw evidence
are collapsed by default. Evidence links show readable labels and relevant UI
nodes rather than a full XML dump. The agent JSON retains the original schema.
Only the exact registered restart-acceptance statement receives a deterministic
Chinese explanation; other statements are preserved rather than guessed or
translated by a model.

The portal is static, read-only, offline, escaped HTML with no scripts or remote
assets. Choose a fresh output directory for each export; existing files are not
overwritten. Re-export after publication or source/evidence drift. The HTML and
JSON can contain private data: do not commit, host publicly, or upload them.
Agent execution must still use `recall`; the catalog is not an execution permit.

## Reproducible real Android acceptance

```sh
python3 -m local_learning_runtime acceptance-android \
  --workspace sample/android --device SERIAL --app com.example.app \
  --resource-id com.example.app:id/navigation \
  --context /private/context.json --output /private/acceptance/run-1 --approve
```

First inspect the installed test app and choose one unique stable enabled UI
node. This command intentionally launches and force-stops/relaunches that app,
but does not tap buttons, send messages, log in, or change privacy settings.
Use a dedicated approved emulator/account; relaunching a production app can
still affect an in-progress user task.

The command:

1. Runs launch -> node assertion -> restart -> node assertion.
2. Separately verifies the same non-mutated observable UI postcondition.
   It does not restore unobserved draft or backend state.
3. Stores real UI XML and the goal/restoration report with installed artifact identity.
4. Publishes only the narrow declared UI observation after approval and checks.
5. Starts an independent CLI process to recall that claim.
6. Starts another process with a changed artifact and requires BLOCKED.
7. Proposes a deliberately false predicate, requires rejection, and checks that
   the published revision did not change.
8. Exports the offline human portal and JSON catalog, plus `acceptance.json`.

This is an actual device runner, not a mocked demo. The local output includes
the recipe, journey, task, changed task, acceptance report and portal. Evidence
remains in the private runtime store. A failed/partial journey stops publication
and leaves its report for investigation. Claims do not assert source execution
or authenticated account identity.

An authorized API35 emulator acceptance on 2026-10-10 passed all eight steps.
Real execution exposed two issues missed by mocks: `monkey` launch failed on
that emulator, and randomized APK paths used `~~`. The adapter now resolves and
starts the launcher via Activity Manager and permits valid randomized paths;
both have regression tests. Private app details and evidence are not in this repo.

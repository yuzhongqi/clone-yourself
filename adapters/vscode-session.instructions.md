---
description: "Use when a new local VS Code session needs to find project knowledge, verified app observations or legacy notes for the current checkout."
---

# Shared local knowledge discovery

Template: install as a personal instruction in the active VS Code profile.
Replace `/ABSOLUTE/PATH/clone-yourself/tools/llr.py` with the checkout
path. Do not install this template unchanged. Keep private state outside Git.

For a local private project task, including after context recovery:

1. Run `python3 /ABSOLUTE/PATH/clone-yourself/tools/llr.py discover --cwd "<actual checkout directory>" --session "<unique session ID>"`.
2. Read the result. DISCOVERED means the directory maps to a registered workspace,
   not that every claim applies to the current app. UNKNOWN_WORKSPACE or AMBIGUOUS
   must be reported, not replaced with another project's knowledge.
3. Use the returned workspace with `catalog` for knowledge, and `recall` with
   actual task conditions before treating claims as currently applicable.
4. Do not turn UNVERIFIED_LEGACY into verified facts. Consult source evidence for
   implementation decisions. Do not launch an app, model learning, or upload data
   merely to discover knowledge.
5. Report the workspace, state home, revision, and referenced knowledge IDs so
   a human can compare results across sessions.

This instruction guides client behavior; it is not a deterministic lifecycle
hook or a guarantee that every model/profile will invoke the command.

For supported clients, prefer installed runtime hooks and the local MCP
`local_learning_search` tool. `local_learning_validators` lists already approved
runtime actions; `local_learning_validate` executes one approved validator and
automatically persists, verifies and publishes its scoped predicates. An extra
"save knowledge" user instruction is not required. Never register or widen an
App validator through an agent without explicit user approval.

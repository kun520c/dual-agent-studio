# Architecture and authority

The [README diagram](../README.md#architecture) represents the current code. `desktop-validation/run.py` starts PySide6 / Qt WebEngine with locally built assets. Restricted QWebChannel routes explicit RPC methods through Python Bridge, HostClient local IPC and TaskHost. ReadOnlyBridge loads history without creating a host.

TaskPolicy validates a closed submission DTO and project policy. TaskScheduler enforces concurrency/resource locks. Coordinator prepares an isolated Git Worktree when configured, invokes the fixed executor flow, checks allowed changes and runs independently registered verification commands. Final acceptance requires a legal accept decision plus fresh successful command/snapshot evidence. There is no model-controlled `verified` shortcut.

## Backend Manager

ManagerLoop calls `generate_plan`, `review_result`, and `propose_replan` on its provider. Proposals enter domain schema, DAG, identity, scope and revision validation. GoalLedger persists immutable plans, work items, Attempts, decisions, events and call accounting in SQLite WAL. GoalHost maps domain work to the existing host/scheduler/coordinator; the Coordinator evidence source binds observations to current Attempts. A file saying `verified=true` is insufficient. The Manager has no new GUI RPC.

## API-only Manager versus CLI Manager

| Path | Implementation | Authorization | Current evidence |
| --- | --- | --- | --- |
| API | `api_profiles`, `api_protocol`, `api_http`, `api_authorization`, `api_transport` | v3 `manager_call`; backend kind / provider / endpoint / model / protocol / data scope / profile hash | B2.2-A FakeHTTP and scripted TLS/socket tests; B2.2-B live integration not run |
| CLI Manager | `authorization.py`, `transport.py`, controlled Windows processes | v2 Manager authority; fixed executable/profile hash and call budget | Offline reference CLI/Fake runner; real Codex/Cursor Manager profiles disabled |
| Executor | `execution_gate.py`, Coordinator adapters | v1 execution authority; workspace / files / budget | Offline validation plus separately documented historical real runs |

OpenAI and DeepSeek have separate fixed profiles and request field allowlists. Both use Responses parsing and the same domain layer. The API transport offers no tools, sets `tool_choice=none`, rejects unexpected tool output, fixes HTTPS endpoints, validates certificates/hosts, rejects redirects and does not retry. Public source has no production signer or deployment credential.

Call identity, start intent, response/request hashes, usage source, unknown billing and reconciliation state are persisted. A sent request with an unknown result reserves its budget and is not resent automatically. Cancellation fences reject late plan/decision effects. This does not guarantee exactly-once provider billing.

## Boundaries

Manager permission and Executor permission are independent. Worktree/write scope/process Jobs are collaboration/lifecycle controls, not OS sandboxes. Production multi-task inheritance remains blocked; offline inheritance is limited to a single-parent chain, same baseline, tracked ordinary text and sufficient downstream scope. Cross-restart trusted proof and automatic model-session handoff remain incomplete.

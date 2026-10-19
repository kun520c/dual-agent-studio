# Security

This development preview has no production trust roots, API credentials or reusable execution grants. Public defaults carry no author-specific path authorization. Basic Mock rehearsal runs only the fixed application-owned sample and trusted script.

## Trust boundaries

- Registration is not execution authority. Real Executor runs require independent v1 signed permission; API and CLI Managers use their own v3/v2 purposes, deployment binding and accounting.
- A model's self-report cannot set `verified`. Coordinator-run commands, allowed changes, current snapshots and legal acceptance must agree.
- Worktrees and `write_paths` are not OS sandboxes. Windows Jobs constrain process lifetime, not filesystem access.
- Offline FakeHTTP/Fake CLI success does not prove live model quality, safe production operation or real repair.
- Time/call budgets are not monetary hard caps. Unknown sent outcomes are not automatically retried; billing may remain unknown.
- Context redaction is not general DLP. Review project material before any specifically authorized live use.

## Public content policy

No private keys, real signed authorizations, usage ledgers, raw model messages, SQLite state, original E2E evidence/logs, internal handoff ZIPs, user Worktrees, portable packages or unredacted PROJECT_MASTER are published. Test code generates temporary offline-only keys; generated files remain under ignored test output and are not CI artifacts.

## Reporting

Use the repository's **Security → Report a vulnerability** option if GitHub private vulnerability reporting is available. If unavailable, open an issue containing only a request for a private contact channel; do not post credentials, exploit data, private code or raw runs publicly. No dedicated security email or response SLA is currently published. Do not submit production secrets in bug reports.

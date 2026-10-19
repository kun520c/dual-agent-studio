# Development status — public source preview

Snapshot: 2026-10-19, GUI v0.14.1 / backend v0.15 B2.2-A. This is a sanitized publication summary, not a copy of the internal PROJECT_MASTER and not an installed app upgrade.

## Historical engineering baseline

Phase A established GoalLedger; B1 integrated GoalHost with the existing host. B2 added offline Manager planning, dispatch, task repair and replan. B2.1 added independent CLI Manager authority and controlled process lifecycle, while real provider profiles remained disabled. **B2.2-A completed its offline engineering acceptance** with the five API modules, v3 authority, Responses parsing, unknown-result audit and an original-host Python source fixture scenario.

The internal B2.2-A baseline reported **59/59 new offline API tests**, Python **485 passed / 1 explicitly skipped** after deduplication, frontend **34/34**, and a successful TypeScript check. These are historical acceptance results, not this public build's new test result, not real model calls, and not original logs included in this repository.

## Public verification

The public runner retains the 59 API safety tests and the compatible core / host / Manager / desktop suites. One private historical-run usage test is omitted because its original raw messages, run artifacts and evidence manifest are not public. Self-contained compaction and live-process-output tests remain. Author-specific default path authorization is removed and its default-deny assertion is updated. Authorization-display tests use disposable temp state. These export-only adaptations are documented in [public export](public-export.md).

See [public validation results](validation.md) for checks actually rerun on this public copy. Local results and GitHub Actions results are reported separately. CI does not call installed agents or paid services, does not create production licenses and does not upload fixture artifacts.

## Not yet accepted

- B2.2-B live API integration, account/provider availability, physical-network failure modes and real billing: not executed.
- Real Codex/Cursor Manager no-tool isolation / native protocol: unconfirmed; profiles disabled.
- Real automatic repair: not directly accepted. Historical first-pass success with zero repair is not repair proof.
- Production multi-task result inheritance, multiple parents, general add/delete/binary inheritance: blocked.
- Automatic cross-session handoff / provider session reconnection: planned.
- Fresh Windows deployment, native GUI long-run reliability and old binary packages: not revalidated for this source publication.

Manager orchestration is backend-only. The GUI remains v0.14.1; do not describe this repository as a stable v0.15 desktop release.

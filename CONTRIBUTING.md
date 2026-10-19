# Contributing

This source preview has no selected project license. Discuss proposed contributions in an issue before submitting code; there is no contributor license agreement or automatic grant of project reuse rights.

Use the [source setup and protected offline checks](docs/getting-started.md). Keep each change reviewable and describe its behavior, validation and limits. Tests must use temporary fixtures and fixed Fake providers. Do not invoke paid services, installed Codex/Cursor or production credentials in tests/CI. Do not upload .test-output, raw model messages, grant ledgers, keys or private projects.

Preserve fail-closed authorization, independent verification, cancellation/recovery and budgets. Do not weaken `verified` or silently skip security failures. Changes to API profiles, protocols or authority need focused safety tests. Update the public status summary when capabilities change, while the private development PROJECT_MASTER remains the internal authority. Maintainers review the export and publish deliberately; local changes are not automatically synchronized.

# Public source validation

Validated locally on 2026-10-19 (Windows 11, Python 3.13.5, Node.js 24.15.0, Qt/PySide6 6.12.0). Fresh dependencies were installed inside the independent public checkout from PyPI and npm. Node.js 22.12+ is the declared build requirement; hosted Windows CI uses Node 22. This does not constitute deployment acceptance on a second Windows machine.

| Protected Python group | Collected | Passed | Failed/errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| api | 59 | 59 | 0 | 0 |
| manager-cli | 39 | 39 | 0 | 0 |
| manager | 53 | 53 | 0 | 0 |
| ledger | 45 | 45 | 0 | 0 |
| host | 33 | 33 | 0 | 0 |
| core | 182 | 179 | 0 | 3 |
| desktop | 119 | 119 | 0 | 0 |
| rehearsal | 6 | 6 | 0 | 0 |

**491 unique Python tests: 488 passed, 3 skipped, 0 failures/errors in the final selected per-group results.** Groups overlap on 45 GoalLedger test IDs. The 59 API tests are all offline engineering tests; none is a real model test.

Skipped: two private historical Codex/Cursor raw-probe replays are unavailable by design; one installed Codex help query is excluded by the zero-agent scope. One additional private historical usage test is omitted from the public source. No migration/authorization safety test is disabled to obtain these results.

These are final per-group runs, not one uninterrupted all invocation. The initial combined run did not finish; the first grouped run revealed missing public-only config and legacy migration fixtures. The export now includes synthetic safe config/sample files and 18 exact legacy Python source fixtures, and affected groups were rerun in full. Original product runtime code was not changed to fix these export dependencies. Original private logs remain private.

| Other check | Actual result |
| --- | --- |
| TypeScript | exit 0 |
| Vite 6.4.4 production frontend build | exit 0 |
| Node frontend tests | 34 / 34 passed |
| npm audit after public-only Vite update | 0 findings |
| Read-only Qt/WebEngine smoke with independent state | exit 0; no host/submission, JS errors or renderer crashes |
| Export boundary tests (dry-run, conflict, traversal, Junction, stale entries) | 11 / 11 passed |
| Exact-file/hash, extensions/size, local Markdown links/anchors and PNG metadata | passed |
| Gitleaks and independent detect-secrets review | no real secret found; fixture/hash false positives reviewed |

Scans complement the explicit allowlist, source/runtime separation, staged/history checks and manual screenshot/license review. They do not mathematically prove the absence of secrets. Test artifacts, ephemeral keys, raw logs and E2E evidence are not tracked or uploaded as CI artifacts.

The [Windows workflow](../.github/workflows/windows.yml) performs fresh dependency installation, frontend checks/build/tests, separate-interpreter offline groups and public file checks. Its actual hosted result is visible in [GitHub Actions](https://github.com/kun520c/dual-agent-studio/actions); this local table does not claim a hosted CI result.

Live Manager B2.2-B, real Codex/Cursor, real automatic repair, native GUI soak, Java/Maven/JUnit, old binary packages, physical provider networking and fresh Windows deployment were not executed/retested for this publication.

## Hosted CI follow-up — 2026-10-20

The first hosted run was cancelled while the serial Python step was still active; it is not a passing CI result. The workflow now runs the same eight protected groups as separate Windows matrix jobs with independent timeouts and visible test-ID progress. A separate job checks TypeScript, builds the frontend, runs all Node tests and checks public file hashes/links. No authorization, migration or API safety test is removed. Generated keys, grants and test outputs are not uploaded as artifacts.

The local results above remain the actual dated per-group baseline. Check GitHub Actions for the current hosted conclusion; no green badge is asserted here.

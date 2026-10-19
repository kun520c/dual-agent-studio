# Getting started

Use a local Windows 10/11 NTFS directory with permission to create Git worktrees and Junctions. The supported validation baseline is Python 3.13, Node.js 22.12+ and Git. Core code targets Python 3.11+, but this public preview is tested on 3.13. The GUI language is Chinese.

Run the clone / venv / pip / npm commands in the [README](../README.md#getting-started-windows). There is no prebuilt frontend or embedded Python. `requirements.txt` pins the GUI runtime; `design-prototype/package-lock.json` pins frontend dependencies. Use public PyPI and npm sources. Do not copy the author's original virtual environment or `node_modules`.

## Offline desktop rehearsal

```powershell
.\.venv\Scripts\python.exe -B desktop-validation/run.py --state .test-output/gui-state
```

Open 项目管理, choose 创建安全演练, then 新建任务. Keep Mock, review the preflight, and submit. Only the fixed application-owned rehearsal sample is authorized for this path. Registration of another project does not grant execution permission. The rehearsal creates local project/run/sample files in this clone; these paths are ignored by Git. No model installation, API credential or production key is needed.

For read-only inspection:

```powershell
.\.venv\Scripts\python.exe -B desktop-validation/run.py --read-only --state .test-output/readonly-state --measure .test-output/gui-smoke.json --auto-close 2
```

Read-only mode creates no TaskHost. It still writes isolated Chromium state and measurement output. No original task history is distributed, so a clean clone initially has an empty history.

## Checks

```powershell
npm.cmd run check --prefix design-prototype
npm.cmd run build --prefix design-prototype
npm.cmd test --prefix design-prototype
.\.venv\Scripts\python.exe -B tools/run_offline_checks.py --suite all
```

The protected Python runner rejects installed agent processes and Python socket connections; its selected child fixtures are fixed offline scripts. It is not an OS network sandbox. `--suite api` runs the 59 B2.2-A tests; `--suite core` runs the core suite; `--suite desktop` runs the self-contained desktop group. The installed Codex help test is explicitly skipped. Two historical raw-probe replay tests skip when their private source recordings are absent. This is narrower than every historical internal test: private-history and Java/Maven deployment tests are excluded rather than supplied with private data. Offline tests create disposable RSA fixture keys and grants only under `.test-output`; they are not production licenses and must never be published.

Logs and test fixture artifacts remain local under `.test-output`. Windows CI uses the same protected runner. Run the frontend build before Python desktop tests, because `LocalAssets` checks the generated asset map.

## Current build limits

The frontend imports dependencies through the retained `design-prototype/node_modules` path. This is deliberate compatibility, not a requirement to run the old prototype. Portable installer generation is not part of this source preview; old internal packaging/finalization scripts are excluded. Full fresh-machine, native long-run and live model verification are pending. See [validation](development-status.md) and [FAQ](faq.md).

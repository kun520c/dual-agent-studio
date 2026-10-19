# Controlled public export

The original development root is not a Git repository. This repository is a separate, reviewed source publication. The internal `docs/PROJECT_MASTER.md` remains the sole internal long-term status source; it is not exported. This public summary does not write changes back to the development root.

`PUBLIC_SOURCE_MANIFEST.json` records every exported file's relative origin, size and SHA-256. The export tool is [tools/export_public_source.py](../tools/export_public_source.py); its internal release kit supplies an explicitly reviewed per-file allowlist and public documentation overlays. The allowlist is not expanded by the exporter. Use the internal kit against the internal development root, not this public clone as if it were a complete internal checkout.

```powershell
python export_public_source.py --source <development-root> --dest <separate-public-checkout> --allowlist <reviewed-allowlist.json> --dry-run
python export_public_source.py --source <development-root> --dest <separate-public-checkout> --allowlist <reviewed-allowlist.json>
```

Export refuses overlapping roots, reparse points/symlinks, path traversal, unknown extensions, oversized files and conflicts with locally edited destination content. It never follows Worktrees or copies historical .git. It does not delete stale files; a removed allowlist entry requires explicit review. `.public-export-state.json` is local hash state, not uploaded. Use a stable source snapshot and a final source hash check before staging; this is controlled tooling, not an OS sandbox or concurrent adversary boundary.

## Export-only adaptations

- Remove the author's historical absolute-path read-only grant; the public default is empty. Keep synthetic-path safety coverage and update the default-deny test.
- Replace personal paths in a few tests with synthetic examples while preserving forbidden security markers.
- Move authorization-display fixture temp state out of original measurement paths.
- Keep exact legacy ledger v1/v2 Python source as reviewed migration fixtures; migration tests read those source directories instead of private handoff ZIPs. No migration safety test is skipped.
- Exclude one private historical-run usage test; retain self-contained usage tests. Route GoalHost/Manager evidence into ignored local test output through the protected runner.
- Preserve frontend import paths but publish only dependency declarations/lockfile; provide working scripts and update the stale HTML title.
- Update Vite from the internal 6.3.5 to 6.4.4 in the public dependency overlay after npm audit identified development-server vulnerabilities. Original development dependencies are unchanged.
- Omit old portable build/finalize/signing scripts, raw logs, E2E artifacts, production authorizations/credentials, call audits, databases, handoff ZIPs, Worktrees and internal reports.

Future release sequence: finish development → update internal PROJECT_MASTER → test → review allowlist/overlays → dry-run export → inspect diff/conflicts → export → rerun public tests → scan all files/staged blobs/history → review screenshots/licenses/links → commit and deliberately publish. Never force-push unknown remote history or automatically publish every local edit.

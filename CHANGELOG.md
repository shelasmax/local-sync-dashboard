# Changelog

## v1.3.0 - 2026-04-15

### Diagnostics-to-ops and operational screen hardening

- Linked profile diagnostics more tightly with `/ops`: `/profiles` now shows profile counters, contextual helper blocks, and direct runbook links for missing remote, auth/rights, local path, and Synology mount-path scenarios.
- Hardened operational surfaces on `/runs`: active, recovery, and incident sections now stay visible as dedicated surfaces and render explicit empty-state guidance when there is no live runtime snapshot.
- Fixed persisted `Скрыть` / `Показать` controls so they actually hide and restore content on `/`, `/jobs`, and `/runs`, while keeping the browser-stored expanded/collapsed state.
- Expanded run incidents on `/runs` to include interrupted history alongside failed/blocked runs, so the operational screen still shows actionable context even when there are no active snapshots.
- Added defensive fallback in `/profiles`: setup diagnostics failures no longer take the whole page down with `500`, and the page can still render with an empty remote set.

## v1.2.1 - 2026-04-15

### Archive UX and compact operational surfaces

- Split run UX into two layers: `/runs` is now the operational screen for active, interrupted, and incident-driven actions, while `/runs/archive` holds the full run history with pagination and filters.
- Added manual run-history cleanup: single-run delete and bulk archive cleanup remove only local `RunHistory` rows and log files, without touching source or target data.
- Fixed routing regression for `/runs/archive` by registering the archive route before `/runs/{run_id}` so the archive no longer falls through into the run-detail integer parser.
- Made recovery and incident surfaces more compact: top recovery blocks on `/jobs`, `/runs`, and `/` now show only the latest interrupted run where appropriate and expose explicit `Скрыть` / `Показать` toggles with persisted browser state.
- Restored active-run visibility on `/runs` by building the active section from live runtime snapshots instead of relying only on database rows with `running` status.

## v1.2.0 - 2026-04-15

### Resilience: orphan-run detection and process cleanup

- **Orphan-run reaper**: periodic APScheduler job (`_reap_orphan_runs`) runs every 5 minutes and marks DB rows stuck in `running` status as `interrupted` when no active thread owns them. Also cleans stale `var/runtime/*.json` snapshot files. Previously, a dead rclone process could leave a run as `running` forever — only recovered on uvicorn restart.
- **SIGKILL grace period**: `_stream_process` now sends `SIGTERM`, waits 5 seconds, then sends `SIGKILL` if the process refuses to die (e.g. D-state from stale NFS/SMB mount). Prevents zombie rclone processes from blocking the job slot indefinitely.
- **Broad exception handling in `_execute_job`**: unexpected exceptions (OSError, selector errors, etc.) now mark the run as `FAILED` in the database instead of silently leaving it as `running`. Added outer `try/except` with `_mark_run_failed()` as a safety net.
- **Safe I/O in stream loop**: `readline()` and `stream.read()` wrapped in `try/except OSError/ValueError` to handle half-closed file descriptors. Selector cleanup moved to `finally` block to prevent descriptor leaks.

## v1.1.1 - 2026-04-15

- Fixed UI blocking during active rclone jobs: replaced `window.location.reload()` with AJAX polling to `/api/runtime/jobs` on `/jobs`, `/runs`, `/run_detail` pages. Full page reload now only triggers when a job transitions from active to finished.
- Added 30-second TTL cache for `collect_setup_diagnostics()` to avoid repeated `rclone listremotes` subprocess calls and `/Volumes` filesystem scans on every HTML page render.

## v1.1.0 - 2026-04-15

- Stabilization release for `Control Room` UI and route safety.
- Added edit/clone flows for jobs from `/jobs` and `/runs`.
- Added route preview and folder browse helpers in the job form.
- Added Synology-specific validation for `/Volumes/...` paths and duplicated `source_path`.
- Improved repeated-failure and recovery UX across dashboard, jobs, runs, and run detail.
- Simplified shell layout: lower header, smaller hero blocks, unified card rhythm across `/`, `/jobs`, `/runs`, `/profiles`, `/setup`, and `/ops`.
- Moved operational readiness from dedicated navigation into the main dashboard.

## Unreleased history before tags

- Earlier application states are preserved in Git history on GitHub.
- No release tags existed before `v1.1.0`.

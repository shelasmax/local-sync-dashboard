# Changelog

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

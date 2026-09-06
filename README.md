<div align="center">

<img src="app/static/favicon.svg" width="72" height="72" alt="Local Sync Dashboard logo" />

# Local Sync Dashboard

**Your files. Your Mac. One control room.**

Plan, run, and monitor one-way file copies between S3, Synology, Yandex Disk, and local folders — with a local web interface powered by rclone.

[![Release](https://img.shields.io/github/v/release/shelasmax/local-sync-dashboard?color=2f6f63)](https://github.com/shelasmax/local-sync-dashboard/releases/latest)
![macOS](https://img.shields.io/badge/platform-macOS-9f5a35)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-2f6f63)
![rclone copy](https://img.shields.io/badge/transfer-rclone%20copy-9f5a35)

**English** · [Русский](README.ru.md)

[Quick start](#quick-start) · [Screenshots](docs/SCREENSHOTS.md) · [Releases](https://github.com/shelasmax/local-sync-dashboard/releases) · [Changelog](CHANGELOG.md)

</div>

![Local Sync Dashboard: service readiness, routes, and recent runs](docs/images/overview.png)

<p align="center"><sub>Real application UI with synthetic demo data. The application interface is currently in Russian; documentation is available in English and Russian.</sub></p>

## A control room for your transfers

Keep recurring file transfers in one place: configure storage profiles, inspect the exact route, preview a copy with a dry run, and follow progress while rclone does the work.

| Capability | What it gives you |
| --- | --- |
| **Storage profiles** | Reuse local folders, mounted Synology shares, S3-compatible storage, and Yandex Disk across jobs. |
| **Preview before copying** | Check source and destination paths, browse folders, and run a dry run before moving data. |
| **Manual and scheduled jobs** | Start on demand or use hourly, daily, weekly, and cron schedules. Edit, clone, pause, and resume schedules. |
| **Live progress** | See transferred bytes and files, speed, ETA, checks, and active items. |
| **Recovery and diagnostics** | Find interrupted runs and repeated failures, then follow a relevant scenario in the built-in Ops / Runbook. |
| **Tuning and history** | Set bandwidth, checksums, filters, `transfers`, `checkers`, and `fast-list`; keep full history in a separate archive. |

> **Copy behavior:** jobs use [`rclone copy`](https://rclone.org/commands/rclone_copy/). Files missing from the source are not automatically deleted from the destination. Changed files at the same destination path can still be updated or overwritten; this is not a versioned backup system.

## See it in action

| Jobs and live progress | Storage profiles |
| --- | --- |
| [![Jobs, schedules, and live copy progress](docs/images/jobs.png)](docs/images/jobs.png) | [![Reusable storage profiles and diagnostics](docs/images/profiles.png)](docs/images/profiles.png) |
| Manage routes and see which copy is running. | Check connections and find the next corrective action. |

[Open the full gallery →](docs/SCREENSHOTS.md)

## Supported storage

| Storage | How it connects |
| --- | --- |
| **S3 / S3-compatible** | An existing rclone remote, plus a bucket and optional prefix. |
| **Yandex Disk** | An existing rclone remote, for example `yadisk:`. |
| **Synology** | An SMB/NFS share already mounted on your Mac under `/Volumes`. |
| **Local folders** | Any accessible directory on the Mac. |
| **iCloud Drive / Google Drive for Desktop** | Their locally available folders, through a local-folder profile. No direct cloud SDK integration. |

All transfers run **through your Mac**, including cloud-to-cloud copies. The Mac must stay awake, online, and connected to the required volumes. The app does not mount network shares or create rclone remotes for you.

## Quick start

You need **macOS**, **Python 3.11+**, **rclone**, and **Git**. Install rclone using its [official installation guide](https://rclone.org/install/).

```bash
git clone https://github.com/shelasmax/local-sync-dashboard.git
cd local-sync-dashboard
python3 --version  # Must be 3.11 or newer
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For cloud storage, create your connections in rclone first:

```bash
rclone config
rclone listremotes
```

Start the dashboard:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **[127.0.0.1:8000](http://127.0.0.1:8000)**.

1. **Prepare** (`Подготовка`, `/setup`): check rclone, remotes, and mounted folders.
2. **Add profiles** (`Профили`, `/profiles`): select the source and destination storage.
3. **Create a job** (`Задачи`, `/jobs`): inspect the route and run a dry run.
4. **Start the copy** and follow it in **Runs** (`Запуски`, `/runs`).

For Synology, connect in Finder first. A share such as `smb://nas.local/media/photos` becomes a local path such as `/Volumes/media/photos`. Use the actual mounted path; do not enter the SMB URL or an internal NAS path such as `/home`.

## Running longer transfers

- Run without `--reload`. Restarting the app interrupts app-managed transfers.
- The default time limit is **6 hours per run**. Adjust it in **Ops / Runbook → run limit** (`/ops`) before a longer transfer; the UI accepts 1–72 hours.
- After an interruption, rerun the same `copy` to copy the remaining delta. rclone rechecks files; the app does not reattach to the old process or guarantee byte-level resume.
- Use live progress to assess the current attempt. A previous run's summary describes that attempt only.

For persistent local operation, see the [macOS service guide](docs/OPERATIONS.md). It covers the service copy, update/status scripts, backup, and recovery.

## Local data and boundaries

The default data directory is `var/`: SQLite in `var/app.db`, logs in `var/logs/`, and active-run snapshots in `var/runtime/`. Set `LOCAL_SYNC_DATA_DIR` to use another location.

Additional secrets entered in the app are stored in **macOS Keychain**. Cloud authentication is managed separately by **rclone**. Treat its configuration and your application data as private.

This is a local, single-user utility. Keep it bound to `127.0.0.1`; it has no multi-user authentication layer. There is no two-way synchronization, delete propagation, or conflict resolution. Archive cleanup removes local run records and logs, not source or destination files.

## Latest release · v2.1.1

Timeouts now have explicit failure summaries, run details show the bytes and files already transferred, and the Ops page lets you adjust the per-run time limit.

[Bilingual release notes →](https://github.com/shelasmax/local-sync-dashboard/releases/tag/v2.1.1)

## Development

Built with **FastAPI · Jinja2 · SQLAlchemy / SQLite · APScheduler · rclone · macOS Keychain**.

```bash
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest tests.test_common tests.test_system tests.test_profiles tests.test_rclone tests.test_jobs tests.test_runtime_recovery
```

| Area | Location |
| --- | --- |
| HTTP routes and UI | [`app/main.py`](app/main.py), [`app/templates/`](app/templates/) |
| Profiles and credentials | [`app/services/profiles.py`](app/services/profiles.py), [`app/services/keychain.py`](app/services/keychain.py) |
| Jobs, scheduling, recovery | [`app/services/jobs.py`](app/services/jobs.py) |
| Transfer commands | [`app/services/rclone.py`](app/services/rclone.py) |
| Local state | [`app/models.py`](app/models.py), [`app/database.py`](app/database.py) |

Next priorities: configuration export and restore without secrets, notifications and migration to another Mac, profile-level runtime defaults, and guided rclone setup. See the [detailed backlog (Russian)](docs/BACKLOG.md) and [architecture notes (Russian)](docs/PROJECT.md).

Found a problem? [Open an issue](https://github.com/shelasmax/local-sync-dashboard/issues) with your macOS, Python, rclone, and app versions, reproduction steps, and a sanitized error message. Remove tokens, credentials, and private file paths before posting.

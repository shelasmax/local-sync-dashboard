# macOS operations

**English** · [Русский](OPERATIONS.ru.md) · [Back to the project](../README.md)

## Manual use and persistent operation

The simplest setup is a terminal running `uvicorn app.main:app --host 127.0.0.1 --port 8000`. Keep it open while jobs run. Do not use `--reload` for actual transfers.

For persistent operation, the project provides a per-user macOS LaunchAgent. It runs a **service copy outside `~/Documents`**, at:

```text
~/Library/Application Support/com.localsync.dashboard/service
```

This avoids relying on background access to a checkout inside a macOS privacy-protected folder. It is a user-session service, not an always-on remote worker; your Mac still needs to stay awake and online.

## Install or update the service copy

First complete the [manual setup](../README.md#quick-start) and verify a small dry run. The service updater uses `/usr/bin/python3` when creating its virtualenv; check that this interpreter satisfies the project's **Python 3.11+** requirement. If it does not, create the service virtualenv with your supported Python before running the updater:

```bash
python3 --version  # Must be 3.11 or newer
mkdir -p "$HOME/Library/Application Support/com.localsync.dashboard/service"
python3 -m venv "$HOME/Library/Application Support/com.localsync.dashboard/service/.venv"
```

Wait for active jobs to finish, then stop the manually started app so port 8000 is free. From the repository root:

```bash
./scripts/update_launchd_service.sh
./scripts/launchd_status.sh
```

The updater copies code into the service directory, refreshes its virtualenv, writes the LaunchAgent plist, and restarts `com.localsync.dashboard`. Its `rsync --delete` removes obsolete **service-code files**, while excluding `var/` and `.venv/`. It does not transfer or delete source/destination storage data.

**First installation:** repository `var/` is excluded from the copy. Existing profiles, jobs, and history are not migrated automatically. If you need them, stop both app instances and restore a private backup into the service's `var/` before using its schedules.

**Verification:** check the status script, open [127.0.0.1:8000](http://127.0.0.1:8000), verify profiles and routes, and run a small dry run. Use `/ops` for missing remotes, mounts, credentials, interrupted jobs, and time limits.

## Back up local state

Configuration export/import without secrets is planned; there is no built-in migration wizard yet.

1. Wait for active copies to finish and stop the app. For a managed service, unload the agent before copying its data; simply killing the process can cause `KeepAlive` to restart it.
2. Copy the **entire data directory** to a private backup location. The default is `var/` in the active app copy; `LOCAL_SYNC_DATA_DIR` may override it. Keep the existing backup until the replacement is verified.
3. Handle rclone configuration and macOS Keychain separately. A database backup does not include those credentials. Reauthorize cloud remotes and re-enter app secrets on a new Mac as needed.
4. Restore data with the app stopped, check folder paths and mounted volumes, then start the matching app version. Review enabled schedules before allowing scheduled work to resume.

To unload the managed service:

```bash
launchctl bootout "gui/$(id -u)/com.localsync.dashboard"
```

To load the already-installed plist again:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.localsync.dashboard.plist"
```

Backups and logs can contain private names and paths. Do not attach them to public issues without sanitizing them.

## Rollback and recovery

Before a code update, save the current revision and a stopped-state backup of `var/`. To roll back, restore that code revision in a separate checkout and run its service updater after active copies finish. If schema compatibility is uncertain, restore the matching database backup while the app is stopped; additive migrations do not guarantee backward compatibility with every older version.

An interrupted run becomes `interrupted`, with the last available progress retained. Use the same route and repeat `rclone copy` from the delta after checking mounts, remotes, free space, and sleep settings. Do not restart a route while an independently running copy still writes to the same destination.

## Runtime tuning

The job form supports `transfers`, `checkers`, and `fast-list`, plus bandwidth limits, filters, and checksum verification. Leave defaults in place initially, then tune a representative small job. More concurrency is not always faster; `fast-list` uses more memory and depends on backend support.

The default per-run timeout is 6 hours. `/ops` accepts 1–72 hours and applies the setting to subsequent executions. The Mac remains part of the transfer path even when both endpoints are clouds.

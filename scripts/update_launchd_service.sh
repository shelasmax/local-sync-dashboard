#!/bin/zsh

set -euo pipefail

APP_LABEL="com.localsync.dashboard"
PORT="8000"
USER_ID="$(id -u)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_BASE="$HOME/Library/Application Support/$APP_LABEL"
SERVICE_ROOT="$SERVICE_BASE/service"
PLIST_TEMPLATE="$REPO_ROOT/launchd/com.localsync.dashboard.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/com.localsync.dashboard.plist"

if [[ ! -f "$PLIST_TEMPLATE" ]]; then
  echo "launchd template not found: $PLIST_TEMPLATE" >&2
  exit 1
fi

mkdir -p "$SERVICE_BASE" "$HOME/Library/LaunchAgents"

/usr/bin/rsync -a --delete \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'output/' \
  --exclude 'var/' \
  --exclude '.venv/' \
  "$REPO_ROOT/" "$SERVICE_ROOT/"

if [[ ! -x "$SERVICE_ROOT/.venv/bin/python" ]]; then
  /usr/bin/python3 -m venv "$SERVICE_ROOT/.venv"
fi

"$SERVICE_ROOT/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$SERVICE_ROOT/.venv/bin/python" -m pip install -e "$SERVICE_ROOT" >/dev/null

/usr/bin/perl -0pe "s#/ABSOLUTE/PATH/TO/S3_Synology-to-Yandex_DIsk#$SERVICE_ROOT#g" \
  "$PLIST_TEMPLATE" > "$PLIST_PATH"

/bin/launchctl bootout "gui/$USER_ID/$APP_LABEL" >/dev/null 2>&1 || true
sleep 1

if ! /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST_PATH"; then
  sleep 2
  /bin/launchctl bootstrap "gui/$USER_ID" "$PLIST_PATH"
fi

/bin/launchctl enable "gui/$USER_ID/$APP_LABEL"
/bin/launchctl kickstart -k "gui/$USER_ID/$APP_LABEL"
/bin/launchctl print "gui/$USER_ID/$APP_LABEL" >/dev/null

echo "Updated service copy: $SERVICE_ROOT"
echo "Restarted launchd agent: $APP_LABEL"
echo "UI should be available at http://127.0.0.1:$PORT"

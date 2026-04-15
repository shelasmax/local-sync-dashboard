#!/bin/zsh

set -euo pipefail

APP_LABEL="com.localsync.dashboard"
PORT="8000"
USER_ID="$(id -u)"
SERVICE_ROOT="$HOME/Library/Application Support/$APP_LABEL/service"
PLIST_PATH="$HOME/Library/LaunchAgents/com.localsync.dashboard.plist"
STDOUT_LOG="$SERVICE_ROOT/var/logs/launchd.stdout.log"
STDERR_LOG="$SERVICE_ROOT/var/logs/launchd.stderr.log"

echo "Agent: $APP_LABEL"
echo "Port: $PORT"
echo "Plist: $PLIST_PATH"
echo "Service root: $SERVICE_ROOT"
echo

if [[ -f "$PLIST_PATH" ]]; then
  echo "LaunchAgent plist: present"
else
  echo "LaunchAgent plist: missing"
fi

echo
echo "launchctl:"
STATUS_TMP="$(mktemp "/tmp/$APP_LABEL.status.XXXXXX")"
if /bin/launchctl print "gui/$USER_ID/$APP_LABEL" >"$STATUS_TMP" 2>/dev/null; then
  /usr/bin/grep -nE "state =|pid =|path =|program =|working directory =|stdout path =|stderr path =|last exit code|last terminating signal" "$STATUS_TMP" || true
else
  echo "  agent is not loaded in launchd"
fi
rm -f "$STATUS_TMP"

echo
echo "Port listener:"
if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN; then
  true
else
  echo "  nothing is listening on 127.0.0.1:$PORT"
fi

echo
echo "HTTP check:"
HTTP_STATUS="$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" || true)"
if [[ "$HTTP_STATUS" == "200" ]]; then
  echo "  HTTP 200 from /"
elif [[ "$HTTP_STATUS" == "000" ]]; then
  echo "  HTTP check unavailable from current shell; rely on listener/logs above"
else
  echo "  HTTP check failed (status: ${HTTP_STATUS:-unavailable})"
fi

echo
for log_path in "$STDOUT_LOG" "$STDERR_LOG"; do
  echo "Log tail: $log_path"
  if [[ -f "$log_path" ]]; then
    /usr/bin/tail -n 10 "$log_path"
  else
    echo "  log file missing"
  fi
  echo
done

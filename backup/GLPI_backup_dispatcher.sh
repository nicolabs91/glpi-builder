#!/bin/bash
# Managed by Docker App Manager. Run this single dispatcher from Synology Task Scheduler.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BACKUP_TASK_DIR=${GLPI_BACKUP_TASK_DIR:-"$(dirname "$SCRIPT_DIR")/GLPI_backup/_system"}
PROJECTS_DIR="$BACKUP_TASK_DIR/projects"
STATE_DIR="$BACKUP_TASK_DIR/state"
LOCKS_DIR="$BACKUP_TASK_DIR/locks"
BACKUP_SCRIPT="$BACKUP_TASK_DIR/GLPI_backup.sh"
HEARTBEAT="$STATE_DIR/dispatcher.env"
FALLBACK_HEARTBEAT="$SCRIPT_DIR/.dispatcher.env"
LOCK_DIR="$LOCKS_DIR/dispatcher.lock"

for DIRECTORY in "$PROJECTS_DIR" "$STATE_DIR" "$LOCKS_DIR"; do
  [ ! -L "$DIRECTORY" ] || {
    echo "Unsafe symlink used for managed backup directory: $DIRECTORY" >&2
    exit 1
  }
  mkdir -p "$DIRECTORY"
  chmod 0700 "$DIRECTORY"
done
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another dispatcher run is already active; exiting safely."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT INT TERM

write_heartbeat() {
  STATUS_VALUE=$1
  HEARTBEAT_EPOCH=$(date +%s)
  printf 'LAST_HEARTBEAT=%s\nSTATUS=%s\n' "$HEARTBEAT_EPOCH" "$STATUS_VALUE" > "$HEARTBEAT.tmp"
  mv "$HEARTBEAT.tmp" "$HEARTBEAT"
  printf 'LAST_HEARTBEAT=%s\nSTATUS=%s\n' "$HEARTBEAT_EPOCH" "$STATUS_VALUE" > "$FALLBACK_HEARTBEAT.tmp"
  mv "$FALLBACK_HEARTBEAT.tmp" "$FALLBACK_HEARTBEAT"
}

NOW=$(date +%s)
TODAY=$(date +%Y-%m-%d)
WEEKDAY=$(date +%u)
CURRENT_HM=$(date +%H:%M)
write_heartbeat running

for CONFIG in "$PROJECTS_DIR"/*.env; do
  [ -f "$CONFIG" ] || continue
  unset PROJECT_NAME SCHEDULE_ENABLED SCHEDULE_KIND SCHEDULE_TIME SCHEDULE_WEEKDAYS INTERVAL_HOURS
  # shellcheck disable=SC1090
  . "$CONFIG"
  [ "${SCHEDULE_ENABLED:-no}" = "yes" ] || continue
  case "${PROJECT_NAME:-}" in *[!a-z0-9_-]*|'') continue ;; esac
  STATE="$STATE_DIR/$PROJECT_NAME.env"
  LAST_ATTEMPT=0
  LAST_DATE=""
  LAST_SUCCESS=0
  [ -f "$STATE" ] && LAST_ATTEMPT=$(sed -n 's/^LAST_ATTEMPT=//p' "$STATE" | tail -1)
  [ -f "$STATE" ] && LAST_DATE=$(sed -n 's/^LAST_DATE=//p' "$STATE" | tail -1)
  [ -f "$STATE" ] && LAST_SUCCESS=$(sed -n 's/^LAST_SUCCESS=//p' "$STATE" | tail -1)
  case "$LAST_ATTEMPT" in *[!0-9]*|'') LAST_ATTEMPT=0 ;; esac
  case "$LAST_SUCCESS" in *[!0-9]*|'') LAST_SUCCESS=0 ;; esac

  DUE=no
  case "${SCHEDULE_KIND:-daily}" in
    daily)
      [ "$CURRENT_HM" \> "${SCHEDULE_TIME:-02:00}" ] || [ "$CURRENT_HM" = "${SCHEDULE_TIME:-02:00}" ] || continue
      [ "$LAST_DATE" != "$TODAY" ] && DUE=yes
      ;;
    weekly)
      case ",${SCHEDULE_WEEKDAYS:-7}," in *,"$WEEKDAY",*)
        if { [ "$CURRENT_HM" \> "${SCHEDULE_TIME:-02:00}" ] || [ "$CURRENT_HM" = "${SCHEDULE_TIME:-02:00}" ]; } && [ "$LAST_DATE" != "$TODAY" ]; then DUE=yes; fi
        ;;
      esac
      ;;
    interval)
      HOURS=${INTERVAL_HOURS:-24}
      case "$HOURS" in *[!0-9]*|'') HOURS=24 ;; esac
      [ $((NOW - LAST_ATTEMPT)) -ge $((HOURS * 3600)) ] && DUE=yes
      ;;
  esac
  [ "$DUE" = yes ] || continue

  printf 'LAST_ATTEMPT=%s\nLAST_DATE=%s\nLAST_STATUS=running\n' "$NOW" "$TODAY" > "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
  LOG="$STATE_DIR/$PROJECT_NAME.last.log"
  if GLPI_BACKUP_ENV="$CONFIG" /bin/bash "$BACKUP_SCRIPT" > "$LOG.tmp" 2>&1; then
    printf 'LAST_ATTEMPT=%s\nLAST_DATE=%s\nLAST_SUCCESS=%s\nLAST_STATUS=success\n' "$NOW" "$TODAY" "$(date +%s)" > "$STATE.tmp"
  else
    CODE=$?
    printf 'LAST_ATTEMPT=%s\nLAST_DATE=%s\nLAST_SUCCESS=%s\nLAST_STATUS=failed\nLAST_EXIT_CODE=%s\n' "$NOW" "$TODAY" "$LAST_SUCCESS" "$CODE" > "$STATE.tmp"
  fi
  mv "$STATE.tmp" "$STATE"
  mv "$LOG.tmp" "$LOG"
done

write_heartbeat idle

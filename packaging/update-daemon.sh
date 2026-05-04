#!/bin/sh
set -eu

CONFIG_DIR="${CONFIG_DIR:-/config}"
INSTALL_DIR="${HERMES_VOICE_INSTALL_DIR:?HERMES_VOICE_INSTALL_DIR is required}"
SOURCE_DIR="${HERMES_VOICE_SOURCE_DIR:?HERMES_VOICE_SOURCE_DIR is required}"
UPDATE_BRANCH="${HERMES_VOICE_UPDATE_BRANCH:-main}"
REQUEST_FILE="$CONFIG_DIR/update-request"
LAST_FILE="$CONFIG_DIR/update-request.done"
STATUS_FILE="$CONFIG_DIR/update-status.json"

json_string() {
  printf '%s' "$1" | awk 'BEGIN { ORS = "" } { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); if (NR > 1) printf "\\n"; printf "%s", $0 }'
}

write_status() {
  id="$1"
  running="$2"
  ok="$3"
  message="$(json_string "$4")"
  detail="$(json_string "${5:-}")"
  current="$(git -C "$SOURCE_DIR" rev-parse --short HEAD 2>/dev/null || true)"
  tmp="$STATUS_FILE.tmp.$$"
  printf '{"id":"%s","running":%s,"ok":%s,"message":"%s","detail":"%s","current":"%s","time":%s}\n' \
    "$id" "$running" "$ok" "$message" "$detail" "$current" "$(date +%s)" > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

run_update() {
  id="$1"
  log_file="$CONFIG_DIR/update-$id.log"
  write_status "$id" true false "Update running" ""

  set +e
  {
    echo "Fetching origin/$UPDATE_BRANCH"
    git -C "$SOURCE_DIR" fetch --prune origin "$UPDATE_BRANCH:refs/remotes/origin/$UPDATE_BRANCH"
    git -C "$SOURCE_DIR" checkout -B "$UPDATE_BRANCH" "origin/$UPDATE_BRANCH"
    git -C "$SOURCE_DIR" reset --hard "origin/$UPDATE_BRANCH"
    echo "Rebuilding Hermes Voice from $SOURCE_DIR"
    HERMES_VOICE_INSTALL_DIR="$INSTALL_DIR" \
      HERMES_VOICE_UPDATE_FROM_WEBUI=1 \
      "$SOURCE_DIR/packaging/install.sh"
  } > "$log_file" 2>&1
  code=$?
  set -e

  tail_text="$(tail -n 80 "$log_file" 2>/dev/null || true)"
  if [ "$code" -eq 0 ]; then
    write_status "$id" false true "Update finished" "$tail_text"
  else
    write_status "$id" false false "Update failed with exit $code" "$tail_text"
  fi
}

mkdir -p "$CONFIG_DIR"
write_status "startup" false true "Updater ready" ""

while :; do
  if [ -f "$REQUEST_FILE" ]; then
    request_id="$(tr -cd 'A-Za-z0-9_.:-' < "$REQUEST_FILE" | head -c 80 || true)"
    last_id="$(cat "$LAST_FILE" 2>/dev/null || true)"
    if [ -n "$request_id" ] && [ "$request_id" != "$last_id" ]; then
      printf '%s\n' "$request_id" > "$LAST_FILE"
      run_update "$request_id"
    fi
  fi
  sleep 2
done

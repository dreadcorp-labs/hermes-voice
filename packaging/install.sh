#!/usr/bin/env bash
set -euo pipefail
umask 077

INSTALL_DIR="${HERMES_VOICE_INSTALL_DIR:-$HOME/.hermes-voice}"
PACKAGE_MODE="${HERMES_VOICE_PACKAGE_MODE:-single}"
WEBUI_PORT="${WEBUI_PORT:-8765}"
LIVEKIT_PORT="${LIVEKIT_PORT:-7880}"
LIVEKIT_RTC_TCP_PORT="${LIVEKIT_RTC_TCP_PORT:-7881}"
LIVEKIT_RTC_UDP_START="${LIVEKIT_RTC_UDP_START:-50000}"
LIVEKIT_RTC_UDP_END="${LIVEKIT_RTC_UDP_END:-50100}"
REDIS_PORT="${REDIS_PORT:-16379}"
TTS_PORT="${TTS_PORT:-8890}"
PUBLIC_HOST="${HERMES_VOICE_PUBLIC_HOST:-localhost}"
BIND_HOST="${HERMES_VOICE_BIND_HOST:-127.0.0.1}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd "$script_dir/.." && pwd)"

default_compose_project="$(basename "$INSTALL_DIR" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//')"
COMPOSE_PROJECT_NAME="${HERMES_VOICE_COMPOSE_PROJECT:-${default_compose_project:-hermes-voice}}"

if [ ! -f "$source_dir/sidecar/Dockerfile" ] || [ ! -f "$source_dir/tts/Dockerfile" ]; then
  source_dir="$INSTALL_DIR/source"
  if [ ! -f "$source_dir/sidecar/Dockerfile" ] || [ ! -f "$source_dir/tts/Dockerfile" ]; then
    if [ -z "${HERMES_VOICE_REPO_URL:-}" ]; then
      echo "Could not find the Hermes Voice source tree." >&2
      echo "Run this script from the repo, or set HERMES_VOICE_REPO_URL to a git URL." >&2
      exit 1
    fi
    if ! command -v git >/dev/null 2>&1; then
      echo "git is required when installing from HERMES_VOICE_REPO_URL." >&2
      exit 1
    fi
    mkdir -p "$INSTALL_DIR"
    rm -rf "$source_dir"
    git clone "$HERMES_VOICE_REPO_URL" "$source_dir"
  fi
fi

if command -v git >/dev/null 2>&1 && [ -d "$source_dir/.git" ]; then
  HERMES_VOICE_VERSION="${HERMES_VOICE_VERSION:-$(git -C "$source_dir" rev-parse --short HEAD 2>/dev/null || true)}"
fi
HERMES_VOICE_VERSION="${HERMES_VOICE_VERSION:-dev}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker, then rerun this installer." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Install or update Docker, then rerun this installer." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR/config"
if [ "$PACKAGE_MODE" = "single" ]; then
  cp "$source_dir/packaging/compose.single.yaml" "$INSTALL_DIR/compose.yaml"
elif [ "$PACKAGE_MODE" = "multi" ]; then
  cp "$source_dir/packaging/compose.yaml" "$INSTALL_DIR/compose.yaml"
else
  echo "Unsupported HERMES_VOICE_PACKAGE_MODE: $PACKAGE_MODE" >&2
  echo "Use 'single' or 'multi'." >&2
  exit 1
fi

secret_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  elif command -v shasum >/dev/null 2>&1; then
    date +%s%N | shasum -a 256 | awk '{print $1}'
  else
    date +%s%N | sha256sum | awk '{print $1}'
  fi
}

read_existing_config() {
  local key="$1"
  local path="$INSTALL_DIR/config/hermes-voice.env"
  if [ -f "$path" ]; then
    awk -F= -v wanted="$key" '$1 == wanted { value=$0; sub(/^[^=]*=/, "", value); gsub(/^'\''|'\''$/, "", value); gsub(/^"|"$/, "", value); print value; exit }' "$path"
  fi
}

detect_lan_cidrs() {
  if command -v ip >/dev/null 2>&1; then
    ip -o -4 addr show scope global | awk '
      $2 !~ /^(lo|docker|br-|veth|tailscale|tun|wg)/ {
        if (out) out = out "," $4; else out = $4
      }
      END { print out }
    '
  fi
}

LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-$(read_existing_config LIVEKIT_API_KEY)}"
LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-$(secret_hex)}"
if [ "$LIVEKIT_API_KEY" = "hermes_livekit" ]; then
  echo "LIVEKIT_API_KEY=hermes_livekit is not allowed. Use a random key or omit it so the installer can generate one." >&2
  exit 1
fi
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-$(read_existing_config LIVEKIT_API_SECRET)}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-$(secret_hex)}"
HERMES_SETUP_TOKEN="${HERMES_SETUP_TOKEN:-$(read_existing_config HERMES_SETUP_TOKEN)}"
HERMES_SETUP_TOKEN="${HERMES_SETUP_TOKEN:-$(secret_hex)}"
HERMES_DISCOVERY_CIDRS="${HERMES_DISCOVERY_CIDRS:-$(read_existing_config HERMES_DISCOVERY_CIDRS)}"
HERMES_DISCOVERY_CIDRS="${HERMES_DISCOVERY_CIDRS:-$(detect_lan_cidrs)}"
HERMES_DISCOVERY_PORTS="${HERMES_DISCOVERY_PORTS:-$(read_existing_config HERMES_DISCOVERY_PORTS)}"
HERMES_DISCOVERY_PORTS="${HERMES_DISCOVERY_PORTS:-8642,8000,8080,1235}"
if [ -z "${HERMES_API_URL:-}" ]; then
  HERMES_API_URL="$(read_existing_config HERMES_API_URL)"
fi
if [ -z "${HERMES_API_URL:-}" ]; then
  HERMES_API_URL="http://host.docker.internal:8642/v1/chat/completions"
  if command -v curl >/dev/null 2>&1; then
    status="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/models 2>/dev/null || true)"
    if [ "$status" != "000" ] && [ -n "$status" ]; then
      HERMES_API_URL="http://host.docker.internal:8642/v1/chat/completions"
    fi
  fi
fi
HERMES_API_KEY="${HERMES_API_KEY:-${API_SERVER_KEY:-}}"
HERMES_API_KEY="${HERMES_API_KEY:-$(read_existing_config HERMES_API_KEY)}"
if [ -z "$HERMES_API_KEY" ] && [ -f "$HOME/.hermes/.env" ]; then
  hermes_key_line="$(grep -E '^(HERMES_API_KEY|API_SERVER_KEY)=' "$HOME/.hermes/.env" | head -n 1 || true)"
  HERMES_API_KEY="${hermes_key_line#*=}"
  HERMES_API_KEY="${HERMES_API_KEY%\"}"
  HERMES_API_KEY="${HERMES_API_KEY#\"}"
  HERMES_API_KEY="${HERMES_API_KEY%\'}"
  HERMES_API_KEY="${HERMES_API_KEY#\'}"
fi
LIVEKIT_PUBLIC_URL="${LIVEKIT_PUBLIC_URL:-$(read_existing_config LIVEKIT_PUBLIC_URL)}"
LIVEKIT_PUBLIC_URL="${LIVEKIT_PUBLIC_URL:-ws://$PUBLIC_HOST:$LIVEKIT_PORT}"
WEBUI_URL="${HERMES_VOICE_WEBUI_URL:-http://$PUBLIC_HOST:$WEBUI_PORT}"
if [ "$PACKAGE_MODE" = "single" ]; then
  LIVEKIT_INTERNAL_URL="ws://127.0.0.1:$LIVEKIT_PORT"
  LIVEKIT_CONFIG_PORT="$LIVEKIT_PORT"
  LIVEKIT_CONFIG_REDIS="127.0.0.1:$REDIS_PORT"
  LIVEKIT_CONFIG_RTC_TCP_PORT="$LIVEKIT_RTC_TCP_PORT"
  SIDECAR_PORT="$WEBUI_PORT"
  SIDECAR_BIND_HOST="$BIND_HOST"
  SIDECAR_STATIC_DIR="/app/sidecar/static"
  TTS_INTERNAL_URL="http://127.0.0.1:$TTS_PORT/v1/audio/speech"
  EMOTION_HELPER="/app/sidecar/wav2vec_emotion_analyze.py"
  MANAGE_LOG_SERVICE="hermes-voice"
else
  LIVEKIT_INTERNAL_URL="ws://livekit:7880"
  LIVEKIT_CONFIG_PORT="7880"
  LIVEKIT_CONFIG_REDIS="redis:6379"
  LIVEKIT_CONFIG_RTC_TCP_PORT="7881"
  SIDECAR_PORT="8765"
  SIDECAR_BIND_HOST="0.0.0.0"
  SIDECAR_STATIC_DIR="/app/static"
  TTS_INTERNAL_URL="http://tts:8889/v1/audio/speech"
  EMOTION_HELPER="/app/wav2vec_emotion_analyze.py"
  MANAGE_LOG_SERVICE="sidecar"
fi

cat > "$INSTALL_DIR/.env" <<EOF
HERMES_VOICE_COMPOSE_PROJECT=$COMPOSE_PROJECT_NAME
HERMES_VOICE_SOURCE_DIR=$source_dir
HERMES_VOICE_VERSION=$HERMES_VOICE_VERSION
HERMES_VOICE_PUBLIC_HOST=$PUBLIC_HOST
HERMES_VOICE_BIND_HOST=$BIND_HOST
HERMES_VOICE_PACKAGE_MODE=$PACKAGE_MODE
WEBUI_PORT=$WEBUI_PORT
LIVEKIT_PORT=$LIVEKIT_PORT
LIVEKIT_RTC_TCP_PORT=$LIVEKIT_RTC_TCP_PORT
LIVEKIT_RTC_UDP_START=$LIVEKIT_RTC_UDP_START
LIVEKIT_RTC_UDP_END=$LIVEKIT_RTC_UDP_END
LIVEKIT_NODE_IP=$PUBLIC_HOST
REDIS_PORT=$REDIS_PORT
TTS_PORT=$TTS_PORT
EOF

cat > "$INSTALL_DIR/config/livekit.yaml" <<EOF
port: $LIVEKIT_CONFIG_PORT

redis:
  address: $LIVEKIT_CONFIG_REDIS

rtc:
  tcp_port: $LIVEKIT_CONFIG_RTC_TCP_PORT
  port_range_start: $LIVEKIT_RTC_UDP_START
  port_range_end: $LIVEKIT_RTC_UDP_END
  use_external_ip: false

keys:
  "$LIVEKIT_API_KEY": "$LIVEKIT_API_SECRET"

logging:
  level: info
  pion_level: warn

room:
  auto_create: true
  empty_timeout: 300
  departure_timeout: 30
EOF

cat > "$INSTALL_DIR/config/hermes-voice.env" <<EOF
LIVEKIT_URL=$LIVEKIT_INTERNAL_URL
LIVEKIT_PUBLIC_URL=$LIVEKIT_PUBLIC_URL
LIVEKIT_API_KEY=$LIVEKIT_API_KEY
LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET
LIVEKIT_ROOM=hermes-voice
HERMES_SETUP_TOKEN=$HERMES_SETUP_TOKEN
HERMES_VOICE_VERSION=$HERMES_VOICE_VERSION

HERMES_LIVEKIT_VOICE_HOST=$SIDECAR_BIND_HOST
HERMES_LIVEKIT_VOICE_PORT=$SIDECAR_PORT
HERMES_LIVEKIT_STATIC_DIR=$SIDECAR_STATIC_DIR

HERMES_API_URL=$HERMES_API_URL
HERMES_API_KEY=$HERMES_API_KEY
HERMES_SESSION_ID=livekit-voice-main
HERMES_API_MODEL=hermes-agent
HERMES_API_REASONING_EFFORT=none
HERMES_LIVEKIT_HERMES_STREAMING=true
HERMES_DISCOVERY_CIDRS=$HERMES_DISCOVERY_CIDRS
HERMES_DISCOVERY_PORTS=$HERMES_DISCOVERY_PORTS
HERMES_DISCOVERY_MAX_HOSTS=512

HERMES_LIVEKIT_TTS_BACKEND=hermes_voice_native
HERMES_LIVEKIT_TTS_URL=$TTS_INTERNAL_URL
HERMES_LIVEKIT_TTS_MODEL=tts-1
HERMES_LIVEKIT_TTS_VOICE=
HERMES_LIVEKIT_TTS_RESPONSE_FORMAT=mp3
HERMES_LIVEKIT_TTS_SPEED=1.0
HERMES_LIVEKIT_TTS_TIMEOUT_SECONDS=30

HERMES_LIVEKIT_STT_PROVIDER=auto
HERMES_LIVEKIT_STT_MODEL=
HERMES_LIVEKIT_LOCAL_STT_MODEL=base.en

HERMES_EMOTION2VEC_ENABLED=true
HERMES_EMOTION2VEC_PYTHON=/usr/local/bin/python
HERMES_EMOTION2VEC_HELPER=$EMOTION_HELPER
HERMES_EMOTION2VEC_PYTHONPATH=
HERMES_EMOTION2VEC_CACHE_DIR=/data/emotion
HERMES_EMOTION2VEC_TIMEOUT_SECONDS=8.0
EOF

chmod 600 "$INSTALL_DIR/.env" "$INSTALL_DIR/config/livekit.yaml" "$INSTALL_DIR/config/hermes-voice.env"

if command -v chown >/dev/null 2>&1; then
  chown -R 1000:1000 "$INSTALL_DIR/config" 2>/dev/null || true
fi

echo "Starting Hermes Voice from $INSTALL_DIR"
(
  cd "$INSTALL_DIR"
  docker compose up -d --build
)

cat <<EOF

Hermes Voice is starting.

Web UI:
  $WEBUI_URL

First-run setup:
  $WEBUI_URL/setup?setupToken=$HERMES_SETUP_TOKEN

If you did not provide HERMES_API_KEY, open the setup URL above and complete first-run setup.

Manage:
  cd "$INSTALL_DIR"
  docker compose ps
  docker compose logs -f $MANAGE_LOG_SERVICE
  docker compose restart $MANAGE_LOG_SERVICE
  docker compose down

EOF

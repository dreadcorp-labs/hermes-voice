#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${CONFIG_DIR:-/config}"
DATA_DIR="${DATA_DIR:-/data}"
WEBUI_PORT="${WEBUI_PORT:-8765}"
LIVEKIT_PORT="${LIVEKIT_PORT:-7880}"
LIVEKIT_RTC_TCP_PORT="${LIVEKIT_RTC_TCP_PORT:-7881}"
LIVEKIT_RTC_UDP_START="${LIVEKIT_RTC_UDP_START:-50000}"
LIVEKIT_RTC_UDP_END="${LIVEKIT_RTC_UDP_END:-50100}"
REDIS_PORT="${REDIS_PORT:-16379}"
TTS_PORT="${TTS_PORT:-8890}"
PUBLIC_HOST="${HERMES_VOICE_PUBLIC_HOST:-${LIVEKIT_NODE_IP:-}}"

mkdir -p "$CONFIG_DIR" "$DATA_DIR/redis" "$DATA_DIR/emotion" "$DATA_DIR/huggingface" "$DATA_DIR/whisper"

secret_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    date +%s%N | sha256sum | awk '{print $1}'
  fi
}

detect_host_ip() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

read_env_value() {
  local key="$1"
  local path="$CONFIG_DIR/hermes-voice.env"
  if [ -f "$path" ]; then
    awk -F= -v wanted="$key" '$1 == wanted { value=$0; sub(/^[^=]*=/, "", value); gsub(/^'\''|'\''$/, "", value); gsub(/^"|"$/, "", value); print value; exit }' "$path"
  fi
}

if [ -z "$PUBLIC_HOST" ]; then
  PUBLIC_HOST="$(detect_host_ip)"
fi
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"

LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-$(read_env_value LIVEKIT_API_KEY)}"
LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-hermes_livekit}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-$(read_env_value LIVEKIT_API_SECRET)}"
LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-$(secret_hex)}"
LIVEKIT_ROOM="${LIVEKIT_ROOM:-$(read_env_value LIVEKIT_ROOM)}"
LIVEKIT_ROOM="${LIVEKIT_ROOM:-hermes-voice}"
LIVEKIT_URL="${LIVEKIT_URL:-ws://127.0.0.1:$LIVEKIT_PORT}"
LIVEKIT_PUBLIC_URL="${LIVEKIT_PUBLIC_URL:-$(read_env_value LIVEKIT_PUBLIC_URL)}"
LIVEKIT_PUBLIC_URL="${LIVEKIT_PUBLIC_URL:-ws://$PUBLIC_HOST:$LIVEKIT_PORT}"
HERMES_API_URL="${HERMES_API_URL:-$(read_env_value HERMES_API_URL)}"
HERMES_API_URL="${HERMES_API_URL:-http://host.docker.internal:8642/v1/chat/completions}"
HERMES_API_KEY="${HERMES_API_KEY:-${API_SERVER_KEY:-$(read_env_value HERMES_API_KEY)}}"
HERMES_DISCOVERY_CIDRS="${HERMES_DISCOVERY_CIDRS:-$(read_env_value HERMES_DISCOVERY_CIDRS)}"
HERMES_DISCOVERY_PORTS="${HERMES_DISCOVERY_PORTS:-$(read_env_value HERMES_DISCOVERY_PORTS)}"
HERMES_DISCOVERY_PORTS="${HERMES_DISCOVERY_PORTS:-8642,8000,8080,1235}"

cat > "$CONFIG_DIR/livekit.yaml" <<EOF
port: $LIVEKIT_PORT

redis:
  address: 127.0.0.1:$REDIS_PORT

rtc:
  tcp_port: $LIVEKIT_RTC_TCP_PORT
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

if [ ! -f "$CONFIG_DIR/hermes-voice.env" ]; then
  cat > "$CONFIG_DIR/hermes-voice.env" <<EOF
LIVEKIT_URL=$LIVEKIT_URL
LIVEKIT_PUBLIC_URL=$LIVEKIT_PUBLIC_URL
LIVEKIT_API_KEY=$LIVEKIT_API_KEY
LIVEKIT_API_SECRET=$LIVEKIT_API_SECRET
LIVEKIT_ROOM=$LIVEKIT_ROOM

HERMES_LIVEKIT_VOICE_HOST=0.0.0.0
HERMES_LIVEKIT_VOICE_PORT=$WEBUI_PORT
HERMES_LIVEKIT_STATIC_DIR=/app/sidecar/static

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
HERMES_LIVEKIT_TTS_URL=http://127.0.0.1:$TTS_PORT/v1/audio/speech
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
HERMES_EMOTION2VEC_HELPER=/app/sidecar/wav2vec_emotion_analyze.py
HERMES_EMOTION2VEC_PYTHONPATH=
HERMES_EMOTION2VEC_CACHE_DIR=$DATA_DIR/emotion
HERMES_EMOTION2VEC_TIMEOUT_SECONDS=8.0
EOF
fi

redis-server --port "$REDIS_PORT" --save "" --appendonly no --dir "$DATA_DIR/redis" &
redis_pid=$!

livekit-server --config "$CONFIG_DIR/livekit.yaml" --node-ip "${LIVEKIT_NODE_IP:-$PUBLIC_HOST}" &
livekit_pid=$!

(
  cd /app/tts
  uvicorn app.main:app --host 0.0.0.0 --port "$TTS_PORT"
) &
tts_pid=$!

python /app/sidecar/livekit_voice_server.py --env "$CONFIG_DIR/hermes-voice.env" &
sidecar_pid=$!

shutdown() {
  kill "$sidecar_pid" "$tts_pid" "$livekit_pid" "$redis_pid" 2>/dev/null || true
  wait "$sidecar_pid" "$tts_pid" "$livekit_pid" "$redis_pid" 2>/dev/null || true
}
trap shutdown INT TERM

wait -n "$sidecar_pid" "$tts_pid" "$livekit_pid" "$redis_pid"
status=$?
shutdown
exit "$status"

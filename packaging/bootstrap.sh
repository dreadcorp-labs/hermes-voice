#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${HERMES_VOICE_REPO_URL:-https://github.com/dreadcorp-labs/hermes-voice.git}"
INSTALL_DIR="${HERMES_VOICE_INSTALL_DIR:-$HOME/.hermes-voice}"
SOURCE_DIR="${HERMES_VOICE_SOURCE_DIR:-$INSTALL_DIR/source}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Install git, then rerun this installer." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"

if [ -d "$SOURCE_DIR/.git" ]; then
  git -C "$SOURCE_DIR" fetch --prune origin
  git -C "$SOURCE_DIR" checkout -B main origin/main
  git -C "$SOURCE_DIR" reset --hard origin/main
else
  rm -rf "$SOURCE_DIR"
  git clone "$REPO_URL" "$SOURCE_DIR"
fi

HERMES_VOICE_INSTALL_DIR="$INSTALL_DIR" "$SOURCE_DIR/packaging/install.sh"

# Hermes Voice Agent Guide

This repo is the package source for the Hermes Voice LiveKit sidecar,
browser client, and desktop shell.

## Layout

- `sidecar/livekit_voice_server.py` runs the LiveKit voice sidecar.
- `sidecar/static/index.html` is the browser voice client.
- `deploy/macos/` contains macOS LaunchAgent and environment templates.
- `deploy/livekit/` contains the LiveKit compose/config files.
- `docs/` contains architecture and TTS backend notes.

## Working Rules

- Preserve live secrets. Do not commit `.env`, live `livekit.yaml`, API keys,
  generated audio, or logs.
- Keep voice requests routed through Hermes for tools and memory unless the
  user explicitly asks for a sidecar-only fast path.
- Treat the voice sidecar as latency-sensitive, but do not reduce normal text
  Hermes coding sessions to voice-mode reasoning settings.
- Prefer small patches and verify from an independent check where practical.
- Do not overwrite user edits. Read `git status --short` before modifying files.

## Verification

Use the narrowest relevant check first:

```bash
python3 -m py_compile sidecar/livekit_voice_server.py
```

For LiveKit compose changes:

```bash
docker compose -f deploy/livekit/compose.yaml config
```

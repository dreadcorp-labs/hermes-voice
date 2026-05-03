# Hermes Voice

Hermes Voice is a browser and desktop voice client for a Hermes API
server. The Python sidecar joins a LiveKit room as the agent participant,
transcribes user audio through the configured Hermes STT path, sends turns to
Hermes, and plays replies through a native OpenAI-compatible TTS service.

## Layout

- `sidecar/livekit_voice_server.py`: Python sidecar and HTTP API.
- `sidecar/static/index.html`: browser voice client.
- `desktop/`: Electron shell for a desktop mic toggle.
- `deploy/macos/`: macOS LaunchAgent and environment template.
- `deploy/livekit/`: LiveKit Docker Compose and config template.
- `docs/`: architecture and TTS backend notes.

Secrets are intentionally not committed. Copy
`deploy/macos/livekit-voice.env.example` to the install host and fill in
LiveKit and Hermes API credentials locally.

## Runtime Dependencies

- Docker and Docker Compose for the packaged install.
- Existing Hermes API server with `/v1/chat/completions`.

The packaged container stack includes:

- Browser WebUI and Python sidecar.
- LiveKit and Redis.
- Kokoro TTS plus Graillon/post-FX chain.
- Packaged local STT fallback.
- Optional packaged local voice-emotion recognition.

## Run Locally

```bash
python3 -m py_compile sidecar/livekit_voice_server.py
python3 sidecar/livekit_voice_server.py --env deploy/macos/livekit-voice.env.example
```

## Container Install

One-line install:

```bash
curl -fsSL https://raw.githubusercontent.com/dreadcorp-labs/hermes-voice/main/packaging/bootstrap.sh | bash
```

Then open `http://localhost:8765/`. If `HERMES_API_KEY` was not provided, the
WebUI starts in first-run setup mode and asks for the Hermes API URL/key.

From a local checkout:

```bash
./packaging/install.sh
```

Desktop shell:

```bash
cd desktop
npm install
npm start
```

Override the desktop client URL with `HERMES_LIVEKIT_DESKTOP_URL`.

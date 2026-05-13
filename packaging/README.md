# Hermes Voice Container Package

The default install target is a single Docker container running:

- Hermes Voice WebUI and LiveKit voice bridge.
- Kokoro voices with Graillon/post-FX chain.
- LiveKit server.
- Redis for LiveKit room state.

The Hermes agent/API server is external. First-run setup connects this voice
stack to an existing Hermes API URL and API key.

The installer uses Docker Compose to build and run the all-in-one voice service
plus a small updater helper. The helper owns Docker access for Settings -> Run
Update, while the voice service only writes a local update request file. Set
`HERMES_VOICE_PACKAGE_MODE=multi` to use the older four-container layout.

## One-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/dreadcorp-labs/hermes-voice/main/packaging/bootstrap.sh | bash
```

Then open:

```text
http://localhost:8765/
```

Useful environment overrides:

```bash
HERMES_VOICE_INSTALL_DIR="$HOME/.hermes-voice" \
HERMES_VOICE_PUBLIC_HOST="localhost" \
HERMES_API_URL="http://host.docker.internal:8642/v1/chat/completions" \
HERMES_API_KEY="..." \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/dreadcorp-labs/hermes-voice/main/packaging/bootstrap.sh)"
```

## Local Install

From this repo:

```bash
./packaging/install.sh
```

Then open:

```text
http://localhost:8765/
```

The same environment overrides work with a local checkout:

```bash
HERMES_VOICE_INSTALL_DIR="$HOME/.hermes-voice" \
HERMES_VOICE_PUBLIC_HOST="localhost" \
HERMES_API_URL="http://host.docker.internal:8642/v1/chat/completions" \
HERMES_API_KEY="..." \
HERMES_API_MODEL="hermes-agent" \
HERMES_API_PROVIDER="hermes" \
./packaging/install.sh
```

If `HERMES_API_KEY` is omitted, the sidecar starts in setup mode and the WebUI
collects the Hermes connection details.

The Hermes API field accepts either a full OpenAI-compatible chat-completions
URL or just `host:port`; `host:port` is expanded to
`http://host:port/v1/chat/completions`.

The WebUI model selector controls the model override sent to Hermes for every
voice turn. The default package starts with `hermes-agent` and then merges the
models advertised by the connected Hermes `/v1/models` endpoint.

The installer records detected LAN CIDRs in `HERMES_DISCOVERY_CIDRS` so the
setup page can search for Hermes on common API ports. Override it when needed:

```bash
HERMES_DISCOVERY_CIDRS="192.168.1.0/24,10.0.0.0/24" ./packaging/install.sh
```

## Browser Microphone Access

Browsers require a secure origin for microphone capture. Use `https://...` or
`http://localhost:...`; raw LAN HTTP URLs such as `http://192.168.x.x:8765`
can load the UI but will block microphone input. Typed chat still works on
plain HTTP.

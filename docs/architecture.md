# Architecture

The packaged install runs LiveKit, Redis, Hermes Voice TTS, and the Python
sidecar from `packaging/compose.yaml`. The Hermes API server remains external:
Hermes Voice connects to it with a configured `/v1/chat/completions` URL and
API key.

The sidecar:

1. Serves the browser client and token endpoint.
2. Joins the LiveKit room as the agent participant.
3. Subscribes to user microphone audio.
4. Segments speech locally and transcribes through Hermes STT tools when
   available, otherwise through the packaged local STT fallback.
5. Sends text to the Hermes API server using the configured session id.
6. Synthesizes replies through the packaged Kokoro/Graillon TTS endpoint.
7. Publishes reply audio back into LiveKit.

The sidecar intentionally does not implement separate calendar or tool routing.
Tool use should be handled by Hermes so behavior stays consistent across voice,
desktop, and text surfaces.

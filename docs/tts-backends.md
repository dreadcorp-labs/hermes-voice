# LiveKit TTS Backends

The main spoken reply path is controlled by:

`HERMES_LIVEKIT_TTS_BACKEND`

Supported packaged value:

- `hermes_voice_native`: calls the configured OpenAI-compatible
  `/v1/audio/speech` endpoint. The package baseline is the bundled `tts`
  container, derived from the current `hermes-voice-dev` service and exposed on
  port `8890`.

- Unknown older backend ids are normalized to `hermes_voice_native` at runtime.
- `kimi_audio` remains an experimental env-only path for Replicate-hosted
  Kimi-Audio when explicitly configured. It is not exposed in the packaged UI.

Useful local checks:

```bash
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8890/health
```

Voice affect:

- `HERMES_EMOTION2VEC_ENABLED=true` enables the packaged local warm
  audio-emotion worker for same-turn emotion labels.
- Emotion recognition is intentionally an on/off product setting. The local
  classifier model is fixed by the app, not exposed as a provider/model choice.
- If local audio emotion times out, the UI reports `unreadable` instead of
  substituting a text-only neutral guess.

Hermes Voice behavior injection:

- `HERMES_LIVEKIT_VOICE_INSTRUCTIONS` is injected by the sidecar as a
  system-message contract on every text or spoken turn.
- The web settings panel exposes the value under `Voice behavior` so it can be
  fine-tuned without editing Hermes itself.

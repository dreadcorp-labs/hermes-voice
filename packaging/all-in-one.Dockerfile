FROM livekit/livekit-server:v1.9.2 AS livekit

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    procps \
    redis-server \
    unzip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=livekit /livekit-server /usr/local/bin/livekit-server

WORKDIR /app

COPY sidecar/requirements.txt /tmp/sidecar-requirements.txt
COPY tts/requirements.txt /tmp/tts-requirements.txt
RUN pip install -r /tmp/sidecar-requirements.txt \
    && pip install -r /tmp/tts-requirements.txt \
    && rm -f /tmp/sidecar-requirements.txt /tmp/tts-requirements.txt

RUN mkdir -p /models \
    && curl -fL --show-error --retry 5 --retry-all-errors --connect-timeout 20 --max-time 600 \
        -o /models/kokoro-v1.0.onnx \
        https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx \
    && curl -fL --show-error --retry 5 --retry-all-errors --connect-timeout 20 --max-time 600 \
        -o /models/voices-v1.0.bin \
        https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

RUN mkdir -p /opt/vst3 \
    && curl -fL --show-error --retry 5 --retry-all-errors --connect-timeout 20 --max-time 600 \
        -o /tmp/Graillon-FREE-3.2.zip \
        https://www.auburnsounds.com/downloads/Graillon-FREE-3.2.zip \
    && unzip -q /tmp/Graillon-FREE-3.2.zip -d /tmp/graillon \
    && find /tmp/graillon -name '*.vst3' -exec cp -R {} /opt/vst3/ \; \
    && rm -rf /tmp/Graillon-FREE-3.2.zip /tmp/graillon

COPY sidecar /app/sidecar
COPY tts/app /app/tts/app
COPY tts/runtime_settings.hermes-voice-dev.json /defaults/runtime_settings.json
COPY packaging/all-in-one-entrypoint.sh /usr/local/bin/hermes-voice-entrypoint

RUN chmod +x /usr/local/bin/hermes-voice-entrypoint \
    && mkdir -p /config /data/cache /data/emotion /data/huggingface /data/redis /data/whisper

ENV HERMES_LIVEKIT_STATIC_DIR=/app/sidecar/static \
    HERMES_EMOTION2VEC_PYTHON=/usr/local/bin/python \
    HERMES_EMOTION2VEC_HELPER=/app/sidecar/wav2vec_emotion_analyze.py \
    HERMES_EMOTION2VEC_PYTHONPATH= \
    HERMES_EMOTION2VEC_CACHE_DIR=/data/emotion \
    GLAADOS_DEFAULT_VOICE=bf_emma \
    GLAADOS_DEFAULT_LANG=en-gb \
    GLAADOS_MODEL_PATH=/models/kokoro-v1.0.onnx \
    GLAADOS_VOICES_PATH=/models/voices-v1.0.bin \
    GLAADOS_DEFAULT_SETTINGS_PATH=/defaults/runtime_settings.json \
    GLAADOS_PLUGIN_DIR=/opt/vst3 \
    GLAADOS_KOKORO_ONLY=true \
    GLAADOS_TTS_BACKEND=kokoro_glados \
    GLAADOS_INWORLD_DEFAULT_VOICE=Wendy \
    GLAADOS_INWORLD_DEFAULT_MODEL=inworld-tts-1.5-max \
    HF_HOME=/data/huggingface

VOLUME ["/config", "/data"]

EXPOSE 8765 7880 7881 8890

ENTRYPOINT ["hermes-voice-entrypoint"]

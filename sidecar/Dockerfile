FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY sidecar/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY sidecar/livekit_voice_server.py /app/livekit_voice_server.py
COPY sidecar/wav2vec_emotion_analyze.py /app/wav2vec_emotion_analyze.py
COPY sidecar/emotion2vec_analyze.py /app/emotion2vec_analyze.py
COPY sidecar/static /app/static

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /config /data/emotion /data/huggingface /data/whisper \
    && chown -R appuser:appuser /app /config /data

USER appuser

ENV HERMES_LIVEKIT_STATIC_DIR=/app/static \
    HERMES_EMOTION2VEC_PYTHON=/usr/local/bin/python \
    HERMES_EMOTION2VEC_HELPER=/app/wav2vec_emotion_analyze.py \
    HERMES_EMOTION2VEC_PYTHONPATH= \
    HERMES_EMOTION2VEC_CACHE_DIR=/data/emotion \
    HF_HOME=/data/huggingface

EXPOSE 8765

CMD ["python", "/app/livekit_voice_server.py", "--env", "/config/hermes-voice.env"]

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    ffmpeg \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

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

COPY app /app/app
COPY runtime_settings.hermes-voice-dev.json /defaults/runtime_settings.json

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/cache /data/huggingface \
    && chown -R appuser:appuser /app /data /models /defaults

USER appuser

ENV GLAADOS_DEFAULT_VOICE=bf_emma \
    GLAADOS_DEFAULT_LANG=en-gb \
    GLAADOS_MODEL_PATH=/models/kokoro-v1.0.onnx \
    GLAADOS_VOICES_PATH=/models/voices-v1.0.bin \
    GLAADOS_DEFAULT_SETTINGS_PATH=/defaults/runtime_settings.json \
    GLAADOS_PLUGIN_DIR=/opt/vst3 \
    HF_HOME=/data/huggingface

EXPOSE 8889

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8889"]

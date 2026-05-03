#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import contextlib
import datetime
import hmac
import ipaddress
import io
import json
import logging
import os
import re
import resource
import signal
import socket
import sys
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from shutil import copyfile
from shutil import which
from typing import Any
from urllib import error as urllib_error
from urllib.parse import urlsplit, urlunsplit
from urllib import request as urllib_request

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from livekit import api, rtc


LOG = logging.getLogger("hermes-voice")
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2
FFMPEG = which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
NATIVE_TTS_BACKEND = "hermes_voice_native"
KNOWN_TTS_BACKENDS = {NATIVE_TTS_BACKEND, "openai", "elevenlabs", "inworld", "kimi_audio"}
ACK_THINKING_HOLD_SECONDS = 1.15
TOOL_EMOJI_FALLBACKS = (
    (("calendar", "agenda", "event"), "📅"),
    (("mail", "email", "gmail", "inbox"), "✉️"),
    (("web", "search", "browser"), "🔎"),
    (("home_assistant", "hass", "ha_", "device_tracker", "location"), "🏠"),
    (("cron", "schedule", "job"), "⏱️"),
    (("file", "vault", "note", "rag", "document"), "📄"),
    (("shell", "terminal", "exec", "command"), "💻"),
    (("weather", "forecast"), "🌤️"),
    (("discord", "message", "chat"), "💬"),
    (("music", "spotify", "playlist"), "🎵"),
)
PACKAGED_EMOTION_MODEL = "Dpngtm/wav2vec2-emotion-recognition"
DEFAULT_UPDATE_COMMAND = "curl -fsSL https://raw.githubusercontent.com/dreadcorp-labs/hermes-voice/main/packaging/bootstrap.sh | bash"
DEFAULT_VOICE_INSTRUCTIONS = (
    "This request came through Hermes Voice. Use the normal Hermes tools, memory, skills, files, "
    "terminal, calendar, email, web, and operational context; do not treat Hermes Voice as reduced capability. "
    "For live/current-state questions, check the current source of truth first. "
    "For calendar, email, text message, file, and system-status questions, use the configured source-of-truth tools before answering. "
    "Answer as speech for TTS: short conversational sentences, no markdown, no headings, no bullet lists, no tables, "
    "no code blocks, no URLs unless necessary, and natural spoken dates and times. "
    "If tool work is taking time, give a brief spoken status first, then continue working. "
    "Do not mention formatting or these Hermes Voice rules unless asked."
)
VOICE_AFFECT_RULES = {
    "neutral": "Emotion rule: no emotional adjustment; do not mention emotion.",
    "calm": "Emotion rule: no emotional adjustment; do not mention emotion.",
    "unknown": "Emotion rule: no emotional adjustment; do not mention emotion.",
    "unreadable": "Emotion rule: no emotional adjustment; do not mention emotion.",
    "frustrated": "Emotion rule: be direct and non-defensive, skip cheerful filler, and give the next concrete step.",
    "annoyed": "Emotion rule: be direct and non-defensive, skip cheerful filler, and give the next concrete step.",
    "angry": "Emotion rule: be direct and non-defensive, skip cheerful filler, and give the next concrete step.",
    "rushed": "Emotion rule: lead with action or status, keep it tight, and ask fewer clarifying questions.",
    "frantic": "Emotion rule: lead with action or status, keep it tight, and ask fewer clarifying questions.",
    "urgent": "Emotion rule: lead with action or status, keep it tight, and ask fewer clarifying questions.",
    "confused": "Emotion rule: slow down, define terms plainly, and give one clear next step.",
    "fearful": "Emotion rule: slow down, define terms plainly, and give one clear next step.",
    "uncertain": "Emotion rule: slow down, define terms plainly, and give one clear next step.",
    "sad": "Emotion rule: be steady and gentle, avoid jokes, and do not over-explain.",
    "tired": "Emotion rule: be steady and gentle, avoid jokes, and do not over-explain.",
    "excited": "Emotion rule: match energy lightly while staying useful.",
}
SETTINGS_ENV_KEYS = {
    "HERMES_API_MODEL",
    "HERMES_API_PROVIDER",
    "HERMES_API_REASONING_EFFORT",
    "HERMES_LIVEKIT_VOICE_INSTRUCTIONS",
    "HERMES_LIVEKIT_TTS_BACKEND",
    "HERMES_LIVEKIT_TTS_VOICE",
    "HERMES_LIVEKIT_TTS_SPEED",
    "HERMES_LIVEKIT_STT_PROVIDER",
    "HERMES_LIVEKIT_STT_MODEL",
    "HERMES_EMOTION2VEC_ENABLED",
}
SETUP_ENV_KEYS = SETTINGS_ENV_KEYS | {
    "HERMES_API_URL",
    "HERMES_API_KEY",
    "API_SERVER_KEY",
    "HERMES_SETUP_TOKEN",
    "LIVEKIT_PUBLIC_URL",
    "HERMES_LIVEKIT_TTS_URL",
    "HERMES_LIVEKIT_STT_PROVIDER",
    "HERMES_LIVEKIT_STT_MODEL",
}
DEFAULT_MODEL_CHOICES = (
    {"id": "kimi-k2.6", "name": "Kimi K2.6", "provider": "kimi-coding"},
    {"id": "kimi-k2.5", "name": "Kimi K2.5", "provider": "kimi-coding"},
    {"id": "hermes-agent", "name": "Hermes default", "provider": "hermes"},
    {"id": "gpt-5.4-mini", "name": "Hermes fast", "provider": "hermes"},
)
TTS_CHOICES = (
    {"id": NATIVE_TTS_BACKEND, "name": "Hermes Voice Native"},
    {"id": "openai", "name": "OpenAI"},
    {"id": "elevenlabs", "name": "ElevenLabs"},
    {"id": "inworld", "name": "Inworld"},
)
STT_CHOICES = (
    {"id": "auto", "name": "Hermes Voice Native"},
    {"id": "openai", "name": "OpenAI"},
    {"id": "elevenlabs", "name": "ElevenLabs"},
    {"id": "inworld", "name": "Inworld"},
)
DEFAULT_DISCOVERY_PORTS = (8642, 8000, 8080, 1235)
DEFAULT_DISCOVERY_HOSTS = (
    "host.docker.internal",
    "hermes.local",
    "hermes",
)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("\"", "'")) and value.endswith(("\"", "'")):
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
            value = str(value)
            if value.startswith(("\"", "'")) and value.endswith(("\"", "'")):
                value = value[1:-1]
        if key:
            values[key] = value
    return values


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_hermes_api_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw):
        raw = "http://" + raw
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif path == "/v1":
        path = "/v1/chat/completions"
    elif path.endswith("/v1"):
        path = path + "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _models_url_for_api_url(value: str) -> str:
    parts = urlsplit(_normalize_hermes_api_url(value))
    path = parts.path
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")] + "/models"
    elif path.endswith("/completions"):
        path = path[: -len("/completions")] + "/models"
    elif path.rstrip("/").endswith("/v1"):
        path = path.rstrip("/") + "/models"
    else:
        path = "/v1/models"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _parse_discovery_ports(value: str) -> list[int]:
    ports: list[int] = []
    for part in _split_csv(value):
        with contextlib.suppress(ValueError):
            port = int(part)
            if 0 < port < 65536 and port not in ports:
                ports.append(port)
    return ports or list(DEFAULT_DISCOVERY_PORTS)


def _normalize_model_choice(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        model_id = item.strip()
        if not model_id:
            return None
        return {"id": model_id, "name": model_id, "provider": _default_provider_for_model(model_id)}
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or item.get("model") or "").strip()
    if not model_id:
        return None
    name = str(item.get("name") or item.get("label") or model_id).strip()
    provider = str(item.get("provider") or _default_provider_for_model(model_id)).strip()
    return {"id": model_id, "name": name or model_id, "provider": provider or "hermes"}


def _default_provider_for_model(model_id: str) -> str:
    model = model_id.strip().lower()
    if model.startswith("kimi-") or model.startswith("moonshot"):
        return "kimi-coding"
    return "hermes"


def _parse_model_choices(value: str) -> list[dict[str, str]]:
    raw = value.strip()
    if not raw:
        return []
    choices: list[dict[str, str]] = []
    with contextlib.suppress(json.JSONDecodeError):
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                choice = _normalize_model_choice(item)
                if choice:
                    choices.append(choice)
            return choices
    for part in _split_csv(raw):
        fields = [field.strip() for field in part.split("|")]
        model_id = fields[0] if fields else ""
        if not model_id:
            continue
        name = fields[1] if len(fields) > 1 and fields[1] else model_id
        provider = fields[2] if len(fields) > 2 and fields[2] else _default_provider_for_model(model_id)
        choices.append({"id": model_id, "name": name, "provider": provider})
    return choices


def _dedupe_model_choices(choices: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in choices:
        choice = _normalize_model_choice(raw)
        if not choice or choice["id"] in seen:
            continue
        deduped.append(choice)
        seen.add(choice["id"])
    return deduped


def _quote_env_value(value: Any) -> str:
    text = str(value)
    if not text or re.search(r"\s|#|['\"\\]", text):
        return json.dumps(text)
    return text


def _write_env_updates(path: Path, updates: dict[str, Any], allowed_keys: set[str] = SETTINGS_ENV_KEYS) -> None:
    safe_updates = {key: str(value) for key, value in updates.items() if key in allowed_keys}
    if not safe_updates:
        return
    lines = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    next_lines: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            next_lines.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        if key in safe_updates:
            next_lines.append(f"{key}={_quote_env_value(safe_updates[key])}")
            seen.add(key)
        else:
            next_lines.append(raw_line)
    for key, value in safe_updates.items():
        if key not in seen:
            next_lines.append(f"{key}={_quote_env_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(next_lines).rstrip() + "\n")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _normalize_tts_backend(value: str) -> str:
    backend = str(value or "").strip().lower()
    if not backend:
        return ""
    if backend in KNOWN_TTS_BACKENDS:
        return backend
    return NATIVE_TTS_BACKEND


def _raise_nofile_limit(target_soft: int = 4096) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= target_soft:
            return
        new_soft = min(target_soft, hard if hard > 0 else target_soft)
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        LOG.info("raised RLIMIT_NOFILE soft limit from %s to %s", soft, new_soft)
    except Exception as exc:
        LOG.warning("could not raise RLIMIT_NOFILE: %s", exc)


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _attachments_prompt(attachments: Any) -> str:
    if not isinstance(attachments, list):
        return ""
    parts: list[str] = []
    for index, item in enumerate(attachments[:5], start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"attachment-{index}").strip()[:120]
        media_type = str(item.get("type") or "application/octet-stream").strip()[:80]
        size = _clamp_int(item.get("size"), 0, 0, 10_000_000)
        text = str(item.get("text") or "").strip()
        if text:
            if len(text) > 16_000:
                text = text[:16_000] + "\n[truncated]"
            parts.append(f"Attachment {index}: {name} ({media_type}, {size} bytes)\n{text}")
        else:
            parts.append(f"Attachment {index}: {name} ({media_type}, {size} bytes). Content was not extracted by the voice client.")
    if not parts:
        return ""
    return "\n\nAttached files for this typed turn:\n\n" + "\n\n---\n\n".join(parts)


@dataclass
class Settings:
    env_path: Path = Path.home() / ".hermes/livekit-voice/.env"
    host: str = "127.0.0.1"
    port: int = 8765
    static_dir: Path = Path(__file__).with_name("static")
    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_public_url: str = "ws://127.0.0.1:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_room: str = "hermes-voice"
    setup_token: str = ""
    agent_identity: str = "hermes-livekit-agent"
    agent_name: str = "Hermes Voice"
    hermes_api_url: str = "http://127.0.0.1:8642/v1/chat/completions"
    hermes_api_key: str = ""
    hermes_session_id: str = "livekit-voice-main"
    hermes_model: str = "kimi-k2.6"
    hermes_provider: str = ""
    model_choices: list[dict[str, str]] = field(default_factory=lambda: list(DEFAULT_MODEL_CHOICES))
    hermes_reasoning_effort: str = "none"
    voice_instructions: str = DEFAULT_VOICE_INSTRUCTIONS
    speech_rms_threshold: int = 420
    silence_seconds: float = 1.05
    min_speech_seconds: float = 0.45
    max_speech_seconds: float = 25.0
    livekit_tts_backend: str = NATIVE_TTS_BACKEND
    livekit_tts_url: str = "http://127.0.0.1:8890/v1/audio/speech"
    livekit_tts_model: str = "tts-1"
    livekit_tts_voice: str = ""
    livekit_tts_response_format: str = "mp3"
    livekit_tts_markup: str = "inworld"
    livekit_tts_speed: float = 1.0
    livekit_tts_timeout_seconds: float = 30.0
    stt_provider: str = "auto"
    stt_model: str = ""
    hermes_streaming: bool = True
    kimi_affect_enabled: bool = True
    kimi_api_url: str = "https://api.moonshot.ai/v1/chat/completions"
    kimi_api_key: str = ""
    kimi_model: str = "kimi-k2.6"
    kimi_affect_timeout_seconds: float = 2.5
    kimi_affect_wait_seconds: float = 0.8
    kimi_audio_enabled: bool = False
    kimi_audio_provider: str = "replicate"
    kimi_audio_replicate_token: str = ""
    kimi_audio_replicate_model: str = "zsxkib/kimi-audio-7b-instruct"
    kimi_audio_replicate_version: str = "7500b32387695e89da3d09271850319ba027969f0c714dfc226361609ff29f2b"
    kimi_audio_replicate_url: str = "https://api.replicate.com/v1/predictions"
    kimi_audio_timeout_seconds: float = 90.0
    kimi_audio_max_clip_seconds: float = 12.0
    emotion2vec_enabled: bool = True
    emotion2vec_python: str = str(Path.home() / ".hermes/hermes-agent/venv/bin/python")
    emotion2vec_helper: Path = Path(__file__).with_name("emotion2vec_analyze.py")
    emotion2vec_model: str = PACKAGED_EMOTION_MODEL
    emotion2vec_pythonpath: str = str(Path.home() / ".hermes/emotion2vec/pythonpath")
    emotion2vec_cache_dir: Path = Path.home() / ".hermes/emotion2vec/cache"
    emotion2vec_timeout_seconds: float = 8.0
    local_stt_model: str = "base.en"
    discovery_cidrs: list[str] = field(default_factory=list)
    discovery_ports: list[int] = field(default_factory=lambda: list(DEFAULT_DISCOVERY_PORTS))
    discovery_max_hosts: int = 512
    version: str = "dev"
    update_repo_api_url: str = "https://api.github.com/repos/dreadcorp-labs/hermes-voice/commits/main"
    update_command: str = ""
    update_display_command: str = DEFAULT_UPDATE_COMMAND

    @classmethod
    def load(cls, env_path: Path) -> "Settings":
        hermes_env = _load_env_file(Path.home() / ".hermes/.env")
        sidecar_env = _load_env_file(env_path)
        for key, value in hermes_env.items():
            os.environ.setdefault(key, value)
        for key, value in sidecar_env.items():
            os.environ[key] = value

        livekit_url = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")

        return cls(
            env_path=env_path,
            host=_env("HERMES_LIVEKIT_VOICE_HOST", "127.0.0.1"),
            port=int(_env("HERMES_LIVEKIT_VOICE_PORT", "8765")),
            static_dir=Path(_env("HERMES_LIVEKIT_STATIC_DIR", str(Path(__file__).with_name("static")))),
            livekit_url=livekit_url,
            livekit_public_url=_env("LIVEKIT_PUBLIC_URL", livekit_url),
            livekit_api_key=_env("LIVEKIT_API_KEY"),
            livekit_api_secret=_env("LIVEKIT_API_SECRET"),
            livekit_room=_env("LIVEKIT_ROOM", "hermes-voice"),
            setup_token=_env("HERMES_SETUP_TOKEN"),
            agent_identity=_env("HERMES_LIVEKIT_AGENT_IDENTITY", "hermes-livekit-agent"),
            agent_name=_env("HERMES_LIVEKIT_AGENT_NAME", "Hermes Voice"),
            hermes_api_url=_normalize_hermes_api_url(_env("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")),
            hermes_api_key=_env("HERMES_API_KEY", _env("API_SERVER_KEY")),
            hermes_session_id=_env("HERMES_SESSION_ID", "livekit-voice-main"),
            hermes_model=_env("HERMES_API_MODEL", "kimi-k2.6"),
            hermes_provider=_env("HERMES_API_PROVIDER", ""),
            model_choices=_dedupe_model_choices(
                [*DEFAULT_MODEL_CHOICES, *_parse_model_choices(_env("HERMES_LIVEKIT_MODEL_CHOICES", ""))]
            ),
            hermes_reasoning_effort=_env("HERMES_API_REASONING_EFFORT", "none"),
            voice_instructions=_env("HERMES_LIVEKIT_VOICE_INSTRUCTIONS", DEFAULT_VOICE_INSTRUCTIONS),
            speech_rms_threshold=int(_env("HERMES_LIVEKIT_RMS_THRESHOLD", "420")),
            silence_seconds=float(_env("HERMES_LIVEKIT_SILENCE_SECONDS", "1.05")),
            min_speech_seconds=float(_env("HERMES_LIVEKIT_MIN_SPEECH_SECONDS", "0.45")),
            max_speech_seconds=float(_env("HERMES_LIVEKIT_MAX_SPEECH_SECONDS", "25.0")),
            livekit_tts_backend=_normalize_tts_backend(_env("HERMES_LIVEKIT_TTS_BACKEND", NATIVE_TTS_BACKEND)),
            livekit_tts_url=_env("HERMES_LIVEKIT_TTS_URL", "http://127.0.0.1:8890/v1/audio/speech"),
            livekit_tts_model=_env("HERMES_LIVEKIT_TTS_MODEL", "tts-1"),
            livekit_tts_voice=_env("HERMES_LIVEKIT_TTS_VOICE", ""),
            livekit_tts_response_format=_env("HERMES_LIVEKIT_TTS_RESPONSE_FORMAT", "mp3"),
            livekit_tts_markup=_env("HERMES_LIVEKIT_TTS_MARKUP", "inworld"),
            livekit_tts_speed=float(_env("HERMES_LIVEKIT_TTS_SPEED", "1.0")),
            livekit_tts_timeout_seconds=float(_env("HERMES_LIVEKIT_TTS_TIMEOUT_SECONDS", "30")),
            stt_provider=_env("HERMES_LIVEKIT_STT_PROVIDER", "auto"),
            stt_model=_env("HERMES_LIVEKIT_STT_MODEL", ""),
            hermes_streaming=_env("HERMES_LIVEKIT_HERMES_STREAMING", "true").lower() not in {"0", "false", "no"},
            kimi_affect_enabled=_env("HERMES_KIMI_AFFECT_ENABLED", "true").lower() not in {"0", "false", "no"},
            kimi_api_url=_env("HERMES_KIMI_API_URL", "https://api.moonshot.ai/v1/chat/completions"),
            kimi_api_key=_env("HERMES_KIMI_API_KEY", _env("MOONSHOT_API_KEY", _env("KIMI_API_KEY"))),
            kimi_model=_env("HERMES_KIMI_MODEL", "kimi-k2.6"),
            kimi_affect_timeout_seconds=float(_env("HERMES_KIMI_AFFECT_TIMEOUT_SECONDS", "2.5")),
            kimi_affect_wait_seconds=float(_env("HERMES_KIMI_AFFECT_WAIT_SECONDS", "0.8")),
            kimi_audio_enabled=_env("HERMES_KIMI_AUDIO_ENABLED", "false").lower() not in {"0", "false", "no"},
            kimi_audio_provider=_env("HERMES_KIMI_AUDIO_PROVIDER", "replicate"),
            kimi_audio_replicate_token=_env("HERMES_KIMI_AUDIO_REPLICATE_TOKEN", _env("REPLICATE_API_TOKEN")),
            kimi_audio_replicate_model=_env("HERMES_KIMI_AUDIO_REPLICATE_MODEL", "zsxkib/kimi-audio-7b-instruct"),
            kimi_audio_replicate_version=_env(
                "HERMES_KIMI_AUDIO_REPLICATE_VERSION",
                "7500b32387695e89da3d09271850319ba027969f0c714dfc226361609ff29f2b",
            ),
            kimi_audio_replicate_url=_env(
                "HERMES_KIMI_AUDIO_REPLICATE_URL",
                "https://api.replicate.com/v1/predictions",
            ),
            kimi_audio_timeout_seconds=float(_env("HERMES_KIMI_AUDIO_TIMEOUT_SECONDS", "90")),
            kimi_audio_max_clip_seconds=float(_env("HERMES_KIMI_AUDIO_MAX_CLIP_SECONDS", "12")),
            emotion2vec_enabled=_env("HERMES_EMOTION2VEC_ENABLED", "true").lower() not in {"0", "false", "no"},
            emotion2vec_python=_env("HERMES_EMOTION2VEC_PYTHON", str(Path.home() / ".hermes/hermes-agent/venv/bin/python")),
            emotion2vec_helper=Path(
                _env("HERMES_EMOTION2VEC_HELPER", str(Path(__file__).with_name("emotion2vec_analyze.py")))
            ),
            emotion2vec_model=PACKAGED_EMOTION_MODEL,
            emotion2vec_pythonpath=_env(
                "HERMES_EMOTION2VEC_PYTHONPATH",
                str(Path.home() / ".hermes/emotion2vec/pythonpath"),
            ),
            emotion2vec_cache_dir=Path(
                _env("HERMES_EMOTION2VEC_CACHE_DIR", str(Path.home() / ".hermes/emotion2vec/cache"))
            ),
            emotion2vec_timeout_seconds=float(_env("HERMES_EMOTION2VEC_TIMEOUT_SECONDS", "8.0")),
            local_stt_model=_env("HERMES_LIVEKIT_LOCAL_STT_MODEL", "base.en"),
            discovery_cidrs=_split_csv(_env("HERMES_DISCOVERY_CIDRS", "")),
            discovery_ports=_parse_discovery_ports(_env("HERMES_DISCOVERY_PORTS", ",".join(str(port) for port in DEFAULT_DISCOVERY_PORTS))),
            discovery_max_hosts=int(_env("HERMES_DISCOVERY_MAX_HOSTS", "512")),
            version=_env("HERMES_VOICE_VERSION", "dev"),
            update_repo_api_url=_env(
                "HERMES_VOICE_UPDATE_REPO_API_URL",
                "https://api.github.com/repos/dreadcorp-labs/hermes-voice/commits/main",
            ),
            update_command=_env("HERMES_VOICE_UPDATE_COMMAND", ""),
            update_display_command=_env("HERMES_VOICE_UPDATE_DISPLAY_COMMAND", DEFAULT_UPDATE_COMMAND),
        )

    def missing_required_settings(self) -> list[str]:
        missing = []
        for name, value in (
            ("LIVEKIT_API_KEY", self.livekit_api_key),
            ("LIVEKIT_API_SECRET", self.livekit_api_secret),
            ("HERMES_API_KEY/API_SERVER_KEY", self.hermes_api_key),
        ):
            if not value:
                missing.append(name)
        return missing

    def setup_required(self) -> bool:
        return bool(self.missing_required_settings())


@dataclass
class RuntimeStatus:
    connected: bool = False
    room: str = ""
    participants: int = 0
    agent_state: str = "starting"
    last_transcript: str = ""
    last_reply: str = ""
    last_error: str = ""
    last_turn_at: float = 0.0
    last_voice_affect: dict[str, Any] | None = None
    last_voice_prosody: dict[str, Any] | None = None
    last_local_emotion: dict[str, Any] | None = None
    last_kimi_audio_status: dict[str, Any] | None = None
    last_timings: dict[str, float] = field(default_factory=dict)
    recent_turns: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "room": self.room,
            "participants": self.participants,
            "agent_state": self.agent_state,
            "last_transcript": self.last_transcript,
            "last_reply": self.last_reply,
            "last_error": self.last_error,
            "last_turn_at": self.last_turn_at,
            "last_voice_affect": self.last_voice_affect,
            "last_voice_prosody": self.last_voice_prosody,
            "last_local_emotion": self.last_local_emotion,
            "last_kimi_audio_status": self.last_kimi_audio_status,
            "last_timings": self.last_timings,
            "recent_turns": self.recent_turns[-5:],
        }


class HermesLiveKitVoice:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = RuntimeStatus(room=settings.livekit_room)
        self.room: rtc.Room | None = None
        self.audio_source: rtc.AudioSource | None = None
        self._tasks: set[asyncio.Task] = set()
        self._track_tasks: dict[str, set[asyncio.Task]] = {}
        self._turn_lock = asyncio.Lock()
        self._speaking = asyncio.Event()
        self._interrupt_requested = asyncio.Event()
        self._stopping = False
        self._last_transcript_norm = ""
        self._last_transcript_at = 0.0
        self._latest_voice_affect: dict[str, Any] | None = None
        self._emotion2vec_lock = asyncio.Lock()
        self._emotion2vec_start_lock = asyncio.Lock()
        self._emotion2vec_ready = False
        self._emotion2vec_proc: asyncio.subprocess.Process | None = None
        self._local_whisper_model: Any | None = None
        self._local_whisper_model_name = ""
        self._last_update_status: dict[str, Any] | None = None

        hermes_agent = Path.home() / ".hermes/hermes-agent"
        if str(hermes_agent) not in sys.path:
            sys.path.insert(0, str(hermes_agent))

    def make_token(self, identity: str, name: str, room: str | None = None, *, role: str = "user") -> str:
        target_room = room or self.settings.livekit_room
        if target_room != self.settings.livekit_room:
            raise ValueError("Invalid LiveKit room")
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,96}", identity):
            raise ValueError("Invalid LiveKit identity")
        token = (
            api.AccessToken(self.settings.livekit_api_key, self.settings.livekit_api_secret)
            .with_identity(identity[:96])
            .with_name(name[:80])
        )
        if hasattr(token, "with_ttl"):
            try:
                ttl_token = token.with_ttl(datetime.timedelta(hours=1))
            except TypeError:
                ttl_token = token.with_ttl(3600)
            if ttl_token is not None:
                token = ttl_token
        grants = api.VideoGrants(
            room_join=True,
            room=target_room,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_update_own_metadata=(role == "agent"),
        )
        return token.with_grants(grants).to_jwt()

    @staticmethod
    def _setup_auth_token_from_request(request: web.Request) -> str:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (
            request.headers.get("X-Hermes-Setup-Token", "")
            or request.query.get("setupToken", "")
            or request.query.get("setup_token", "")
        ).strip()

    def require_setup_token(self, request: web.Request) -> None:
        expected = self.settings.setup_token.strip()
        if not expected:
            return
        provided = self._setup_auth_token_from_request(request)
        if not provided or not hmac.compare_digest(provided, expected):
            raise web.HTTPUnauthorized(text="Missing or invalid setup token")

    def setup_token_required(self) -> bool:
        return bool(self.settings.setup_token.strip())

    @staticmethod
    def _private_discovery_network(network: ipaddress.IPv4Network) -> bool:
        return (
            network.is_private
            or network.is_loopback
            or network.is_link_local
        )

    async def start(self) -> None:
        self._validate_settings()
        self.room = rtc.Room()

        @self.room.on("participant_connected")
        def _on_participant_connected(participant):
            LOG.info("participant connected: %s", participant.identity)
            self._update_participant_count()

        @self.room.on("participant_disconnected")
        def _on_participant_disconnected(participant):
            LOG.info("participant disconnected: %s", participant.identity)
            self._cancel_participant_tasks(participant.identity)
            self._update_participant_count()

        @self.room.on("track_subscribed")
        def _on_track_subscribed(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity != self.settings.agent_identity:
                LOG.info("subscribed to audio from %s", participant.identity)
                self._cancel_participant_tasks(participant.identity)
                task = self._spawn(self._consume_audio_track(track, participant.identity))
                self._track_tasks.setdefault(participant.identity, set()).add(task)
                task.add_done_callback(
                    lambda done, identity=participant.identity: self._track_tasks.get(identity, set()).discard(done)
                )

        @self.room.on("data_received")
        def _on_data_received(packet):
            self._handle_data_packet(packet)

        token = self.make_token(self.settings.agent_identity, self.settings.agent_name, role="agent")
        await self.room.connect(self.settings.livekit_url, token)
        self.status.connected = True
        self._update_participant_count()

        self.audio_source = rtc.AudioSource(SAMPLE_RATE, CHANNELS, queue_size_ms=1200)
        audio_track = rtc.LocalAudioTrack.create_audio_track("hermes-voice-audio", self.audio_source)
        await self.room.local_participant.publish_track(audio_track)
        await self._publish_state("listening")
        if self._emotion2vec_available():
            self._spawn(self._ensure_emotion2vec_worker())
        LOG.info("connected to LiveKit room %s at %s", self.settings.livekit_room, self.settings.livekit_url)

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._emotion2vec_proc and self._emotion2vec_proc.returncode is None:
            self._emotion2vec_proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._emotion2vec_proc.wait(), timeout=2.0)
        self._emotion2vec_ready = False
        if self.room:
            await self.room.disconnect()
        self.status.connected = False
        self.status.agent_state = "disconnected"

    def _validate_settings(self) -> None:
        missing = self.settings.missing_required_settings()
        if missing:
            raise RuntimeError("Missing required setting(s): " + ", ".join(missing))

    def _model_provider(self) -> str:
        return self._provider_for_model(self.settings.hermes_model)

    def _provider_for_model(self, model: str) -> str:
        if self.settings.hermes_provider.strip() and model == self.settings.hermes_model:
            return self.settings.hermes_provider.strip()
        for choice in self.settings.model_choices:
            if choice.get("id") == model:
                return str(choice.get("provider") or "hermes").strip() or "hermes"
        return _default_provider_for_model(model)

    def _hermes_provider_override(self, model: str | None = None) -> str:
        provider = self._provider_for_model((model or self.settings.hermes_model).strip())
        if provider.lower() in {"", "auto", "default", "hermes", "hermes-agent"}:
            return ""
        return provider

    def _initial_update_status(self) -> dict[str, Any]:
        return {
            "current": self.settings.version,
            "latest": "",
            "latestFull": "",
            "available": False,
            "checkedAt": 0,
            "runAvailable": bool(self.settings.update_command.strip()),
            "displayCommand": self.settings.update_display_command,
            "running": False,
            "message": "Not checked",
        }

    async def refresh_remote_model_choices(self) -> None:
        if not self.settings.hermes_api_url or not self.settings.hermes_api_key:
            return
        models_url = _models_url_for_api_url(self.settings.hermes_api_url)
        headers = {"Authorization": f"Bearer {self.settings.hermes_api_key}"}
        timeout = ClientTimeout(total=5, connect=2, sock_read=3)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(models_url, headers=headers) as response:
                    if response.status >= 400:
                        return
                    payload = await response.json()
        except Exception:
            return
        models: list[dict[str, str]] = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("id"):
                model_id = str(item["id"]).strip()
                if model_id:
                    models.append(
                        {
                            "id": model_id,
                            "name": str(item.get("name") or model_id),
                            "provider": str(item.get("provider") or _default_provider_for_model(model_id)),
                        }
                    )
        if models:
            self.settings.model_choices = _dedupe_model_choices([*self.settings.model_choices, *models])

    async def check_update(self) -> dict[str, Any]:
        status = self._initial_update_status()
        current = self.settings.version.strip()
        timeout = ClientTimeout(total=10, connect=3, sock_read=5)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    self.settings.update_repo_api_url,
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "hermes-voice"},
                ) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"update check HTTP {response.status}: {text[:240]}")
                    payload = json.loads(text)
        except Exception as exc:
            status.update(
                {
                    "checkedAt": time.time(),
                    "message": f"Update check failed: {type(exc).__name__}",
                    "error": str(exc)[:300],
                }
            )
            self._last_update_status = status
            return status

        latest = str(payload.get("sha") or "").strip()
        latest_short = latest[:7] if latest else ""
        current_short = current[:7] if current and current != "dev" else current
        available = bool(latest_short and current_short and latest_short != current_short)
        status.update(
            {
                "current": current_short,
                "latest": latest_short,
                "latestFull": latest,
                "available": available,
                "checkedAt": time.time(),
                "message": "Update available" if available else "Up to date",
            }
        )
        self._last_update_status = status
        return status

    async def run_update(self) -> dict[str, Any]:
        command = self.settings.update_command.strip()
        if not command:
            raise web.HTTPServiceUnavailable(text="In-app update is not configured for this install")
        status = dict(self._last_update_status or self._initial_update_status())
        status.update({"running": True, "message": "Update running", "checkedAt": time.time()})
        self._last_update_status = status
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "HERMES_VOICE_UPDATE_FROM_WEBUI": "1"},
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError as exc:
            status.update({"running": False, "ok": False, "message": "Update timed out", "error": str(exc)})
            self._last_update_status = status
            return status
        except Exception as exc:
            status.update(
                {
                    "running": False,
                    "ok": False,
                    "message": f"Update failed: {type(exc).__name__}",
                    "error": str(exc)[:500],
                }
            )
            self._last_update_status = status
            return status

        output = (stdout or b"").decode("utf-8", errors="replace")
        success_message = "Update finished"
        if proc.returncode == 0 and "Update requested" in output:
            success_message = "Update requested; Hermes Voice will restart if changes are available"
        status.update(
            {
                "running": False,
                "ok": proc.returncode == 0,
                "message": success_message if proc.returncode == 0 else f"Update failed with exit {proc.returncode}",
                "exitCode": proc.returncode,
                "outputTail": output[-4000:],
            }
        )
        self._last_update_status = status
        return status

    def _settings_payload(self) -> dict[str, Any]:
        model_choices = _dedupe_model_choices(
            [*self.settings.model_choices, {"id": self.settings.hermes_model, "name": self.settings.hermes_model, "provider": self._model_provider()}]
        )
        return {
            "model": self.settings.hermes_model,
            "modelProvider": self._model_provider(),
            "reasoningEffort": self.settings.hermes_reasoning_effort,
            "voiceInstructions": self.settings.voice_instructions,
            "defaultVoiceInstructions": DEFAULT_VOICE_INSTRUCTIONS,
            "ttsBackend": self.settings.livekit_tts_backend,
            "ttsVoice": self.settings.livekit_tts_voice,
            "ttsSpeed": self.settings.livekit_tts_speed,
            "sttProvider": self.settings.stt_provider,
            "sttModel": self.settings.stt_model,
            "emotionRecognition": self.settings.emotion2vec_enabled,
            "emotionAvailable": self._emotion2vec_available(),
            "kimiApiAvailable": bool(self.settings.kimi_api_key),
            "version": self.settings.version,
            "update": self._last_update_status or self._initial_update_status(),
            "choices": {
                "models": model_choices,
                "ttsBackends": TTS_CHOICES,
                "sttProviders": STT_CHOICES,
            },
        }

    def _apply_settings_update(self, body: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        model = str(body.get("model") or "").strip()
        if model:
            allowed_models = {choice["id"] for choice in self.settings.model_choices}
            if model not in allowed_models:
                self.settings.model_choices = _dedupe_model_choices(
                    [*self.settings.model_choices, {"id": model, "name": model, "provider": _default_provider_for_model(model)}]
                )
            self.settings.hermes_model = model
            self.settings.hermes_provider = self._provider_for_model(model)
            updates["HERMES_API_MODEL"] = model
            updates["HERMES_API_PROVIDER"] = self.settings.hermes_provider

        reasoning = str(body.get("reasoningEffort") or "").strip().lower()
        if reasoning:
            if reasoning not in {"none", "low", "medium", "high", "xhigh"}:
                raise ValueError("Unsupported reasoning effort")
            self.settings.hermes_reasoning_effort = reasoning
            updates["HERMES_API_REASONING_EFFORT"] = reasoning

        tts_backend = _normalize_tts_backend(str(body.get("ttsBackend") or "").strip())
        if tts_backend:
            allowed_tts = {choice["id"] for choice in TTS_CHOICES}
            if tts_backend not in allowed_tts:
                raise ValueError("Unsupported TTS backend")
            self.settings.livekit_tts_backend = tts_backend
            updates["HERMES_LIVEKIT_TTS_BACKEND"] = tts_backend

        if "ttsVoice" in body:
            voice = str(body.get("ttsVoice") or "").strip()[:120]
            self.settings.livekit_tts_voice = voice
            updates["HERMES_LIVEKIT_TTS_VOICE"] = voice

        if "ttsSpeed" in body:
            speed = _clamp_float(body.get("ttsSpeed"), self.settings.livekit_tts_speed, 0.65, 1.35)
            self.settings.livekit_tts_speed = speed
            updates["HERMES_LIVEKIT_TTS_SPEED"] = speed

        if "voiceInstructions" in body:
            instructions = re.sub(r"\s+", " ", str(body.get("voiceInstructions") or "")).strip()
            if not instructions:
                instructions = DEFAULT_VOICE_INSTRUCTIONS
            self.settings.voice_instructions = instructions[:3000]
            updates["HERMES_LIVEKIT_VOICE_INSTRUCTIONS"] = self.settings.voice_instructions

        stt_provider = str(body.get("sttProvider") or "").strip()
        if stt_provider:
            allowed_stt = {choice["id"] for choice in STT_CHOICES}
            if stt_provider not in allowed_stt:
                raise ValueError("Unsupported STT provider")
            self.settings.stt_provider = stt_provider
            updates["HERMES_LIVEKIT_STT_PROVIDER"] = stt_provider

        if "sttModel" in body:
            stt_model = str(body.get("sttModel") or "").strip()[:120]
            self.settings.stt_model = stt_model
            updates["HERMES_LIVEKIT_STT_MODEL"] = stt_model

        if "emotionRecognition" in body:
            enabled = bool(body.get("emotionRecognition"))
            self.settings.emotion2vec_enabled = enabled
            updates["HERMES_EMOTION2VEC_ENABLED"] = "true" if enabled else "false"
            if not enabled:
                self.status.last_voice_affect = None
                self.status.last_local_emotion = {"status": "disabled", "at": time.time()}
                if self._emotion2vec_proc and self._emotion2vec_proc.returncode is None:
                    self._emotion2vec_proc.terminate()
                self._emotion2vec_proc = None
                self._emotion2vec_ready = False
            elif self._emotion2vec_available():
                self._spawn(self._ensure_emotion2vec_worker())

        if updates:
            _write_env_updates(self.settings.env_path, updates)
        return self._settings_payload()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel_participant_tasks(self, identity: str) -> None:
        for task in self._track_tasks.pop(identity, set()):
            task.cancel()

    def _update_participant_count(self) -> None:
        if not self.room:
            self.status.participants = 0
            return
        self.status.participants = len(self.room.remote_participants)

    def _handle_data_packet(self, packet: Any) -> None:
        if getattr(packet, "topic", "") != "hermes.control":
            return
        try:
            payload = json.loads(packet.data.decode("utf-8", "replace"))
        except Exception:
            return
        participant = getattr(packet, "participant", None)
        identity = getattr(participant, "identity", "unknown")
        if payload.get("type") == "talkback":
            LOG.debug("ignoring legacy talkback control from %s", identity)
            return
        if payload.get("type") != "interrupt":
            return
        if not self._speaking.is_set():
            LOG.debug("ignoring interrupt from %s while not speaking", identity)
            return
        LOG.info("interrupt requested by %s: %s", identity, payload.get("reason") or "barge-in")
        self._interrupt_requested.set()
        if self.audio_source:
            with contextlib.suppress(Exception):
                self.audio_source.clear_queue()
        state_coro = self._publish_state("listening", interrupted=True)
        try:
            self._spawn(state_coro)
        except RuntimeError:
            state_coro.close()
            LOG.debug("interrupt state publish skipped without running event loop")

    async def _publish_state(self, state: str, **extra: Any) -> None:
        self.status.agent_state = state
        if not self.room:
            return
        payload = {"type": "agent_state", "state": state, "at": time.time(), **extra}
        try:
            await self.room.local_participant.set_attributes({"lk.agent.state": state})
            await self.room.local_participant.publish_data(
                json.dumps(payload),
                reliable=True,
                topic="hermes.agent_state",
            )
        except Exception as exc:
            LOG.debug("failed to publish LiveKit agent state %s: %s", state, exc)

    async def _publish_tool_cue(
        self,
        emoji: str,
        name: str = "",
        stage: str = "started",
        label: str = "",
    ) -> None:
        if not self.room or not emoji:
            return
        payload = {
            "type": "tool_cue",
            "kind": "emoji",
            "emoji": emoji,
            "name": name,
            "stage": stage,
            "label": label,
            "at": time.time(),
        }
        try:
            LOG.info("publishing LiveKit tool cue %s %s %s", emoji, name, stage)
            await self.room.local_participant.publish_data(
                json.dumps(payload),
                reliable=True,
                topic="hermes.tool_cue",
            )
        except Exception as exc:
            LOG.debug("failed to publish LiveKit tool cue %s: %s", emoji, exc)

    async def _publish_transcript_event(self, role: str, text: str, **extra: Any) -> None:
        if not self.room or not text:
            return
        payload = {"type": "transcript", "role": role, "text": text, "at": time.time(), **extra}
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload),
                reliable=True,
                topic="hermes.transcript",
            )
        except Exception as exc:
            LOG.debug("failed to publish LiveKit transcript event %s: %s", role, exc)

    async def _publish_voice_affect_event(self, affect: dict[str, Any] | None) -> None:
        if not self.room or not affect:
            return
        visible = {
            key: affect.get(key)
            for key in (
                "affect",
                "arousal",
                "pace",
                "urgency",
                "tone_summary",
                "assistant_adjustment",
                "confidence",
                "source",
                "model",
                "local_emotion_guardrail_applied",
                "raw_local_affect",
            )
            if key in affect
        }
        if not visible:
            return
        payload = {"type": "voice_affect", "at": time.time(), **visible}
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload),
                reliable=True,
                topic="hermes.voice_affect",
            )
        except Exception as exc:
            LOG.debug("failed to publish LiveKit voice affect event: %s", exc)

    def _tool_emoji_for_name(self, name: str) -> str:
        lowered = name.lower()
        for needles, emoji in TOOL_EMOJI_FALLBACKS:
            if any(needle in lowered for needle in needles):
                return emoji
        return "🛠️"

    def _tool_cues_from_delta(self, delta: dict[str, Any]) -> list[tuple[str, str]]:
        cues: list[tuple[str, str]] = []
        raw_items: list[Any] = []
        for key in ("tool_calls", "tool_call", "function_call", "tool"):
            value = delta.get(key)
            if value:
                raw_items.extend(value if isinstance(value, list) else [value])
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(
                item.get("name")
                or item.get("tool_name")
                or function.get("name")
                or item.get("type")
                or "tool"
            )
            emoji = str(item.get("emoji") or item.get("icon") or function.get("emoji") or "").strip()
            cues.append((emoji or self._tool_emoji_for_name(name), name))
        return cues

    async def _consume_audio_track(self, track: rtc.RemoteAudioTrack, identity: str) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=CHANNELS, frame_size_ms=FRAME_MS)
        buffer = bytearray()
        speech_started = False
        speech_start = 0.0
        last_loud = 0.0

        try:
            async for event in stream:
                if self._stopping:
                    return

                if self._turn_lock.locked() and not self._interrupt_requested.is_set():
                    buffer.clear()
                    speech_started = False
                    last_loud = 0.0
                    continue

                if self._speaking.is_set() and not self._interrupt_requested.is_set():
                    buffer.clear()
                    speech_started = False
                    last_loud = 0.0
                    continue

                frame_bytes = bytes(event.frame.data)
                if not frame_bytes:
                    continue

                now = time.monotonic()
                rms = audioop.rms(frame_bytes, 2)
                loud = rms >= self.settings.speech_rms_threshold

                if loud and not speech_started:
                    speech_started = True
                    speech_start = now
                    buffer.clear()
                    LOG.debug("speech start from %s rms=%d", identity, rms)

                if speech_started:
                    buffer.extend(frame_bytes)
                    if loud:
                        last_loud = now

                    duration = len(buffer) / (SAMPLE_RATE * CHANNELS * 2)
                    silence = now - last_loud
                    if duration >= self.settings.max_speech_seconds:
                        LOG.info("speech max duration reached for %s", identity)
                        await self._process_utterance(bytes(buffer), identity)
                        buffer.clear()
                        speech_started = False
                    elif silence >= self.settings.silence_seconds:
                        if duration >= self.settings.min_speech_seconds:
                            await self._process_utterance(bytes(buffer), identity)
                        buffer.clear()
                        speech_started = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status.last_error = str(exc)
            LOG.warning("audio consume failed for %s: %s", identity, exc, exc_info=True)

    async def _process_utterance(self, pcm: bytes, identity: str) -> None:
        if self._turn_lock.locked():
            if self._interrupt_requested.is_set():
                LOG.info("waiting for interrupted turn to release before processing barge-in from %s", identity)
                try:
                    await asyncio.wait_for(self._turn_lock.acquire(), timeout=4.0)
                except asyncio.TimeoutError:
                    LOG.info("dropping barge-in utterance from %s; interrupted turn did not release in time", identity)
                    return
                try:
                    await self._process_utterance_locked(pcm, identity)
                finally:
                    self._turn_lock.release()
                return
            LOG.info("dropping utterance from %s while another voice turn is active", identity)
            return

        async with self._turn_lock:
            await self._process_utterance_locked(pcm, identity)

    async def _process_text_turn(
        self,
        text: str,
        identity: str = "typed-web",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("Text is required")
        if len(text) > 24000:
            raise ValueError("Text is too long")
        if self._turn_lock.locked():
            raise RuntimeError("Hermes Voice is already handling a turn")

        async with self._turn_lock:
            return await self._process_text_turn_locked(text, identity, session_id=session_id)

    async def _process_text_turn_locked(
        self,
        transcript: str,
        identity: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        turn_start = time.monotonic()
        timings: dict[str, float] = {"stt": 0.0}
        try:
            self._interrupt_requested.clear()
            self.status.last_transcript = transcript
            LOG.info("typed transcript from %s: %s", identity, transcript)
            await self._publish_transcript_event("user", transcript, identity=identity, source="typed")
            if self._is_new_session_command(transcript):
                return await self._complete_new_session_command(transcript, identity, turn_start, timings)

            reply, turn_timings = await self._answer_and_speak(transcript, turn_start, session_id=session_id)
            timings.update(turn_timings)
            if not reply:
                await self._publish_state("listening")
                return {"reply": "", "timings": timings}

            self.status.last_reply = reply
            self.status.last_turn_at = time.time()
            self.status.last_timings = timings
            if not timings.get("hermes_error"):
                self.status.last_error = ""
            turn = {
                "at": self.status.last_turn_at,
                "identity": identity,
                "transcript": transcript,
                "reply": reply,
                "timings": timings,
            }
            self.status.recent_turns.append(turn)
            self.status.recent_turns = self.status.recent_turns[-8:]
            LOG.info("typed turn complete for %s timings=%s", identity, json.dumps(timings, sort_keys=True))
            await self._publish_transcript_event("assistant", reply, identity=self.settings.agent_identity)
            await self._publish_state("listening")
            return {"reply": reply, "timings": timings}
        except Exception as exc:
            self.status.last_error = str(exc)
            await self._publish_state("error", error=str(exc))
            LOG.warning("typed turn failed: %s", exc, exc_info=True)
            raise

    async def _process_utterance_locked(self, pcm: bytes, identity: str) -> None:
        turn_start = time.monotonic()
        timings: dict[str, float] = {}
        wav_path = self._write_wav(pcm)
        was_interrupted = self._interrupt_requested.is_set()
        try:
            with contextlib.suppress(Exception):
                latest_audio = self.settings.emotion2vec_cache_dir / "last-emotion-input.wav"
                latest_audio.parent.mkdir(parents=True, exist_ok=True)
                copyfile(wav_path, latest_audio)
            self._interrupt_requested.clear()
            await self._publish_state("thinking", stage="transcribing")
            transcript = await asyncio.to_thread(self._transcribe, wav_path)
            timings["stt"] = time.monotonic() - turn_start
            if not transcript:
                await self._publish_state("listening")
                return

            norm = self._normalize_transcript(transcript)
            now = time.monotonic()
            if norm and norm == self._last_transcript_norm and now - self._last_transcript_at < 8.0:
                LOG.info("dropping duplicate transcript from %s: %s", identity, transcript)
                await self._publish_state("listening")
                return
            self._last_transcript_norm = norm
            self._last_transcript_at = now

            self.status.last_transcript = transcript
            LOG.info("voice transcript from %s: %s", identity, transcript)
            await self._publish_transcript_event("user", transcript, identity=identity, source="voice")
            if self._is_new_session_command(transcript):
                await self._complete_new_session_command(transcript, identity, turn_start, timings)
                return

            voice_affect, affect_timing = await self._prepare_kimi_voice_affect(
                pcm=pcm,
                wav_path=wav_path,
                transcript=transcript,
                identity=identity,
                was_interrupted=was_interrupted,
            )
            if affect_timing:
                timings["kimi_affect"] = affect_timing
            await self._publish_voice_affect_event(voice_affect)

            if self.settings.livekit_tts_backend.strip().lower() == "kimi_audio" and self._kimi_audio_configured():
                reply, turn_timings = await self._answer_with_kimi_audio_conversation(
                    pcm=pcm,
                    transcript=transcript,
                    turn_start=turn_start,
                    voice_affect=voice_affect,
                )
            else:
                reply, turn_timings = await self._answer_and_speak(transcript, turn_start, voice_affect=voice_affect)
            timings.update(turn_timings)
            if not reply:
                await self._publish_state("listening")
                return

            self.status.last_reply = reply
            self.status.last_turn_at = time.time()
            self.status.last_timings = timings
            if not timings.get("hermes_error"):
                self.status.last_error = ""
            self.status.recent_turns.append(
                {
                    "at": self.status.last_turn_at,
                    "identity": identity,
                    "transcript": transcript,
                    "reply": reply,
                    "timings": timings,
                }
            )
            self.status.recent_turns = self.status.recent_turns[-8:]
            LOG.info("voice turn complete for %s timings=%s", identity, json.dumps(timings, sort_keys=True))
            await self._publish_transcript_event("assistant", reply, identity=self.settings.agent_identity)
            await self._publish_state("listening")
        except Exception as exc:
            self.status.last_error = str(exc)
            await self._publish_state("error", error=str(exc))
            LOG.warning("turn processing failed: %s", exc, exc_info=True)
        finally:
            try:
                Path(wav_path).unlink()
            except OSError:
                pass

    def _is_new_session_command(self, transcript: str) -> bool:
        return self._normalize_transcript(transcript) == "new session"

    async def _complete_new_session_command(
        self,
        transcript: str,
        identity: str,
        turn_start: float,
        timings: dict[str, float],
    ) -> dict[str, Any]:
        old_session_id = self.settings.hermes_session_id
        self.settings.hermes_session_id = f"livekit-voice-main-{uuid.uuid4().hex[:8]}"
        reply = "New session started."
        LOG.info(
            "Hermes Voice session rotated from %s to %s by %s",
            old_session_id,
            self.settings.hermes_session_id,
            identity,
        )
        await self._publish_state("thinking", stage="tts", transcript=transcript)
        timings.update(await self._speak_reply(reply, turn_start))
        self.status.last_reply = reply
        self.status.last_turn_at = time.time()
        self.status.last_timings = timings
        self.status.last_error = ""
        self.status.recent_turns.append(
            {
                "at": self.status.last_turn_at,
                "identity": identity,
                "transcript": transcript,
                "reply": reply,
                "timings": timings,
            }
        )
        self.status.recent_turns = self.status.recent_turns[-8:]
        LOG.info("local new-session turn complete for %s timings=%s", identity, json.dumps(timings, sort_keys=True))
        await self._publish_transcript_event("assistant", reply, identity=self.settings.agent_identity)
        await self._publish_state("listening")
        return {"reply": reply, "timings": timings}

    @staticmethod
    def _normalize_transcript(transcript: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", transcript.lower()).strip()

    @staticmethod
    def _write_wav(pcm: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(prefix="hermes_livekit_in_", suffix=".wav", delete=False)
        handle.close()
        with wave.open(handle.name, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        return handle.name

    def _transcribe(self, wav_path: str) -> str:
        stt_provider = self.settings.stt_provider.strip().lower()
        stt_model = self.settings.stt_model.strip() or None
        try:
            from tools import transcription_tools
            from tools.voice_mode import is_whisper_hallucination

            if stt_provider in {"", "auto", "elevenlabs", "inworld"}:
                if stt_provider in {"elevenlabs", "inworld"}:
                    LOG.warning("STT provider %s is a placeholder; using Hermes Voice Native", stt_provider)
                result = transcription_tools.transcribe_audio(wav_path, model=stt_model)
            else:
                result = self._transcribe_with_provider(transcription_tools, wav_path, stt_provider, stt_model)
            if not result.get("success"):
                LOG.info("STT skipped/failed: %s", result.get("error"))
                return ""
            transcript = str(result.get("transcript") or "").strip()
            if not transcript or is_whisper_hallucination(transcript):
                return ""
            return transcript
        except ImportError:
            if stt_provider not in {"", "auto", "local"}:
                LOG.warning("Hermes STT tools unavailable; using packaged local STT instead of %s", stt_provider)
            transcript = self._transcribe_local_faster_whisper(wav_path, stt_model or self.settings.local_stt_model)
            return "" if self._is_likely_whisper_hallucination(transcript) else transcript

    def _transcribe_local_faster_whisper(self, wav_path: str, model_name: str) -> str:
        from faster_whisper import WhisperModel

        model_name = (model_name or "base.en").strip()
        if self._local_whisper_model is None or self._local_whisper_model_name != model_name:
            LOG.info("loading packaged local STT model %s", model_name)
            self._local_whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            self._local_whisper_model_name = model_name
        segments, info = self._local_whisper_model.transcribe(
            wav_path,
            beam_size=1,
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        LOG.info("transcribed via packaged local STT model=%s duration=%.1fs", model_name, getattr(info, "duration", 0.0))
        return text

    @staticmethod
    def _is_likely_whisper_hallucination(transcript: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", transcript.lower()).strip()
        if not normalized:
            return True
        return normalized in {
            "thank you",
            "thanks for watching",
            "thanks for watching this video",
            "you",
            "music",
            "bye",
        }

    @staticmethod
    def _transcribe_with_provider(transcription_tools: Any, wav_path: str, provider: str, model: str | None) -> dict[str, Any]:
        stt_config = transcription_tools._load_stt_config()
        if not transcription_tools.is_stt_enabled(stt_config):
            return {"success": False, "transcript": "", "error": "STT is disabled in config.yaml."}
        if provider == "local":
            local_cfg = stt_config.get("local", {})
            model_name = transcription_tools._normalize_local_model(model or local_cfg.get("model", transcription_tools.DEFAULT_LOCAL_MODEL))
            return transcription_tools._transcribe_local(wav_path, model_name)
        if provider == "local_command":
            local_cfg = stt_config.get("local", {})
            model_name = transcription_tools._normalize_local_command_model(model or local_cfg.get("model", transcription_tools.DEFAULT_LOCAL_MODEL))
            return transcription_tools._transcribe_local_command(wav_path, model_name)
        if provider == "groq":
            return transcription_tools._transcribe_groq(wav_path, model or transcription_tools.DEFAULT_GROQ_STT_MODEL)
        if provider == "openai":
            openai_cfg = stt_config.get("openai", {})
            return transcription_tools._transcribe_openai(wav_path, model or openai_cfg.get("model", transcription_tools.DEFAULT_STT_MODEL))
        if provider == "mistral":
            mistral_cfg = stt_config.get("mistral", {})
            return transcription_tools._transcribe_mistral(wav_path, model or mistral_cfg.get("model", transcription_tools.DEFAULT_MISTRAL_STT_MODEL))
        if provider == "xai":
            return transcription_tools._transcribe_xai(wav_path, model or "grok-stt")
        return {"success": False, "transcript": "", "error": f"Unsupported STT provider: {provider}"}

    def _voice_prosody_stats(self, pcm: bytes, transcript: str, was_interrupted: bool) -> dict[str, Any]:
        duration_seconds = len(pcm) / max(1, SAMPLE_RATE * CHANNELS * 2)
        words = re.findall(r"[A-Za-z0-9']+", transcript)
        rms_values: list[int] = []
        loud_frames = 0
        for offset in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[offset : offset + FRAME_BYTES]
            if len(chunk) < 2:
                continue
            rms = audioop.rms(chunk, 2)
            rms_values.append(rms)
            if rms >= self.settings.speech_rms_threshold:
                loud_frames += 1

        avg_rms = sum(rms_values) / len(rms_values) if rms_values else 0.0
        peak_rms = max(rms_values) if rms_values else 0
        loud_ratio = loud_frames / len(rms_values) if rms_values else 0.0
        words_per_minute = (len(words) / duration_seconds * 60.0) if duration_seconds else 0.0
        chars_per_second = (len(transcript) / duration_seconds) if duration_seconds else 0.0
        return {
            "duration_seconds": round(duration_seconds, 2),
            "word_count": len(words),
            "words_per_minute": round(words_per_minute, 1),
            "chars_per_second": round(chars_per_second, 1),
            "avg_rms": round(avg_rms, 1),
            "peak_rms": int(peak_rms),
            "loud_frame_ratio": round(loud_ratio, 3),
            "rms_threshold": self.settings.speech_rms_threshold,
            "barge_in_interrupt": bool(was_interrupted),
        }

    def _local_prosody_hint(self, stats: dict[str, Any]) -> dict[str, Any]:
        wpm = float(stats.get("words_per_minute") or 0.0)
        cps = float(stats.get("chars_per_second") or 0.0)
        barge_in = bool(stats.get("barge_in_interrupt"))

        signals: list[str] = []
        fast = wpm >= 185 or cps >= 14
        very_fast = wpm >= 235 or cps >= 18

        if very_fast:
            signals.append("very fast speech")
        elif fast:
            signals.append("fast speech")
        if barge_in:
            signals.append("barge-in/interruption")

        if barge_in and very_fast:
            affect = "urgent"
            arousal = "high"
            urgency = "high"
            confidence = 0.74
        elif very_fast:
            affect = "rushed"
            arousal = "high"
            urgency = "high"
            confidence = 0.64
        elif fast:
            affect = "rushed"
            arousal = "medium"
            urgency = "medium"
            confidence = 0.56
        elif barge_in:
            affect = "urgent"
            arousal = "medium"
            urgency = "medium"
            confidence = 0.55
        else:
            affect = "neutral"
            arousal = "low"
            urgency = "low"
            confidence = 0.45

        return {
            "affect": affect,
            "arousal": arousal,
            "pace": "fast" if fast else "normal",
            "urgency": urgency,
            "signals": signals or ["no strong prosody signal"],
            "confidence": confidence,
        }

    async def _prepare_kimi_voice_affect(
        self,
        *,
        pcm: bytes,
        wav_path: str,
        transcript: str,
        identity: str,
        was_interrupted: bool,
    ) -> tuple[dict[str, Any] | None, float]:
        if not self.settings.emotion2vec_enabled:
            return None, 0.0

        stats = self._voice_prosody_stats(pcm, transcript, was_interrupted)
        hint = self._local_prosody_hint(stats)
        self.status.last_voice_prosody = {"metrics": stats, "local_hint": hint, "at": time.time()}
        self.status.last_voice_affect = None
        LOG.info("voice prosody stats: %s", json.dumps(self.status.last_voice_prosody, sort_keys=True))
        started = time.monotonic()

        local_emotion = await self._analyze_emotion2vec_affect(wav_path, hint, identity, stats, transcript)
        if local_emotion:
            self._latest_voice_affect = local_emotion
            self.status.last_voice_affect = local_emotion
            return local_emotion, time.monotonic() - started
        if self._emotion2vec_available():
            local_status = self.status.last_local_emotion if isinstance(self.status.last_local_emotion, dict) else {}
            if str(local_status.get("status") or "") in {"timeout", "failed"}:
                hint_affect = self._local_affect_from_hint(hint, identity)
                if hint_affect:
                    self.status.last_voice_affect = hint_affect
                    await self._publish_voice_affect_event(hint_affect)
                    return hint_affect, time.monotonic() - started
                unreadable = {
                    "affect": "unreadable",
                    "arousal": "unknown",
                    "pace": hint.get("pace") or "unknown",
                    "urgency": "unknown",
                    "tone_summary": "audio emotion classifier did not return in time",
                    "assistant_adjustment": "do not infer user emotion from this turn",
                    "confidence": 0.0,
                    "source": "emotion_timeout",
                    "identity": identity,
                    "at": time.time(),
                }
                self.status.last_voice_affect = unreadable
                await self._publish_voice_affect_event(unreadable)
                return None, time.monotonic() - started

        affect = self._local_affect_from_hint(hint, identity)
        if affect:
            self.status.last_voice_affect = affect
        return affect, time.monotonic() - started

    def _kimi_audio_available(self) -> bool:
        return (
            self.settings.kimi_audio_enabled
            and self.settings.kimi_audio_provider.strip().lower() == "replicate"
            and bool(self.settings.kimi_audio_replicate_token)
        )

    def _kimi_audio_configured(self) -> bool:
        return (
            self.settings.kimi_audio_provider.strip().lower() == "replicate"
            and bool(self.settings.kimi_audio_replicate_token)
        )

    def _emotion2vec_available(self) -> bool:
        return (
            self.settings.emotion2vec_enabled
            and Path(self.settings.emotion2vec_python).exists()
            and self.settings.emotion2vec_helper.exists()
            and (not self.settings.emotion2vec_pythonpath or Path(self.settings.emotion2vec_pythonpath).exists())
        )

    async def _ensure_emotion2vec_worker(self) -> asyncio.subprocess.Process | None:
        if not self._emotion2vec_available():
            self._emotion2vec_ready = False
            self.status.last_local_emotion = {
                "status": "disabled",
                "error": "emotion2vec runtime is not installed",
                "at": time.time(),
            }
            return None
        async with self._emotion2vec_start_lock:
            proc = self._emotion2vec_proc
            if proc and proc.returncode is None and proc.stdin and proc.stdout:
                if self._emotion2vec_ready:
                    return proc
                return await self._wait_emotion2vec_ready(proc)

            self._emotion2vec_ready = False
            self.status.last_local_emotion = {
                "status": "starting",
                "model": self.settings.emotion2vec_model,
                "at": time.time(),
            }
            proc = await asyncio.create_subprocess_exec(
                self.settings.emotion2vec_python,
                str(self.settings.emotion2vec_helper),
                "--worker",
                "--model",
                self.settings.emotion2vec_model,
                "--pythonpath",
                self.settings.emotion2vec_pythonpath,
                "--cache-dir",
                str(self.settings.emotion2vec_cache_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._emotion2vec_proc = proc
            self._spawn(self._drain_emotion2vec_stderr(proc))
            return await self._wait_emotion2vec_ready(proc)

    async def _wait_emotion2vec_ready(
        self,
        proc: asyncio.subprocess.Process,
    ) -> asyncio.subprocess.Process | None:
        if not proc.stdout:
            return None
        startup_timeout = max(60.0, self.settings.emotion2vec_timeout_seconds + 20.0)
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0.2, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                LOG.debug("emotion2vec startup stdout: %r", line[:300])
                continue
            if message.get("type") == "ready" and message.get("success"):
                self._emotion2vec_ready = True
                self.status.last_local_emotion = {
                    "status": "ready",
                    "model": message.get("model", self.settings.emotion2vec_model),
                    "at": time.time(),
                }
                return proc
            self.status.last_local_emotion = {
                "status": "failed",
                "error": f"unexpected startup message: {message!r}"[:500],
                "at": time.time(),
            }
            break

        self._emotion2vec_ready = False
        self.status.last_local_emotion = {
            "status": "timeout",
            "phase": "startup",
            "timeout_seconds": round(startup_timeout, 1),
            "at": time.time(),
        }
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        if self._emotion2vec_proc is proc:
            self._emotion2vec_proc = None
        return None

    async def _drain_emotion2vec_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                LOG.debug("emotion2vec: %s", text[:600])

    async def _analyze_emotion2vec_affect(
        self,
        wav_path: str,
        local_hint: dict[str, Any],
        identity: str,
        stats: dict[str, Any],
        transcript: str,
    ) -> dict[str, Any] | None:
        if not self._emotion2vec_available():
            return None
        async with self._emotion2vec_lock:
            try:
                proc = await self._ensure_emotion2vec_worker()
                if not proc or not proc.stdin or not proc.stdout:
                    return None
                request = json.dumps({"audio": wav_path}) + "\n"
                proc.stdin.write(request.encode("utf-8"))
                await proc.stdin.drain()
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=max(0.2, self.settings.emotion2vec_timeout_seconds),
                )
            except asyncio.TimeoutError:
                LOG.info(
                    "emotion2vec inference timed out after %.2fs for %s",
                    self.settings.emotion2vec_timeout_seconds,
                    identity,
                )
                debug_audio_path = None
                with contextlib.suppress(Exception):
                    debug_audio = self.settings.emotion2vec_cache_dir / "last-timeout-input.wav"
                    debug_audio.parent.mkdir(parents=True, exist_ok=True)
                    copyfile(wav_path, debug_audio)
                    debug_audio_path = str(debug_audio)
                self.status.last_local_emotion = {
                    "status": "timeout",
                    "phase": "inference",
                    "timeout_seconds": self.settings.emotion2vec_timeout_seconds,
                    "debug_audio_path": debug_audio_path,
                    "at": time.time(),
                }
                if self._emotion2vec_proc and self._emotion2vec_proc.returncode is None:
                    self._emotion2vec_proc.kill()
                    with contextlib.suppress(Exception):
                        await self._emotion2vec_proc.wait()
                self._emotion2vec_ready = False
                self._emotion2vec_proc = None
                return None
            except Exception as exc:
                self.status.last_local_emotion = {"status": "failed", "error": str(exc)[:500], "at": time.time()}
                return None

        if not line:
            self.status.last_local_emotion = {"status": "failed", "error": "emotion2vec worker closed", "at": time.time()}
            self._emotion2vec_ready = False
            self._emotion2vec_proc = None
            return None
        try:
            result = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.status.last_local_emotion = {
                "status": "failed",
                "error": f"invalid worker JSON: {line[:300]!r}",
                "at": time.time(),
            }
            return None
        result["at"] = time.time()
        self.status.last_local_emotion = result
        if not result.get("success"):
            return None

        affect = {
            "affect": result.get("affect"),
            "arousal": result.get("arousal"),
            "pace": local_hint.get("pace") if local_hint.get("pace") != "normal" else "normal",
            "urgency": result.get("urgency"),
            "tone_summary": result.get("tone_summary"),
            "assistant_adjustment": result.get("assistant_adjustment"),
            "confidence": result.get("confidence"),
            "source": "emotion2vec",
            "model": result.get("model"),
            "identity": identity,
            "at": time.time(),
        }
        affect = self._apply_local_emotion_guardrails(affect, result, local_hint, stats, transcript)
        if affect["affect"] in {"unknown", "other"}:
            return None
        LOG.info("emotion2vec affect: %s", json.dumps(affect, sort_keys=True))
        return affect

    async def _analyze_kimi_audio_affect(
        self,
        pcm: bytes,
        transcript: str,
        stats: dict[str, Any],
        local_hint: dict[str, Any],
        identity: str,
    ) -> dict[str, Any] | None:
        await self._publish_tool_cue("🎧", "Kimi-Audio", stage="started", label="listening to voice tone")
        self.status.last_kimi_audio_status = {
            "status": "starting",
            "provider": "replicate",
            "model": self.settings.kimi_audio_replicate_model,
            "at": time.time(),
        }
        audio_data_uri = await asyncio.to_thread(self._audio_data_uri_from_pcm, pcm)
        prompt = (
            "Listen to this audio and classify the speaker's delivery. "
            "Use the actual voice audio first, not just the transcript. "
            "Return only valid compact JSON with these keys: affect, arousal, pace, urgency, "
            "tone_summary, assistant_adjustment, confidence. "
            "Allowed affect labels: neutral, calm, frustrated, annoyed, rushed, frantic, confused, excited, tired, urgent, sad, sarcastic. "
            "Do not diagnose health or intent. If uncertain, use neutral with lower confidence. "
            f"Transcript from the primary STT: {transcript!r}. "
            f"Local acoustic metrics for reference: {json.dumps(stats, sort_keys=True)}. "
            f"Local prosody hint for reference: {json.dumps(local_hint, sort_keys=True)}."
        )
        prediction = await self._run_replicate_kimi_audio(audio_data_uri, prompt, output_type="text", return_json=True)
        self.status.last_kimi_audio_status = {
            "status": prediction.get("status"),
            "id": prediction.get("id"),
            "error": prediction.get("error"),
            "metrics": prediction.get("metrics"),
            "at": time.time(),
        }
        content = self._extract_replicate_text(prediction)
        affect = self._parse_kimi_affect_json(content)
        if not affect:
            raise RuntimeError(f"Kimi-Audio response did not contain affect JSON: {content[:240]}")
        affect = self._apply_prosody_guardrails(affect, local_hint)
        affect["at"] = time.time()
        affect["source"] = "kimi_audio_replicate"
        affect["model"] = self.settings.kimi_audio_replicate_model
        affect["identity"] = identity
        self._latest_voice_affect = affect
        self.status.last_voice_affect = affect
        label = str(affect.get("tone_summary") or affect.get("affect") or "voice affect").strip()
        await self._publish_tool_cue("🎧", "Kimi-Audio", stage="done", label=label[:80])
        LOG.info("Kimi-Audio affect: %s", json.dumps(affect, sort_keys=True))
        return affect

    def _audio_data_uri_from_pcm(self, pcm: bytes) -> str:
        max_bytes = int(max(1.0, self.settings.kimi_audio_max_clip_seconds) * SAMPLE_RATE * CHANNELS * 2)
        clipped = pcm[:max_bytes]
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(clipped)
        return f"data:audio/wav;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

    @staticmethod
    def _silent_audio_data_uri(duration_seconds: float = 0.25) -> str:
        frames = b"\x00\x00" * int(SAMPLE_RATE * CHANNELS * max(0.05, duration_seconds))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(frames)
        return f"data:audio/wav;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

    @staticmethod
    def _replicate_retry_after_seconds(response_text: str) -> float:
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            return 0.0
        value = payload.get("retry_after")
        try:
            return max(0.0, min(20.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    async def _run_replicate_kimi_audio(
        self,
        audio_data_uri: str,
        prompt: str,
        *,
        output_type: str,
        return_json: bool,
    ) -> dict[str, Any]:
        payload = {
            "version": self.settings.kimi_audio_replicate_version,
            "input": {
                "audio": audio_data_uri,
                "prompt": prompt,
                "output_type": output_type,
                "return_json": return_json,
            }
        }
        headers = {
            "Authorization": f"Bearer {self.settings.kimi_audio_replicate_token}",
            "Content-Type": "application/json",
            "Prefer": f"wait={max(1, min(60, int(self.settings.kimi_audio_timeout_seconds)))}",
        }
        timeout = ClientTimeout(total=max(2.0, self.settings.kimi_audio_timeout_seconds + 3.0))
        async with ClientSession(timeout=timeout) as session:
            for attempt in range(2):
                async with session.post(self.settings.kimi_audio_replicate_url, headers=headers, json=payload) as response:
                    text = await response.text()
                    if response.status == 429 and attempt == 0:
                        retry_after = self._replicate_retry_after_seconds(text)
                        if retry_after > 0:
                            self.status.last_kimi_audio_status = {
                                "status": "rate_limited",
                                "retry_after": retry_after,
                                "error": text[:500],
                                "at": time.time(),
                            }
                            await self._publish_state(
                                "thinking",
                                stage="hermes",
                                error=f"Replicate is rate-limiting Kimi-Audio; retrying in {retry_after:.0f}s.",
                            )
                            await asyncio.sleep(retry_after)
                            continue
                    if response.status >= 400:
                        raise RuntimeError(f"Replicate Kimi-Audio {response.status}: {text[:500]}")
                    prediction = json.loads(text)
                    break

            deadline = time.monotonic() + max(1.0, self.settings.kimi_audio_timeout_seconds)
            while prediction.get("status") not in {"succeeded", "failed", "canceled"}:
                get_url = ((prediction.get("urls") or {}).get("get") or "").strip()
                if not get_url or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.35)
                async with session.get(get_url, headers={"Authorization": headers["Authorization"]}) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"Replicate Kimi-Audio poll {response.status}: {text[:500]}")
                    prediction = json.loads(text)

        if prediction.get("status") == "failed":
            raise RuntimeError(f"Replicate Kimi-Audio failed: {str(prediction.get('error') or '')[:500]}")
        return prediction

    @staticmethod
    def _extract_replicate_media_url(prediction: dict[str, Any]) -> str:
        output = prediction.get("output")
        if isinstance(output, dict):
            for key in ("media_path", "audio", "audio_url", "url"):
                value = output.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(output, list):
            for item in output:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    return item
                if isinstance(item, dict):
                    nested = HermesLiveKitVoice._extract_replicate_media_url({"output": item})
                    if nested:
                        return nested
        if isinstance(output, str) and output.startswith(("http://", "https://")):
            return output
        return ""

    def _download_audio_to_temp(self, url: str, suffix: str) -> str:
        out = tempfile.NamedTemporaryFile(prefix="hermes_livekit_kimi_tts_", suffix=suffix, delete=False)
        out.close()
        request = urllib_request.Request(url, headers={"User-Agent": "hermes-voice/1.0"})
        try:
            with urllib_request.urlopen(request, timeout=max(15.0, self.settings.kimi_audio_timeout_seconds)) as response:
                data = response.read()
        except urllib_error.URLError as exc:
            with contextlib.suppress(OSError):
                Path(out.name).unlink()
            raise RuntimeError(f"failed to download Kimi-Audio output: {exc}") from exc
        Path(out.name).write_bytes(data)
        return out.name

    @staticmethod
    def _extract_replicate_text(prediction: dict[str, Any]) -> str:
        output = prediction.get("output")
        if isinstance(output, str):
            return output.strip()
        if isinstance(output, dict):
            for key in ("json_str", "text", "generated_text", "content", "transcript", "answer"):
                value = output.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(output, ensure_ascii=False)
        if isinstance(output, list):
            chunks: list[str] = []
            for item in output:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    for key in ("text", "generated_text", "content", "answer"):
                        value = item.get(key)
                        if isinstance(value, str):
                            chunks.append(value)
                            break
            return "".join(chunks).strip()
        return ""

    @staticmethod
    def _clean_replicate_text(content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("text", "answer", "response", "content", "generated_text", "transcript"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(data, ensure_ascii=False)
        if isinstance(data, list):
            parts = [item for item in data if isinstance(item, str) and item.strip()]
            if parts:
                return " ".join(parts).strip()
        return text

    @staticmethod
    def _local_affect_from_hint(local_hint: dict[str, Any], identity: str) -> dict[str, Any] | None:
        if not local_hint or local_hint.get("affect") == "neutral":
            return None
        signals = ", ".join(str(item) for item in local_hint.get("signals", []))
        return {
            "affect": local_hint.get("affect"),
            "arousal": local_hint.get("arousal"),
            "pace": local_hint.get("pace"),
            "urgency": local_hint.get("urgency"),
            "tone_summary": f"local prosody suggests {signals}",
            "assistant_adjustment": "respond directly, acknowledge urgency, and avoid extra preamble",
            "confidence": local_hint.get("confidence"),
            "source": "local_prosody",
            "identity": identity,
            "at": time.time(),
        }

    @staticmethod
    def _apply_prosody_guardrails(affect: dict[str, Any], local_hint: dict[str, Any]) -> dict[str, Any]:
        hint_affect = str(local_hint.get("affect") or "neutral")
        hint_confidence = float(local_hint.get("confidence") or 0.0)
        guardrail_affects = {"rushed", "urgent"}
        if hint_affect == "neutral" or hint_confidence < 0.60:
            return affect
        if hint_affect not in guardrail_affects:
            return affect

        raw_affect = str(affect.get("affect") or "").lower()
        raw_summary = str(affect.get("tone_summary") or "").lower()
        too_neutral = raw_affect in {"", "neutral", "calm"} or "casual" in raw_summary
        if not too_neutral:
            return affect

        adjusted = dict(affect)
        adjusted["kimi_raw_affect"] = affect.get("affect")
        adjusted["kimi_raw_tone_summary"] = affect.get("tone_summary")
        adjusted["affect"] = hint_affect
        adjusted["arousal"] = local_hint.get("arousal")
        adjusted["pace"] = local_hint.get("pace")
        adjusted["urgency"] = local_hint.get("urgency")
        signals = ", ".join(str(item) for item in local_hint.get("signals", []))
        adjusted["tone_summary"] = f"prosody overrides neutral transcript: {signals}"
        adjusted["assistant_adjustment"] = "respond directly, acknowledge urgency, and avoid extra preamble"
        adjusted["confidence"] = max(hint_confidence, float(affect.get("confidence") or 0.0))
        adjusted["prosody_guardrail_applied"] = True
        return adjusted

    @staticmethod
    def _apply_local_emotion_guardrails(
        affect: dict[str, Any],
        result: dict[str, Any],
        local_hint: dict[str, Any],
        stats: dict[str, Any],
        transcript: str,
    ) -> dict[str, Any]:
        raw_affect = str(affect.get("affect") or "").lower()
        if raw_affect != "happy":
            return affect

        scores = result.get("scores") if isinstance(result.get("scores"), list) else []
        score_map: dict[str, float] = {}
        for item in scores:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").lower()
            try:
                score_map[label] = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                continue

        tension_score = max(score_map.get("angry", 0.0), score_map.get("disgust", 0.0))
        try:
            top_confidence = float(affect.get("confidence") or 0.0)
        except (TypeError, ValueError):
            top_confidence = 0.0

        forceful_delivery = (
            float(stats.get("avg_rms") or 0.0) >= 1800.0
            or float(stats.get("peak_rms") or 0.0) >= 12000.0
            or float(stats.get("loud_frame_ratio") or 0.0) >= 0.70
        )
        positive_words = {
            "awesome",
            "cool",
            "delighted",
            "excited",
            "fun",
            "funny",
            "glad",
            "good",
            "great",
            "haha",
            "happy",
            "hilarious",
            "love",
            "nice",
            "perfect",
            "thanks",
            "thank",
            "yay",
        }
        words = {word.lower() for word in re.findall(r"[A-Za-z']+", transcript)}
        positive_context = bool(words & positive_words)
        command_like = bool(re.match(r"^\s*(tell|check|look|find|show|what|when|where|why|how|is|are|can|could|do|does)\b", transcript, flags=re.I))

        if not forceful_delivery or positive_context or not command_like or tension_score < 0.12 or top_confidence > 0.90:
            return affect

        adjusted = dict(affect)
        adjusted["raw_local_affect"] = affect.get("affect")
        adjusted["raw_local_tone_summary"] = affect.get("tone_summary")
        adjusted["affect"] = "frustrated"
        adjusted["arousal"] = "high" if float(stats.get("peak_rms") or 0.0) >= 12000.0 else "medium"
        adjusted["urgency"] = "medium"
        adjusted["tone_summary"] = (
            "local calibration treated a happy label as forceful/frustrated delivery "
            "because anger or disgust was also present on a non-positive command"
        )
        adjusted["assistant_adjustment"] = "be direct and non-defensive; do not mirror cheerfulness"
        adjusted["confidence"] = round(max(0.58, min(top_confidence, 0.72)), 4)
        adjusted["local_emotion_guardrail_applied"] = True
        adjusted["raw_local_scores"] = scores[:3]
        if local_hint.get("pace"):
            adjusted["pace"] = local_hint.get("pace")
        return adjusted

    @staticmethod
    def _settle_background_task(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.result()

    @staticmethod
    def _parse_kimi_affect_json(content: str) -> dict[str, Any] | None:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            cleaned = match.group(0)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        compact: dict[str, Any] = {}
        for key in ("affect", "arousal", "pace", "urgency", "tone_summary", "assistant_adjustment", "confidence"):
            if key in data:
                compact[key] = data[key]
        return compact or None

    def _voice_system_prompt(self, transcript: str = "") -> str:
        voice_instructions = self.settings.voice_instructions.strip() or DEFAULT_VOICE_INSTRUCTIONS
        model = self.settings.hermes_model.strip() or "hermes-agent"
        provider_override = self._hermes_provider_override(model)
        if provider_override:
            runtime_fact = (
                f"Current Hermes Voice runtime: main Hermes API with model override {model} "
                f"on provider {provider_override}. This is still Hermes with tools and memory. "
                f"If asked what model you are running in Hermes Voice, answer that you are Hermes using {model} as the current Hermes Voice model override."
            )
        elif model == "hermes-agent":
            runtime_fact = (
                "Current Hermes Voice runtime: main Hermes API default model routing. "
                "If asked what model you are running in Hermes Voice, say you are Hermes using the default Hermes model route."
            )
        else:
            runtime_fact = (
                f"Current Hermes Voice runtime: main Hermes API with model override {model}. "
                f"If asked what model you are running in Hermes Voice, answer with this current Hermes Voice model override."
            )
        return (
            "You are the normal Hermes agent, reached through Hermes Voice. "
            f"{runtime_fact} "
            "Use the same memory, tools, skills, files, terminal, cron, email, web, and operational context you would use from the main Hermes API. "
            "Do not treat Hermes Voice as a reduced-capability mode; if the request needs inspection or action, use the available tools. "
            "For live/current-state questions, do not answer from memory or assumption; use the relevant source-of-truth tool first. "
            "For sending email, use the configured email sending tool after user confirmation; do not use read-only email lookup tools for sending. "
            "Use memory or session search for preferences, history, continuity, and context, or after live tools need interpretation. "
            f"Hermes Voice response contract: {voice_instructions}"
        )

    def _hermes_headers(self, session_id: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.hermes_api_key}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session_id or self.settings.hermes_session_id,
        }

    def _voice_affect_prompt(self, affect: dict[str, Any] | None) -> str | None:
        if not affect:
            return None
        visible = {key: affect.get(key) for key in ("affect", "arousal", "pace", "urgency", "tone_summary", "assistant_adjustment", "confidence") if key in affect}
        if not visible:
            return None
        acknowledgement = self._voice_affect_acknowledgement(affect)
        acknowledgement_rule = (
            "Hermes Voice will prepend the affect acknowledgement before your reply. "
            "Do not add a second separate emotion acknowledgement; just answer the user's request in the matching tone. "
            if acknowledgement
            else "For this test turn, do not mention emotion unless the user asks about it. "
        )
        affect_rule = self._voice_affect_rule(affect)
        return (
            "Local voice-affect sidecar for the current speaker returned this context. "
            "Treat it as fallible conversational context, not a diagnosis or a fact about the user's inner state. "
            f"{acknowledgement_rule}"
            f"{affect_rule} "
            "Do not say a classifier or model detected it unless asked. "
            f"Current voice-affect context: {json.dumps(visible, ensure_ascii=False, sort_keys=True)}"
        )

    @staticmethod
    def _voice_affect_rule(affect: dict[str, Any] | None) -> str:
        if not affect:
            return VOICE_AFFECT_RULES["unknown"]
        affect_label = str(affect.get("affect") or "unknown").lower()
        try:
            confidence = float(affect.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.60:
            return "Emotion rule: low confidence; adjust style subtly and do not mention emotion."
        if affect_label in VOICE_AFFECT_RULES:
            return VOICE_AFFECT_RULES[affect_label]
        arousal = str(affect.get("arousal") or "").lower()
        urgency = str(affect.get("urgency") or "").lower()
        if arousal == "high" or urgency == "high":
            return "Emotion rule: high arousal or urgency; be concise, action-first, and non-defensive."
        return "Emotion rule: adjust style lightly, but do not name the emotion unless the user asks."

    @staticmethod
    def _voice_affect_acknowledgement(affect: dict[str, Any] | None) -> str:
        if not affect:
            return ""
        affect_label = str(affect.get("affect") or "").lower()
        try:
            confidence = float(affect.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.60 or affect_label in {"", "neutral", "calm", "unknown", "unreadable"}:
            return ""
        if affect_label in {"sad", "tired"}:
            return "You sound a little down; I'll keep it gentle."
        if affect_label in {"angry", "frustrated", "annoyed"}:
            return "Sounds like this is frustrating; I'll keep it simple."
        if affect_label in {"rushed", "frantic", "urgent"}:
            return "You sound rushed; I'll keep this tight."
        if affect_label in {"fearful", "confused"}:
            return "Sounds like this might feel a little uncertain; I'll keep it clear."
        return f"You sound {affect_label}; I'll keep that in mind."

    def _hermes_body(self, transcript: str, stream: bool, voice_affect: dict[str, Any] | None = None) -> dict[str, Any]:
        model = self.settings.hermes_model.strip() or "hermes-agent"
        messages = [{"role": "system", "content": self._voice_system_prompt(transcript)}]
        affect_prompt = self._voice_affect_prompt(voice_affect)
        if affect_prompt:
            messages.append({"role": "system", "content": affect_prompt})
        messages.append({"role": "user", "content": transcript})
        body = {
            "model": model,
            "reasoning_effort": self.settings.hermes_reasoning_effort,
            "stream": stream,
            "messages": messages,
        }
        provider = self._hermes_provider_override(model)
        if provider:
            body["provider"] = provider
        return body

    async def _answer_and_speak(
        self,
        transcript: str,
        turn_start: float,
        voice_affect: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> tuple[str, dict[str, float]]:
        if self.settings.hermes_streaming:
            try:
                return await self._stream_hermes_and_speak(
                    transcript,
                    turn_start,
                    voice_affect=voice_affect,
                    session_id=session_id,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - turn_start
                LOG.warning("streaming Hermes voice turn timed out after %.1fs; skipping full-response fallback", elapsed)
                self.status.last_error = "Hermes voice stream timed out"
                await self._publish_state("listening", error="Hermes voice stream timed out")
                return "", {"hermes": elapsed, "timeout": 1.0}
            except Exception as stream_exc:
                LOG.warning("streaming Hermes voice turn failed; falling back to full-response path", exc_info=True)

        await self._publish_state("thinking", stage="hermes", transcript=transcript)
        hermes_started = time.monotonic()
        try:
            reply = await self._ask_hermes(transcript, voice_affect=voice_affect, session_id=session_id)
        except Exception as exc:
            LOG.warning("Hermes voice turn failed", exc_info=True)
            self.status.last_error = str(exc)[:500]
            spoken_error = self._spoken_hermes_error(exc)
            await self._publish_state("error", error=self.status.last_error)
            timings = {"hermes": time.monotonic() - hermes_started, "hermes_error": 1.0}
            speak_timings = await self._speak_reply(spoken_error, turn_start)
            timings.update(speak_timings)
            return spoken_error, timings
        acknowledgement = self._voice_affect_acknowledgement(voice_affect)
        if acknowledgement and not reply.startswith(acknowledgement):
            reply = f"{acknowledgement} {reply}".strip()
        timings = {"hermes": time.monotonic() - hermes_started}
        if not reply:
            return "", timings
        await self._publish_state("thinking", stage="tts", transcript=transcript)
        speak_timings = await self._speak_reply(reply, turn_start)
        timings.update(speak_timings)
        if speak_timings.get("interrupted"):
            return "", timings
        return reply, timings

    def _spoken_hermes_error(self, exc: Exception) -> str:
        message = str(exc)
        model = self.settings.hermes_model.strip() or "the selected model"
        if re.search(r"not found the model|permission denied|model .*not found|404", message, flags=re.I):
            return (
                f"Hermes rejected {model}. It is either not configured for this Hermes install "
                "or the current provider key does not have access. I switched into an error state instead of guessing."
            )
        if re.search(r"401|403|unauthorized|forbidden|api key|authentication", message, flags=re.I):
            return "Hermes rejected the API credentials for this voice session. Check the Hermes API key in setup."
        return "Hermes returned an error before I could answer. Check the voice status panel or logs for the exact error."

    async def _ask_hermes(
        self,
        transcript: str,
        voice_affect: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        headers = self._hermes_headers(session_id=session_id)
        body = self._hermes_body(transcript, stream=False, voice_affect=voice_affect)
        LOG.info(
            "livekit hermes request start session=%s model=%s stream=false transcript_chars=%d",
            session_id or self.settings.hermes_session_id,
            body.get("model"),
            len(transcript),
        )
        timeout = ClientTimeout(total=180)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(self.settings.hermes_api_url, headers=headers, json=body) as response:
                payload = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"Hermes API {response.status}: {payload[:500]}")
                data = json.loads(payload)
        return str(data["choices"][0]["message"]["content"]).strip()

    async def _answer_with_kimi_audio_conversation(
        self,
        *,
        pcm: bytes,
        transcript: str,
        turn_start: float,
        voice_affect: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, float]]:
        if not self.settings.kimi_audio_replicate_token:
            raise RuntimeError("Replicate token is not configured for Kimi-Audio")
        await self._publish_state("thinking", stage="hermes", transcript=transcript)
        started = time.monotonic()
        affect_hint = ""
        if voice_affect:
            visible = {key: voice_affect.get(key) for key in ("affect", "arousal", "pace", "urgency", "confidence") if key in voice_affect}
            affect_hint = f"\nVoice-affect context, if useful for tone: {json.dumps(visible, ensure_ascii=False, sort_keys=True)}"
        prompt = (
            "You are Hermes Voice, a concise voice assistant. Listen to the user's audio and answer in natural spoken English. "
            "Keep the reply to one or two conversational sentences unless the user explicitly asks for detail. "
            "Use complete sentences and finish naturally; do not trail off, stop mid-word, or end on an unfinished phrase. "
            "Return both text and speech. Do not add labels, stage directions, or markdown. "
            f"Primary STT transcript for reference: {transcript!r}.{affect_hint}"
        )
        prediction = await self._run_replicate_kimi_audio(
            self._audio_data_uri_from_pcm(pcm),
            prompt,
            output_type="both",
            return_json=True,
        )
        self.status.last_kimi_audio_status = {
            "status": prediction.get("status"),
            "id": prediction.get("id"),
            "error": prediction.get("error"),
            "metrics": prediction.get("metrics"),
            "mode": "conversation",
            "at": time.time(),
        }
        media_url = self._extract_replicate_media_url(prediction)
        if not media_url:
            raise RuntimeError(f"Kimi-Audio conversation returned no media URL: {str(prediction.get('output'))[:500]}")
        reply = self._clean_replicate_text(self._extract_replicate_text(prediction)) or "Done."
        tts_path = await asyncio.to_thread(self._download_audio_to_temp, media_url, ".wav")
        timings = {
            "hermes": time.monotonic() - started,
            "hermes_first_sentence": time.monotonic() - started,
            "tts": time.monotonic() - started,
            "tts_chunks": 1.0,
            "kimi_audio_conversation": 1.0,
        }
        try:
            self._speaking.set()
            await self._publish_state("speaking")
            playback_started = time.monotonic()
            playback_timings = await self._play_audio_file(tts_path, manage_speaking=False)
            timings["first_audio"] = playback_started - turn_start + playback_timings["first_frame"]
            timings["playback"] = playback_timings["duration"]
            timings["playback_done"] = time.monotonic() - turn_start
            timings["interrupted"] = playback_timings.get("interrupted", 0.0)
            if timings["interrupted"]:
                return "", timings
            return reply, timings
        finally:
            self._speaking.clear()
            with contextlib.suppress(OSError):
                Path(tts_path).unlink()

    async def _stream_hermes_and_speak(
        self,
        transcript: str,
        turn_start: float,
        voice_affect: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> tuple[str, dict[str, float]]:
        sentence_queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
        timings: dict[str, float] = {}
        hermes_started = time.monotonic()
        first_sentence_at: float | None = None
        acknowledgement = self._voice_affect_acknowledgement(voice_affect)

        async def producer() -> str:
            nonlocal first_sentence_at
            reply_parts: list[str] = []
            pending_text = ""
            try:
                if acknowledgement:
                    await sentence_queue.put({"text": acknowledgement, "kind": "ack"})
                async for delta in self._stream_hermes_deltas(transcript, voice_affect=voice_affect, session_id=session_id):
                    if not delta:
                        continue
                    reply_parts.append(delta)
                    pending_text += delta
                    sentences, pending_text = self._pop_complete_tts_sentences(pending_text)
                    for sentence in sentences:
                        if first_sentence_at is None:
                            first_sentence_at = time.monotonic()
                            timings["hermes_first_sentence"] = first_sentence_at - hermes_started
                        await sentence_queue.put({"text": sentence, "kind": "reply"})

                final_tail = pending_text.strip()
                if final_tail:
                    if not re.search(r"[.!?;:]$", final_tail):
                        final_tail += "."
                    if first_sentence_at is None:
                        first_sentence_at = time.monotonic()
                        timings["hermes_first_sentence"] = first_sentence_at - hermes_started
                    await sentence_queue.put({"text": final_tail, "kind": "reply"})

                timings["hermes"] = time.monotonic() - hermes_started
                reply = "".join(reply_parts).strip()
                if acknowledgement and not reply.startswith(acknowledgement):
                    return f"{acknowledgement} {reply}".strip()
                return reply
            finally:
                await sentence_queue.put(None)

        producer_task = asyncio.create_task(producer())
        try:
            speak_timings = await self._speak_sentence_queue(sentence_queue, turn_start)
            if speak_timings.get("interrupted"):
                timings.update(speak_timings)
                return "", timings
            try:
                reply = await producer_task
            except Exception:
                if speak_timings.get("first_audio", 0.0) > 0:
                    timings.update(speak_timings)
                    return "", timings
                raise
        finally:
            if not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer_task
        timings.update(speak_timings)
        return reply, timings

    @staticmethod
    def _pop_complete_tts_sentences(text: str) -> tuple[list[str], str]:
        text = re.sub(r"\s*\n+\s*", " ", text)
        sentences: list[str] = []
        while True:
            match = re.search(r"(.+?[.!?;:])(?:\s+|$)", text, flags=re.S)
            if not match:
                return sentences, text
            sentence = re.sub(r"\s+", " ", match.group(1)).strip()
            if sentence:
                sentences.append(sentence)
            text = text[match.end():]

    async def _speak_sentence_queue(
        self,
        sentence_queue: "asyncio.Queue[dict[str, str] | None]",
        turn_start: float,
    ) -> dict[str, float]:
        tts_total = 0.0
        tts_chunks = 0
        underruns = 0
        first_audio = None
        total_playback = 0.0
        generated_paths: list[str] = []
        next_task: asyncio.Task[tuple[str, str] | None] | None = None

        async def synth_next() -> tuple[str, str] | None:
            nonlocal tts_total, tts_chunks
            item = await sentence_queue.get()
            if item is None:
                return None
            sentence = item["text"]
            kind = item.get("kind", "reply")
            started = time.monotonic()
            if self.settings.livekit_tts_backend.strip().lower() == "kimi_audio":
                try:
                    path = await self._synthesize_kimi_audio_tts(sentence)
                except Exception as exc:
                    LOG.warning("Kimi-Audio sentence TTS failed; falling back to native TTS: %s", exc, exc_info=True)
                    path = await asyncio.to_thread(self._synthesize_native_tts, sentence)
            else:
                path = await asyncio.to_thread(self._synthesize_sentence_audio, sentence)
            tts_total += time.monotonic() - started
            tts_chunks += 1
            generated_paths.append(path)
            return path, kind

        current_item = await synth_next()
        if current_item is None:
            return {"tts": 0.0, "tts_chunks": 0.0, "first_audio": 0.0, "playback": 0.0, "playback_done": time.monotonic() - turn_start}

        try:
            while current_item is not None:
                if self._interrupt_requested.is_set():
                    self._speaking.clear()
                    return {
                        "tts": tts_total,
                        "tts_chunks": float(tts_chunks),
                        "tts_underruns": float(underruns),
                        "first_audio": first_audio or 0.0,
                        "playback": total_playback,
                        "playback_done": time.monotonic() - turn_start,
                        "interrupted": 1.0,
                    }
                current_path, current_kind = current_item
                next_task = asyncio.create_task(synth_next())
                self._speaking.set()
                await self._publish_state("speaking")
                playback_started = time.monotonic()
                playback_timings = await self._play_audio_file(current_path, manage_speaking=False)
                if first_audio is None:
                    first_audio = playback_started - turn_start + playback_timings["first_frame"]
                total_playback += playback_timings["duration"]
                try:
                    Path(current_path).unlink()
                    generated_paths.remove(current_path)
                except (OSError, ValueError):
                    pass
                self._speaking.clear()

                if playback_timings.get("interrupted"):
                    return {
                        "tts": tts_total,
                        "tts_chunks": float(tts_chunks),
                        "tts_underruns": float(underruns),
                        "first_audio": first_audio or 0.0,
                        "playback": total_playback,
                        "playback_done": time.monotonic() - turn_start,
                        "interrupted": 1.0,
                    }

                if current_kind == "ack":
                    await self._publish_state("thinking", stage="hermes")
                    await asyncio.sleep(ACK_THINKING_HOLD_SECONDS)
                elif next_task is not None and not next_task.done():
                    await self._publish_state("thinking", stage="hermes")

                wait_started = time.monotonic()
                current_item = await next_task
                next_task = None
                if current_item is not None and time.monotonic() - wait_started > 0.05:
                    underruns += 1

            return {
                "tts": tts_total,
                "tts_chunks": float(tts_chunks),
                "tts_underruns": float(underruns),
                "first_audio": first_audio or 0.0,
                "playback": total_playback,
                "playback_done": time.monotonic() - turn_start,
                "interrupted": 0.0,
            }
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await next_task
            self._speaking.clear()
            for path in list(generated_paths):
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    def _synthesize_sentence_audio(self, sentence: str) -> str:
        return self._synthesize_native_tts(sentence)

    async def _stream_hermes_deltas(
        self,
        transcript: str,
        voice_affect: dict[str, Any] | None = None,
        session_id: str | None = None,
    ):
        active_session_id = session_id or self.settings.hermes_session_id
        headers = self._hermes_headers(session_id=active_session_id)
        body = self._hermes_body(transcript, stream=True, voice_affect=voice_affect)
        timeout = ClientTimeout(total=180)
        line_buffer = ""
        event_type = "message"
        published_tool_cues: set[str] = set()
        trace_id = uuid.uuid4().hex[:8]
        request_started = time.monotonic()
        first_chunk_logged = False
        first_event_logged = False
        first_content_logged = False
        first_tool_logged = False
        LOG.info(
            "voice-debug %s livekit hermes stream start session=%s model=%s transcript_chars=%d",
            trace_id,
            active_session_id,
            body.get("model"),
            len(transcript),
        )
        async with ClientSession(timeout=timeout) as session:
            async with session.post(self.settings.hermes_api_url, headers=headers, json=body) as response:
                LOG.info(
                    "voice-debug %s livekit hermes headers status=%s elapsed=%.3fs",
                    trace_id,
                    response.status,
                    time.monotonic() - request_started,
                )
                if response.status >= 400:
                    payload = await response.text()
                    raise RuntimeError(f"Hermes API {response.status}: {payload[:500]}")
                await self._publish_state("thinking", stage="hermes", transcript=transcript)
                async for raw in response.content.iter_any():
                    if not raw:
                        continue
                    if not first_chunk_logged:
                        first_chunk_logged = True
                        LOG.info(
                            "voice-debug %s livekit first_sse_bytes elapsed=%.3fs bytes=%d",
                            trace_id,
                            time.monotonic() - request_started,
                            len(raw),
                        )
                    line_buffer += raw.decode("utf-8", "replace")
                    while "\n" in line_buffer:
                        line, line_buffer = line_buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            event_type = "message"
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip() or "message"
                            if not first_event_logged:
                                first_event_logged = True
                                LOG.info(
                                    "voice-debug %s livekit first_sse_event event=%s elapsed=%.3fs",
                                    trace_id,
                                    event_type,
                                    time.monotonic() - request_started,
                                )
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_text = line[5:].strip()
                        if not data_text or data_text == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_text)
                        except json.JSONDecodeError:
                            continue
                        if event_type in {"hermes.tool.generating", "hermes.tool.progress"}:
                            name = str(data.get("tool") or data.get("name") or "tool")
                            emoji = str(data.get("emoji") or "").strip() or self._tool_emoji_for_name(name)
                            stage = "generating" if event_type == "hermes.tool.generating" else "started"
                            label = str(data.get("label") or data.get("preview") or "").strip()
                            if not first_tool_logged:
                                first_tool_logged = True
                                LOG.info(
                                    "voice-debug %s livekit first_tool_event tool=%s stage=%s elapsed=%.3fs",
                                    trace_id,
                                    name,
                                    stage,
                                    time.monotonic() - request_started,
                                )
                            await self._publish_tool_cue(emoji, name, stage=stage, label=label)
                            event_type = "message"
                            continue
                        for choice in data.get("choices", []):
                            delta = choice.get("delta") or {}
                            for emoji, name in self._tool_cues_from_delta(delta):
                                cue_key = f"{emoji}:{name}"
                                if cue_key not in published_tool_cues:
                                    published_tool_cues.add(cue_key)
                                    await self._publish_tool_cue(emoji, name)
                            content = delta.get("content")
                            if content:
                                if not first_content_logged:
                                    first_content_logged = True
                                    LOG.info(
                                        "voice-debug %s livekit first_content_delta elapsed=%.3fs chars=%d",
                                        trace_id,
                                        time.monotonic() - request_started,
                                        len(str(content)),
                                    )
                                yield str(content)
        if not first_content_logged and not first_tool_logged:
            raise RuntimeError(
                f"Hermes stream ended without content for model {body.get('model')}. "
                "The selected model may be unavailable or rejected by the provider."
            )
        LOG.info(
            "voice-debug %s livekit hermes stream done elapsed=%.3fs",
            trace_id,
            time.monotonic() - request_started,
        )

    async def _speak_reply(self, text: str, turn_start: float) -> dict[str, float]:
        backend = _normalize_tts_backend(self.settings.livekit_tts_backend)
        if backend == "kimi_audio":
            try:
                return await self._speak_kimi_audio(text, turn_start)
            except Exception as exc:
                LOG.warning("Kimi-Audio TTS failed; falling back to native TTS: %s", exc, exc_info=True)
                timings = await self._speak_native_tts(text, turn_start)
                timings["tts_fallback"] = 1.0
                return timings
        if backend != NATIVE_TTS_BACKEND:
            LOG.warning("unknown LiveKit TTS backend %s; using native TTS", backend)
        return await self._speak_native_tts(text, turn_start)

    async def _speak_kimi_audio(self, text: str, turn_start: float) -> dict[str, float]:
        tts_started = time.monotonic()
        tts_path = await self._synthesize_kimi_audio_tts(text)
        timings = {"tts": time.monotonic() - tts_started, "tts_chunks": 1.0}
        try:
            playback_timings = await self._play_audio_file(tts_path)
            timings.update(playback_timings)
            timings["playback_done"] = time.monotonic() - turn_start
            return timings
        finally:
            with contextlib.suppress(OSError):
                Path(tts_path).unlink()

    async def _synthesize_kimi_audio_tts(self, text: str) -> str:
        if not self.settings.kimi_audio_replicate_token:
            raise RuntimeError("Replicate token is not configured for Kimi-Audio TTS")
        cleaned = self._clean_text_for_tts(text[:1200])
        if not cleaned:
            raise RuntimeError("No speakable text")
        prompt = (
            "Generate a natural spoken audio reply in a clear conversational assistant voice. "
            "Read only the following text; do not add commentary, labels, or sound effects.\n\n"
            f"{cleaned}"
        )
        prediction = await self._run_replicate_kimi_audio(
            self._silent_audio_data_uri(),
            prompt,
            output_type="audio",
            return_json=True,
        )
        self.status.last_kimi_audio_status = {
            "status": prediction.get("status"),
            "id": prediction.get("id"),
            "error": prediction.get("error"),
            "metrics": prediction.get("metrics"),
            "mode": "tts",
            "at": time.time(),
        }
        media_url = self._extract_replicate_media_url(prediction)
        if not media_url:
            raise RuntimeError(f"Kimi-Audio TTS returned no media URL: {str(prediction.get('output'))[:500]}")
        return await asyncio.to_thread(self._download_audio_to_temp, media_url, ".wav")

    async def _speak_native_tts(self, text: str, turn_start: float) -> dict[str, float]:
        tts_started = time.monotonic()
        tts_path = await asyncio.to_thread(self._synthesize_native_tts, text)
        try:
            timings = {"tts": time.monotonic() - tts_started, "tts_chunks": 1.0}
            await self._publish_state("speaking")
            playback_started = time.monotonic()
            playback_timings = await self._play_audio_file(tts_path)
            timings["first_audio"] = playback_started - turn_start + playback_timings["first_frame"]
            timings["playback"] = playback_timings["duration"]
            timings["playback_done"] = time.monotonic() - turn_start
            timings["interrupted"] = float(playback_timings.get("interrupted", 0.0))
            return timings
        finally:
            try:
                Path(tts_path).unlink()
            except OSError:
                pass

    def _clean_text_for_tts(self, text: str) -> str:
        markup = self.settings.livekit_tts_markup.strip().lower()
        if markup in {"inworld", "auto"}:
            return self._strip_markdown_preserving_inworld_tts(text)

        try:
            from tools.tts_tool import _strip_markdown_for_tts

            return _strip_markdown_for_tts(text)
        except ImportError:
            return self._strip_markdown_preserving_inworld_tts(text)

    @staticmethod
    def _strip_markdown_preserving_inworld_tts(text: str) -> str:
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"https?://\S+|www\.\S+", "", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}\d+[.)]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s{0,3}[-*_]{3,}\s*$", " ", text, flags=re.MULTILINE)
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)
        text = re.sub(r"__([^_\n]+)__", r"*\1*", text)
        text = re.sub(r"<(?!\s*break\b)[^>]+>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _synthesize_native_tts(self, text: str) -> str:
        cleaned = self._clean_text_for_tts(text[:3500])
        if not cleaned:
            raise RuntimeError("No speakable text")
        response_format = self.settings.livekit_tts_response_format.lower().strip() or "mp3"
        suffix = ".ogg" if response_format in {"ogg", "opus"} else f".{response_format}"
        out = tempfile.NamedTemporaryFile(prefix="hermes_livekit_tts_", suffix=suffix, delete=False)
        out.close()
        payload = {
            "model": self.settings.livekit_tts_model,
            "input": cleaned,
            # Empty voice lets the native TTS service use its saved/default voice.
            "voice": self.settings.livekit_tts_voice,
            "response_format": response_format,
            "speed": self.settings.livekit_tts_speed,
        }
        request = urllib_request.Request(
            self.settings.livekit_tts_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.settings.livekit_tts_timeout_seconds) as response:
                Path(out.name).write_bytes(response.read())
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"TTS endpoint {exc.code}: {detail[:500]}") from exc
        except Exception as exc:
            raise RuntimeError(f"TTS endpoint unavailable: {type(exc).__name__}: {exc}") from exc
        if not Path(out.name).exists() or Path(out.name).stat().st_size <= 0:
            raise RuntimeError(f"TTS output missing: {out.name}")
        return out.name

    async def _play_audio_file(self, audio_path: str, manage_speaking: bool = True) -> dict[str, float]:
        if not self.audio_source:
            return {"first_frame": 0.0, "duration": 0.0}

        if manage_speaking:
            self._speaking.set()
        started = time.monotonic()
        first_frame_at: float | None = None
        frame_count = 0
        capture_stalled = False
        interrupted = False
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                audio_path,
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            while True:
                if self._interrupt_requested.is_set():
                    interrupted = True
                    break
                chunk = await proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                if self._interrupt_requested.is_set():
                    interrupted = True
                    break
                if len(chunk) < FRAME_BYTES:
                    chunk = chunk + (b"\x00" * (FRAME_BYTES - len(chunk)))
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=SAMPLE_RATE,
                    num_channels=CHANNELS,
                    samples_per_channel=FRAME_SAMPLES,
                )
                try:
                    await asyncio.wait_for(self.audio_source.capture_frame(frame), timeout=2.0)
                except asyncio.TimeoutError:
                    capture_stalled = True
                    LOG.warning("LiveKit audio capture stalled after %d frame(s); ending playback early", frame_count)
                    break
                frame_count += 1
                if first_frame_at is None:
                    first_frame_at = time.monotonic()
            if interrupted and self.audio_source:
                with contextlib.suppress(Exception):
                    self.audio_source.clear_queue()
            if (capture_stalled or interrupted) and proc.returncode is None:
                proc.kill()
            expected_playout = max(0.0, frame_count * FRAME_MS / 1000)
            if not interrupted:
                try:
                    await asyncio.wait_for(self.audio_source.wait_for_playout(), timeout=max(2.0, expected_playout + 2.0))
                except asyncio.TimeoutError:
                    LOG.warning("LiveKit audio playout wait timed out after %.2fs expected audio", expected_playout)
            await proc.wait()
            if proc.returncode:
                stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
                if not capture_stalled and not interrupted:
                    raise RuntimeError(f"ffmpeg playback conversion failed: {stderr[:400]}")
            return {
                "first_frame": (first_frame_at - started) if first_frame_at else 0.0,
                "duration": time.monotonic() - started,
                "interrupted": 1.0 if interrupted else 0.0,
            }
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if manage_speaking:
                self._speaking.clear()


def create_app(bot: HermesLiveKitVoice) -> web.Application:
    app = web.Application()

    def discovery_hosts(extra_cidrs: list[str]) -> list[str]:
        hosts: list[str] = []
        for host in DEFAULT_DISCOVERY_HOSTS:
            hosts.append(host)
        for cidr in [*bot.settings.discovery_cidrs, *extra_cidrs]:
            with contextlib.suppress(ValueError):
                network = ipaddress.ip_network(cidr, strict=False)
                if network.version != 4:
                    continue
                if not bot._private_discovery_network(network):
                    LOG.info("skipping non-private discovery CIDR %s", cidr)
                    continue
                if network.num_addresses > bot.settings.discovery_max_hosts + 2:
                    LOG.info("skipping discovery CIDR %s with %s addresses", cidr, network.num_addresses)
                    continue
                if len(hosts) >= bot.settings.discovery_max_hosts:
                    LOG.info("discovery host limit reached at %s hosts", len(hosts))
                    break
                for ip in network.hosts():
                    if len(hosts) >= bot.settings.discovery_max_hosts:
                        break
                    hosts.append(str(ip))
        deduped: list[str] = []
        seen: set[str] = set()
        for host in hosts:
            if host not in seen:
                deduped.append(host)
                seen.add(host)
        return deduped

    async def probe_candidate(
        session: ClientSession,
        semaphore: asyncio.Semaphore,
        host: str,
        port: int,
        api_key: str,
    ) -> dict[str, Any] | None:
        url = _normalize_hermes_api_url(f"http://{host}:{port}")
        models_url = _models_url_for_api_url(url)
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with semaphore:
            try:
                async with session.get(models_url, headers=headers) as response:
                    if response.status not in {200, 401, 403}:
                        return None
                    model_ids: list[str] = []
                    if response.status == 200:
                        with contextlib.suppress(Exception):
                            payload = await response.json()
                            if isinstance(payload, dict):
                                for item in payload.get("data") or []:
                                    if isinstance(item, dict) and item.get("id"):
                                        model_ids.append(str(item["id"]))
                    return {
                        "url": url,
                        "modelsUrl": models_url,
                        "host": host,
                        "port": port,
                        "status": response.status,
                        "authRequired": response.status in {401, 403},
                        "models": model_ids[:12],
                    }
            except (asyncio.TimeoutError, ClientError, socket.gaierror, OSError):
                return None

    async def index(_: web.Request) -> web.Response:
        if bot.settings.setup_required():
            setup_page = bot.settings.static_dir / "setup.html"
            if setup_page.exists():
                return web.FileResponse(setup_page, headers={"Cache-Control": "no-store"})
        return web.FileResponse(bot.settings.static_dir / "index.html", headers={"Cache-Control": "no-store"})

    async def setup_page(_: web.Request) -> web.Response:
        setup_html = bot.settings.static_dir / "setup.html"
        if not setup_html.exists():
            raise web.HTTPNotFound(text="Setup page not found")
        return web.FileResponse(setup_html, headers={"Cache-Control": "no-store"})

    async def health(_: web.Request) -> web.Response:
        if bot.settings.setup_required():
            status = "setup_required"
        else:
            status = "ok" if bot.status.connected else "starting"
        return web.json_response(
            {
                "status": status,
                "setupRequired": bot.settings.setup_required(),
                "missingSettings": bot.settings.missing_required_settings(),
                "activeModel": bot.settings.hermes_model,
                "modelProvider": bot._model_provider(),
                "ttsBackend": bot.settings.livekit_tts_backend,
                "ttsUrl": bot.settings.livekit_tts_url,
                "ttsVoice": bot.settings.livekit_tts_voice,
                "ttsSpeed": bot.settings.livekit_tts_speed,
                "sttProvider": bot.settings.stt_provider,
                "sttModel": bot.settings.stt_model,
                "hermesStreaming": bot.settings.hermes_streaming,
                "emotionRecognitionEnabled": bot.settings.emotion2vec_enabled,
                "emotionRecognitionAvailable": bot._emotion2vec_available(),
                "emotionRecognitionProvider": "local",
                **bot.status.to_dict(),
            }
        )

    async def config(_: web.Request) -> web.Response:
        await bot.refresh_remote_model_choices()
        return web.json_response(
            {
                "livekitUrl": bot.settings.livekit_url,
                "livekitPublicUrl": bot.settings.livekit_public_url,
                "room": bot.settings.livekit_room,
                "agentName": bot.settings.agent_name,
                "setupRequired": bot.settings.setup_required(),
                "missingSettings": bot.settings.missing_required_settings(),
                "settings": bot._settings_payload(),
            }
        )

    async def setup_status(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "setupRequired": bot.settings.setup_required(),
                "setupTokenRequired": bot.setup_token_required(),
                "missingSettings": bot.settings.missing_required_settings(),
                "hermesApiUrl": bot.settings.hermes_api_url,
                "livekitPublicUrl": bot.settings.livekit_public_url,
                "ttsUrl": bot.settings.livekit_tts_url,
                "emotionRecognition": bot.settings.emotion2vec_enabled,
                "discoveryCidrs": ",".join(bot.settings.discovery_cidrs),
                "discoveryPorts": ",".join(str(port) for port in bot.settings.discovery_ports),
            }
        )

    async def setup_discover(request: web.Request) -> web.Response:
        bot.require_setup_token(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected JSON object")
        api_key = str(body.get("hermesApiKey") or "").strip()
        extra_cidrs = _split_csv(str(body.get("cidrs") or ""))
        ports = _parse_discovery_ports(str(body.get("ports") or ",".join(str(port) for port in bot.settings.discovery_ports)))
        hosts = discovery_hosts(extra_cidrs)
        timeout = ClientTimeout(total=12, connect=1.0, sock_read=2.0)
        semaphore = asyncio.Semaphore(64)
        async with ClientSession(timeout=timeout) as session:
            tasks = [
                probe_candidate(session, semaphore, host, port, api_key)
                for host in hosts
                for port in ports
            ]
            results = [result for result in await asyncio.gather(*tasks) if result]
        results.sort(key=lambda item: (item["authRequired"], item["host"] not in DEFAULT_DISCOVERY_HOSTS, item["host"], item["port"]))
        return web.json_response({"candidates": results, "scannedHosts": len(hosts), "ports": ports})

    async def setup_save(request: web.Request) -> web.Response:
        bot.require_setup_token(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="Expected JSON body")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected JSON object")

        hermes_api_url = _normalize_hermes_api_url(str(body.get("hermesApiUrl") or ""))
        hermes_api_key = str(body.get("hermesApiKey") or "").strip()
        livekit_public_url = str(body.get("livekitPublicUrl") or "").strip()
        tts_url = str(body.get("ttsUrl") or "").strip()
        stt_provider = str(body.get("sttProvider") or "auto").strip().lower() or "auto"
        stt_model = str(body.get("sttModel") or "").strip()
        emotion_enabled = bool(body.get("emotionRecognition", True))

        if hermes_api_url and not re.match(r"^https?://", hermes_api_url):
            raise web.HTTPBadRequest(text="Hermes API URL must be an HTTP URL or host:port")
        if not hermes_api_key:
            raise web.HTTPBadRequest(text="Hermes API key is required")
        if livekit_public_url and not re.match(r"^wss?://", livekit_public_url):
            raise web.HTTPBadRequest(text="LiveKit public URL must start with ws:// or wss://")
        if tts_url and not re.match(r"^https?://", tts_url):
            raise web.HTTPBadRequest(text="TTS URL must start with http:// or https://")
        if stt_provider not in {"auto", "local", "openai", "groq", "mistral", "xai"}:
            raise web.HTTPBadRequest(text="Unsupported STT provider")

        updates: dict[str, Any] = {
            "HERMES_API_KEY": hermes_api_key,
            "HERMES_LIVEKIT_STT_PROVIDER": stt_provider,
            "HERMES_LIVEKIT_STT_MODEL": stt_model,
            "HERMES_EMOTION2VEC_ENABLED": "true" if emotion_enabled else "false",
        }
        if hermes_api_url:
            updates["HERMES_API_URL"] = hermes_api_url
        if livekit_public_url:
            updates["LIVEKIT_PUBLIC_URL"] = livekit_public_url
        if tts_url:
            updates["HERMES_LIVEKIT_TTS_URL"] = tts_url
        _write_env_updates(bot.settings.env_path, updates, allowed_keys=SETUP_ENV_KEYS)

        if bool(body.get("restart", True)):
            loop = asyncio.get_running_loop()
            loop.call_later(0.2, request.app["stop_event"].set)
        return web.json_response({"ok": True, "restartScheduled": bool(body.get("restart", True))})

    async def settings_get(_: web.Request) -> web.Response:
        if bot.settings.setup_required():
            raise web.HTTPServiceUnavailable(text="Setup is required")
        await bot.refresh_remote_model_choices()
        return web.json_response(bot._settings_payload())

    async def settings_patch(request: web.Request) -> web.Response:
        bot.require_setup_token(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="Expected JSON body")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected JSON object")
        try:
            payload = bot._apply_settings_update(body)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f"Could not persist settings: {exc}")
        return web.json_response(payload)

    async def update_check(request: web.Request) -> web.Response:
        bot.require_setup_token(request)
        return web.json_response(await bot.check_update())

    async def update_run(request: web.Request) -> web.Response:
        bot.require_setup_token(request)
        return web.json_response(await bot.run_update())

    async def token(request: web.Request) -> web.Response:
        if bot.settings.setup_required():
            raise web.HTTPServiceUnavailable(text="Setup is required")
        body = await request.json()
        name = str(body.get("name") or "Guest").strip()[:80]
        identity_prefix = "hermes-livekit-desktop" if bool(body.get("desktop")) else "hermes-livekit-web"
        identity = f"{identity_prefix}-{uuid.uuid4().hex[:8]}"
        jwt = bot.make_token(identity=identity, name=name)
        return web.json_response(
            {
                "token": jwt,
                "url": bot.settings.livekit_public_url,
                "room": bot.settings.livekit_room,
                "identity": identity,
            }
        )

    async def text_turn(request: web.Request) -> web.Response:
        if bot.settings.setup_required():
            raise web.HTTPServiceUnavailable(text="Setup is required")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="Expected JSON body")

        text = str(body.get("text") or "").strip()
        attachments = _attachments_prompt(body.get("attachments"))
        if attachments:
            text = f"{text}{attachments}".strip()
        identity = str(body.get("identity") or "typed-web").strip()[:80] or "typed-web"
        session_id = str(body.get("sessionId") or body.get("session_id") or "").strip()[:120] or None
        try:
            result = await bot._process_text_turn(text, identity=identity, session_id=session_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except RuntimeError as exc:
            raise web.HTTPConflict(text=str(exc))
        return web.json_response(result)

    app.router.add_get("/", index)
    app.router.add_get("/setup", setup_page)
    app.router.add_get("/health", health)
    app.router.add_get("/config", config)
    app.router.add_get("/setup/status", setup_status)
    app.router.add_post("/setup/discover", setup_discover)
    app.router.add_post("/setup", setup_save)
    app.router.add_get("/settings", settings_get)
    app.router.add_patch("/settings", settings_patch)
    app.router.add_get("/update", update_check)
    app.router.add_post("/update", update_run)
    app.router.add_post("/token", token)
    app.router.add_post("/text-turn", text_turn)
    app.router.add_static("/static", bot.settings.static_dir, show_index=False)
    return app


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=str(Path.home() / ".hermes/livekit-voice/.env"))
    parser.add_argument("--log-level", default=_env("HERMES_LIVEKIT_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _raise_nofile_limit()

    settings = Settings.load(Path(args.env))
    bot = HermesLiveKitVoice(settings)
    if settings.setup_required():
        bot.status.agent_state = "setup_required"
        bot.status.last_error = "Setup is required: " + ", ".join(settings.missing_required_settings())
        LOG.warning(bot.status.last_error)
    else:
        await bot.start()

    app = create_app(bot)
    stop_event = asyncio.Event()
    app["stop_event"] = stop_event
    runner = web.AppRunner(app, keepalive_timeout=8)
    await runner.setup()
    sites = []
    for host in [value.strip() for value in settings.host.split(",") if value.strip()]:
        site = web.TCPSite(runner, host, settings.port)
        await site.start()
        sites.append(site)
        LOG.info("web client listening on http://%s:%d", host, settings.port)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
        if bot.status.connected:
            await bot.stop()


if __name__ == "__main__":
    asyncio.run(_main())

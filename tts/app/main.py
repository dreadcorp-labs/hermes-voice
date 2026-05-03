from __future__ import annotations

import io
import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib import error as urllib_error
from urllib import request as urllib_request

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from kokoro_onnx import Kokoro
from pedalboard import (
    Chorus,
    Compressor,
    Delay,
    Gain,
    HighpassFilter,
    LowpassFilter,
    Pedalboard,
    Phaser,
    PitchShift,
    Reverb,
    load_plugin,
)
from pydantic import BaseModel, Field


logger = logging.getLogger("kokoro-glados-voice")
logging.basicConfig(level=os.getenv("GLAADOS_LOG_LEVEL", "INFO"))

DEFAULT_VOICE = os.getenv("GLAADOS_DEFAULT_VOICE", "bf_emma")
DEFAULT_LANG = os.getenv("GLAADOS_DEFAULT_LANG", "en-gb")
MODEL_PATH = Path(os.getenv("GLAADOS_MODEL_PATH", "/models/kokoro-v1.0.onnx"))
VOICES_PATH = Path(os.getenv("GLAADOS_VOICES_PATH", "/models/voices-v1.0.bin"))
PLUGIN_DIR = Path(os.getenv("GLAADOS_PLUGIN_DIR", "/opt/vst3"))
DATA_DIR = Path(os.getenv("GLAADOS_DATA_DIR", "/data"))
SETTINGS_PATH = Path(os.getenv("GLAADOS_SETTINGS_PATH", str(DATA_DIR / "runtime_settings.json")))
DEFAULT_SETTINGS_PATH = Path(os.getenv("GLAADOS_DEFAULT_SETTINGS_PATH", "/defaults/runtime_settings.json"))
GRAILLON_ENABLED = os.getenv("GLAADOS_GRAILLON_ENABLED", "true").lower() not in {"0", "false", "no"}
MAX_INPUT_CHARS = int(os.getenv("GLAADOS_MAX_INPUT_CHARS", "4000"))
DEFAULT_TUNE_KEY = str(os.getenv("GLAADOS_TUNE_KEY", "G") or "G").strip()
DEFAULT_TUNE_SCALE = str(os.getenv("GLAADOS_TUNE_SCALE", "major") or "major").strip().lower()
DEFAULT_EFFECTS_PRESET = str(
    os.getenv("GLAADOS_EFFECTS_PRESET", "g_major_pressurized") or "g_major_pressurized"
).strip().lower()
DEFAULT_TTS_BACKEND = str(os.getenv("GLAADOS_TTS_BACKEND", "kokoro_glados") or "kokoro_glados").strip().lower()
KOKORO_ONLY_MODE = os.getenv("GLAADOS_KOKORO_ONLY", "true").lower() not in {"0", "false", "no"}
DEFAULT_POCKETTTS_VOICE = str(os.getenv("GLAADOS_POCKETTTS_DEFAULT_VOICE", "vera") or "vera").strip().lower()
POCKETTTS_WARM_ON_START = os.getenv("GLAADOS_POCKETTTS_WARM_ON_START", "false").lower() not in {"0", "false", "no"}
POCKETTTS_WARM_VOICES_RAW = str(os.getenv("GLAADOS_POCKETTTS_WARM_VOICES", DEFAULT_POCKETTTS_VOICE) or DEFAULT_POCKETTTS_VOICE)
INWORLD_API_KEY = str(os.getenv("GLAADOS_INWORLD_API_KEY") or os.getenv("INWORLD_API_KEY") or "").strip()
INWORLD_SYNTH_URL = str(
    os.getenv("GLAADOS_INWORLD_SYNTH_URL", "https://api.inworld.ai/tts/v1/voice")
    or "https://api.inworld.ai/tts/v1/voice"
).strip()
INWORLD_VOICES_URL = str(
    os.getenv("GLAADOS_INWORLD_VOICES_URL", "https://api.inworld.ai/voices/v1/voices")
    or "https://api.inworld.ai/voices/v1/voices"
).strip()
INWORLD_DEFAULT_VOICE = str(os.getenv("GLAADOS_INWORLD_DEFAULT_VOICE", "Wendy") or "Wendy").strip()
INWORLD_DEFAULT_MODEL = str(
    os.getenv("GLAADOS_INWORLD_DEFAULT_MODEL", "inworld-tts-1.5-max") or "inworld-tts-1.5-max"
).strip()
INWORLD_TIMEOUT_SECONDS = float(os.getenv("GLAADOS_INWORLD_TIMEOUT_SECONDS", "30"))
INWORLD_MAX_INPUT_CHARS = int(os.getenv("GLAADOS_INWORLD_MAX_INPUT_CHARS", "2000"))
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")

VOICE_ALIASES = {
    "alloy": DEFAULT_VOICE,
    "glados": DEFAULT_VOICE,
    "glados_emma": DEFAULT_VOICE,
}

CUSTOM_KOKORO_VOICE_BLENDS: dict[str, dict[str, float]] = {
    "bf_emma_isabella_blend": {
        "bf_emma": 0.5,
        "bf_isabella": 0.5,
    },
}

KOKORO_VOICES = [
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    *CUSTOM_KOKORO_VOICE_BLENDS.keys(),
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
]
POCKETTTS_VOICES = [
    "cosette",
    "marius",
    "javert",
    "alba",
    "jean",
    "anna",
    "vera",
    "fantine",
    "charles",
    "paul",
    "eponine",
    "azelma",
    "george",
    "mary",
    "jane",
    "michael",
    "eve",
    "bill_boerst",
    "peter_yearsley",
    "stuart_bell",
    "caro_davy",
]
INWORLD_FALLBACK_VOICES = [
    INWORLD_DEFAULT_VOICE,
    "Alex",
    "Ashley",
    "Darlene",
    "Dennis",
    "Sarah",
]
INWORLD_MODELS = [
    "inworld-tts-1.5-max",
    "inworld-tts-1.5-mini",
]
INWORLD_AUDIO_ENCODINGS = [
    "LINEAR16",
    "MP3",
    "OGG_OPUS",
]
INWORLD_TIMESTAMP_TYPES = [
    "TIMESTAMP_TYPE_UNSPECIFIED",
    "WORD",
    "CHARACTER",
]
INWORLD_TEXT_NORMALIZATION_MODES = [
    "APPLY_TEXT_NORMALIZATION_UNSPECIFIED",
    "ON",
    "OFF",
]

VOICE_LANGUAGE_HINTS = {
    "bf_": "en-gb",
    "bm_": "en-gb",
    "af_": "en-us",
    "am_": "en-us",
}

NOTE_PARAMETER_NAMES = [
    "allow_c",
    "allow_c_sharp",
    "allow_d",
    "allow_d_sharp",
    "allow_e",
    "allow_f",
    "allow_f_sharp",
    "allow_g",
    "allow_g_sharp",
    "allow_a",
    "allow_a_sharp",
    "allow_b",
]

NOTE_INDEX = {
    "C": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
}

AVAILABLE_KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
AVAILABLE_SCALES = ["major", "minor", "natural_minor"]

SCALE_INTERVALS = {
    "major": {0, 2, 4, 5, 7, 9, 11},
    "minor": {0, 2, 3, 5, 7, 8, 10},
    "natural_minor": {0, 2, 3, 5, 7, 8, 10},
}

AVAILABLE_PRESETS = [
    "graillon_only",
    "glados_ambience",
    "g_major_pressurized",
]
AVAILABLE_BACKENDS = {
    "kokoro_glados": {
        "label": "Kokoro + Graillon",
        "description": "Local Kokoro voices rendered through Graillon and post FX.",
        "supports_effects": True,
    },
    "inworld_fx": {
        "label": "Inworld + Graillon",
        "description": "Inworld cloud voices rendered through the same Graillon and post FX chain.",
        "supports_effects": True,
    },
}
if KOKORO_ONLY_MODE:
    AVAILABLE_BACKENDS = {
        "kokoro_glados": {
            "label": "Kokoro + Graillon",
            "description": "Local Kokoro voices rendered through Graillon and post FX.",
            "supports_effects": True,
        },
    }
RETIRED_BACKEND_ALIASES = {
    "pockettts_fx": "kokoro_glados",
}
USER_PRESET_SLOTS = [f"user_slot_{idx}" for idx in range(1, 11)]

app = FastAPI(title="Hermes Voice Dev", version="0.3.0")
_graillon_plugin = None
_graillon_status = "disabled"
_kokoro: Kokoro | None = None
_pocket_model = None
_pocket_voice_states: dict[str, Any] = {}
_pocket_model_key: tuple[Any, ...] | None = None
_pocket_status = "not_loaded"
_inworld_voices: list[str] = list(dict.fromkeys(INWORLD_FALLBACK_VOICES))
_inworld_voice_details: list[dict[str, Any]] = []
_inworld_status = "not_configured" if not INWORLD_API_KEY else "not_loaded"
_runtime_settings: "RuntimeSettings" | None = None
_user_presets: dict[str, "SavedPreset"] = {}
_settings_lock = threading.RLock()
_audio_lock = threading.RLock()
_pocket_lock = threading.RLock()


class SpeechRequest(BaseModel):
    model: str = Field(default="kokoro")
    input: str
    voice: str = Field(default="")
    response_format: str = Field(default="mp3")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class ToggleFilterSettings(BaseModel):
    enabled: bool = False
    cutoff_frequency_hz: float = 0.0


class PedalCompressorSettings(BaseModel):
    enabled: bool = False
    threshold_db: float = -18.0
    ratio: float = 2.0
    attack_ms: float = 4.0
    release_ms: float = 90.0
    makeup_gain_db: float = 0.0


class ChorusSettings(BaseModel):
    enabled: bool = False
    rate_hz: float = 0.28
    depth: float = 0.14
    centre_delay_ms: float = 7.5
    feedback: float = 0.05
    mix: float = 0.05


class PhaserSettings(BaseModel):
    enabled: bool = False
    rate_hz: float = 0.12
    depth: float = 0.18
    centre_frequency_hz: float = 1450.0
    feedback: float = 0.03
    mix: float = 0.03


class DelaySettings(BaseModel):
    enabled: bool = False
    delay_seconds: float = 0.082
    feedback: float = 0.10
    mix: float = 0.07


class ReverbSettings(BaseModel):
    enabled: bool = False
    room_size: float = 0.34
    damping: float = 0.52
    wet_level: float = 0.06
    dry_level: float = 0.94
    width: float = 0.65


class GraillonSettings(BaseModel):
    enabled: bool = True
    key: str = DEFAULT_TUNE_KEY
    scale: str = DEFAULT_TUNE_SCALE
    correction: float = 1.0
    amount: float = 1.0
    snap_max_st: float = 1.0
    snap_min_st: float = 0.0
    smooth: float = 0.0
    inertia: float = 0.0
    dry_mix_db: float = 0.0
    wet_mix_db: float = 0.7079457640647888
    ptm_enabled: bool = False
    formant: float = 0.0
    chorus: float = 0.0
    compressor: float = 0.0


class PocketTTSSettings(BaseModel):
    language: str = "english"
    temperature: float = Field(default=0.7, ge=0.1, le=1.5)
    lsd_decode_steps: int = Field(default=1, ge=1, le=8)
    noise_clamp: float | None = Field(default=None, ge=0.0, le=5.0)
    eos_threshold: float = Field(default=-4.0, ge=-12.0, le=4.0)
    quantize: bool = False
    frames_after_eos: int | None = Field(default=None, ge=0, le=12)
    max_tokens: int = Field(default=50, ge=10, le=200)


class InworldSettings(BaseModel):
    model: str = INWORLD_DEFAULT_MODEL
    audio_encoding: str = "LINEAR16"
    sample_rate_hertz: int = Field(default=24000, ge=8000, le=48000)
    speaking_rate: float = Field(default=1.0, ge=0.25, le=4.0)
    temperature: float = Field(default=1.0, gt=0.0, le=2.0)
    timestamp_type: str = "TIMESTAMP_TYPE_UNSPECIFIED"
    apply_text_normalization: str = "APPLY_TEXT_NORMALIZATION_UNSPECIFIED"


class RuntimeSettings(BaseModel):
    tts_backend: str = DEFAULT_TTS_BACKEND
    effects_preset: str = DEFAULT_EFFECTS_PRESET
    voice: str = INWORLD_DEFAULT_VOICE if DEFAULT_TTS_BACKEND == "inworld_fx" else DEFAULT_VOICE
    pitch_shift_semitones: float = 0.0
    graillon: GraillonSettings = Field(default_factory=GraillonSettings)
    highpass: ToggleFilterSettings = Field(
        default_factory=lambda: ToggleFilterSettings(enabled=False, cutoff_frequency_hz=110.0)
    )
    lowpass: ToggleFilterSettings = Field(
        default_factory=lambda: ToggleFilterSettings(enabled=False, cutoff_frequency_hz=7600.0)
    )
    compressor: PedalCompressorSettings = Field(default_factory=PedalCompressorSettings)
    chorus: ChorusSettings = Field(default_factory=ChorusSettings)
    phaser: PhaserSettings = Field(default_factory=PhaserSettings)
    delay: DelaySettings = Field(default_factory=DelaySettings)
    reverb: ReverbSettings = Field(default_factory=ReverbSettings)
    pockettts: PocketTTSSettings = Field(default_factory=PocketTTSSettings)
    inworld: InworldSettings = Field(default_factory=InworldSettings)


class SavedPreset(BaseModel):
    label: str
    settings: RuntimeSettings


def _iter_chunks(payload: bytes, chunk_size: int = 16384) -> Iterable[bytes]:
    for idx in range(0, len(payload), chunk_size):
        yield payload[idx:idx + chunk_size]


def _resolve_voice(voice: str) -> str:
    voice = str(voice or DEFAULT_VOICE).strip()
    return VOICE_ALIASES.get(voice, voice)


def _voice_for_backend(backend: str, voice: str | None) -> str:
    backend = _normalize_tts_backend(backend)
    voices = _voices_by_backend().get(backend, KOKORO_VOICES)
    fallback = INWORLD_DEFAULT_VOICE if backend == "inworld_fx" else DEFAULT_VOICE
    candidate = str(voice or fallback).strip()
    if backend == "pockettts_fx":
        fallback = DEFAULT_POCKETTTS_VOICE
        candidate = candidate.lower()
    if backend == "kokoro_glados":
        candidate = _resolve_voice(candidate)
    if backend == "inworld_fx":
        for available_voice in voices:
            if available_voice.lower() == candidate.lower():
                return available_voice
    if candidate in voices:
        return candidate
    return fallback if fallback in voices else voices[0]


def _voices_by_backend() -> dict[str, list[str]]:
    kokoro_voices = list(dict.fromkeys(KOKORO_VOICES))
    if _kokoro is not None:
        try:
            kokoro_voices = list(dict.fromkeys([*getattr(_kokoro.voices, "files", []), *CUSTOM_KOKORO_VOICE_BLENDS.keys()]))
        except Exception:
            pass
    voices_by_backend = {
        "kokoro_glados": kokoro_voices,
        "inworld_fx": list(dict.fromkeys([INWORLD_DEFAULT_VOICE, *_inworld_voices])),
    }
    if KOKORO_ONLY_MODE:
        return {"kokoro_glados": kokoro_voices}
    return voices_by_backend


def _resolve_lang(voice: str) -> str:
    voice = _resolve_voice(voice)
    if voice in CUSTOM_KOKORO_VOICE_BLENDS:
        source_voices = CUSTOM_KOKORO_VOICE_BLENDS[voice].keys()
        if all(str(source_voice).startswith(("bf_", "bm_")) for source_voice in source_voices):
            return "en-gb"
    for prefix, lang in VOICE_LANGUAGE_HINTS.items():
        if voice.startswith(prefix):
            return lang
    return DEFAULT_LANG


def _media_type_for(fmt: str) -> str:
    fmt = fmt.lower()
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
        "pcm": "application/octet-stream",
    }.get(fmt, "application/octet-stream")


def _fuzzy_set_parameter(plugin, needles: tuple[str, ...], value: float) -> bool:
    params = getattr(plugin, "parameters", {}) or {}
    for name, parameter in params.items():
        lowered = name.lower()
        if all(needle in lowered for needle in needles):
            try:
                parameter.raw_value = value
                logger.info("Configured Graillon parameter %s=%s", name, value)
                return True
            except Exception as exc:
                logger.warning("Failed to configure Graillon parameter %s: %s", name, exc)
    return False


def _log_plugin_parameters(plugin) -> None:
    params = getattr(plugin, "parameters", {}) or {}
    if not params:
        logger.info("Graillon plugin exposed no parameters")
        return
    for name, parameter in params.items():
        try:
            logger.info(
                "Graillon parameter %s raw=%s text=%s",
                name,
                getattr(parameter, "raw_value", None),
                getattr(parameter, "string_value", None),
            )
        except Exception:
            logger.info("Graillon parameter %s", name)


def _set_named_parameter(plugin, name: str, value: float) -> bool:
    params = getattr(plugin, "parameters", {}) or {}
    parameter = params.get(name)
    if parameter is None:
        return False
    try:
        parameter.raw_value = value
        logger.info("Configured Graillon parameter %s=%s", name, value)
        return True
    except Exception as exc:
        logger.warning("Failed to configure Graillon parameter %s: %s", name, exc)
        return False


def _normalize_key(key_name: str) -> str:
    normalized = str(key_name or "G").strip().upper()
    if len(normalized) > 1 and normalized[1] == "B":
        normalized = normalized[0] + "b"
    if normalized.endswith("B") and len(normalized) == 2:
        normalized = normalized[0] + "b"
    normalized = normalized.replace("b", "B")
    return {
        "DB": "C#",
        "EB": "D#",
        "GB": "F#",
        "AB": "G#",
        "BB": "A#",
    }.get(normalized, normalized)


def _configure_scale(plugin, key_name: str, scale_name: str) -> None:
    tonic = NOTE_INDEX.get(_normalize_key(key_name).upper())
    intervals = SCALE_INTERVALS.get(scale_name.lower())
    if tonic is None or intervals is None:
        logger.warning("Invalid Graillon key/scale config: key=%s scale=%s", key_name, scale_name)
        return

    allowed = {(tonic + interval) % 12 for interval in intervals}
    for idx, parameter_name in enumerate(NOTE_PARAMETER_NAMES):
        _set_named_parameter(plugin, parameter_name, 1.0 if idx in allowed else 0.0)
    logger.info("Configured Graillon scale key=%s scale=%s allowed=%s", key_name, scale_name, sorted(allowed))


def _default_settings() -> RuntimeSettings:
    settings = RuntimeSettings()
    return _coerce_runtime_settings(_apply_preset(DEFAULT_EFFECTS_PRESET, settings))


def _apply_preset(preset_name: str, settings: RuntimeSettings | None = None) -> RuntimeSettings:
    config = settings.model_copy(deep=True) if settings is not None else RuntimeSettings()
    preset = str(preset_name or "graillon_only").strip().lower()

    config.effects_preset = preset
    config.pitch_shift_semitones = 0.0
    config.graillon = GraillonSettings(
        enabled=True,
        key="G" if preset == "g_major_pressurized" else DEFAULT_TUNE_KEY,
        scale="major" if preset == "g_major_pressurized" else DEFAULT_TUNE_SCALE,
        correction=1.0,
        amount=1.0,
        snap_max_st=1.0,
        snap_min_st=0.0,
        smooth=0.0,
        inertia=0.0,
        dry_mix_db=0.0,
        wet_mix_db=0.7079457640647888,
        ptm_enabled=False,
        formant=0.0,
        chorus=0.0,
        compressor=0.0,
    )
    config.highpass = ToggleFilterSettings(enabled=False, cutoff_frequency_hz=110.0)
    config.lowpass = ToggleFilterSettings(enabled=False, cutoff_frequency_hz=7600.0)
    config.compressor = PedalCompressorSettings(enabled=False, threshold_db=-18.0, ratio=2.0, attack_ms=4.0, release_ms=90.0)
    config.chorus = ChorusSettings(enabled=False, rate_hz=0.28, depth=0.14, centre_delay_ms=7.5, feedback=0.05, mix=0.05)
    config.phaser = PhaserSettings(enabled=False, rate_hz=0.12, depth=0.18, centre_frequency_hz=1450.0, feedback=0.03, mix=0.03)
    config.delay = DelaySettings(enabled=False, delay_seconds=0.082, feedback=0.10, mix=0.07)
    config.reverb = ReverbSettings(enabled=False, room_size=0.34, damping=0.52, wet_level=0.06, dry_level=0.94, width=0.65)

    if preset == "glados_ambience":
        config.highpass.enabled = True
        config.lowpass.enabled = True
        config.compressor = PedalCompressorSettings(enabled=True, threshold_db=-19.0, ratio=2.1, attack_ms=4.0, release_ms=90.0)
        config.chorus.enabled = True
        config.phaser = PhaserSettings(enabled=True, rate_hz=0.12, depth=0.24, centre_frequency_hz=1450.0, feedback=0.08, mix=0.04)
        config.delay.enabled = True
        config.reverb.enabled = True
    elif preset == "g_major_pressurized":
        config.graillon.key = "G"
        config.graillon.scale = "major"
        config.compressor = PedalCompressorSettings(enabled=True, threshold_db=-30.0, ratio=6.0, attack_ms=1.5, release_ms=180.0)
        config.phaser = PhaserSettings(enabled=True, rate_hz=0.11, depth=0.15, centre_frequency_hz=1350.0, feedback=0.02, mix=0.022)
    elif preset != "graillon_only":
        logger.warning("Unknown preset %s, falling back to graillon_only", preset)
        config.effects_preset = "graillon_only"

    return config


def _normalize_tts_backend(backend_name: str) -> str:
    backend = str(backend_name or DEFAULT_TTS_BACKEND).strip().lower()
    backend = RETIRED_BACKEND_ALIASES.get(backend, backend)
    if backend not in AVAILABLE_BACKENDS:
        raise ValueError(f"unknown tts backend: {backend_name}")
    return backend


def _coerce_runtime_settings(settings: RuntimeSettings) -> RuntimeSettings:
    settings = settings.model_copy(deep=True)
    if KOKORO_ONLY_MODE:
        settings.tts_backend = "kokoro_glados"
    settings.tts_backend = _normalize_tts_backend(settings.tts_backend)
    if settings.inworld.model not in INWORLD_MODELS:
        settings.inworld.model = INWORLD_DEFAULT_MODEL
    if settings.inworld.audio_encoding not in INWORLD_AUDIO_ENCODINGS:
        settings.inworld.audio_encoding = "LINEAR16"
    if settings.inworld.timestamp_type not in INWORLD_TIMESTAMP_TYPES:
        settings.inworld.timestamp_type = "TIMESTAMP_TYPE_UNSPECIFIED"
    if settings.inworld.apply_text_normalization not in INWORLD_TEXT_NORMALIZATION_MODES:
        settings.inworld.apply_text_normalization = "APPLY_TEXT_NORMALIZATION_UNSPECIFIED"
    settings.voice = _voice_for_backend(settings.tts_backend, settings.voice)
    return settings


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _ensure_data_dir() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _normalize_user_slot(slot_name: str) -> str:
    slot = str(slot_name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if slot in USER_PRESET_SLOTS:
        return slot
    raise ValueError(f"unknown user preset slot: {slot_name}")


def _default_preset_label(slot_name: str) -> str:
    try:
        slot_index = USER_PRESET_SLOTS.index(slot_name) + 1
    except ValueError:
        slot_index = 0
    return f"Custom {slot_index}" if slot_index else slot_name


def _serialize_user_presets(user_presets: dict[str, SavedPreset]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for slot_name in USER_PRESET_SLOTS:
        preset = user_presets.get(slot_name)
        if preset is None:
            continue
        payload[slot_name] = preset.model_dump(mode="json")
    return payload


def _save_settings(settings: RuntimeSettings, user_presets: dict[str, SavedPreset] | None = None) -> None:
    _ensure_data_dir()
    payload = {
        "settings": settings.model_dump(mode="json"),
        "user_presets": _serialize_user_presets(user_presets or _user_presets),
    }
    SETTINGS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_settings() -> tuple[RuntimeSettings, dict[str, SavedPreset]]:
    _ensure_data_dir()
    if not SETTINGS_PATH.exists() and DEFAULT_SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text(DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    if SETTINGS_PATH.exists():
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if "settings" in payload:
                settings = RuntimeSettings.model_validate(payload["settings"])
                loaded_presets: dict[str, SavedPreset] = {}
                for slot_name, preset_payload in (payload.get("user_presets") or {}).items():
                    try:
                        normalized_slot = _normalize_user_slot(slot_name)
                        loaded_presets[normalized_slot] = SavedPreset.model_validate(preset_payload)
                    except Exception as exc:
                        logger.warning("Skipping invalid preset slot %s: %s", slot_name, exc)
                return _coerce_runtime_settings(settings), loaded_presets
            return _coerce_runtime_settings(RuntimeSettings.model_validate(payload)), {}
        except Exception as exc:
            logger.warning("Failed to load runtime settings from %s: %s", SETTINGS_PATH, exc)
    settings = _default_settings()
    _save_settings(settings, {})
    return settings, {}


def _current_settings_copy() -> RuntimeSettings:
    with _settings_lock:
        if _runtime_settings is None:
            raise RuntimeError("runtime settings not initialized")
        return _runtime_settings.model_copy(deep=True)


def _current_user_presets_copy() -> dict[str, SavedPreset]:
    with _settings_lock:
        return {slot: preset.model_copy(deep=True) for slot, preset in _user_presets.items()}


def _set_runtime_state(
    settings: RuntimeSettings,
    user_presets: dict[str, SavedPreset] | None = None,
) -> tuple[RuntimeSettings, dict[str, SavedPreset]]:
    global _runtime_settings, _user_presets
    settings = _coerce_runtime_settings(settings)
    with _settings_lock:
        _runtime_settings = settings
        if user_presets is not None:
            _user_presets = {slot: preset.model_copy(deep=True) for slot, preset in user_presets.items()}
        _save_settings(_runtime_settings, _user_presets)
        return _runtime_settings.model_copy(deep=True), _current_user_presets_copy()


def _set_runtime_settings(settings: RuntimeSettings) -> RuntimeSettings:
    updated_settings, _ = _set_runtime_state(settings)
    return updated_settings


def _update_runtime_settings(patch: dict[str, Any]) -> RuntimeSettings:
    current = _current_settings_copy()
    if "tts_backend" in patch:
        patch["tts_backend"] = _normalize_tts_backend(str(patch["tts_backend"]))
    if "effects_preset" in patch:
        current = _apply_preset(str(patch["effects_preset"]), current)
        patch = {key: value for key, value in patch.items() if key != "effects_preset"}
    merged = _deep_merge(current.model_dump(mode="json"), patch)
    updated = RuntimeSettings.model_validate(merged)
    return _set_runtime_settings(_coerce_runtime_settings(updated))


def _save_user_preset(slot_name: str, label: str, settings: RuntimeSettings) -> dict[str, SavedPreset]:
    normalized_slot = _normalize_user_slot(slot_name)
    clean_label = str(label or "").strip() or _default_preset_label(normalized_slot)
    user_presets = _current_user_presets_copy()
    user_presets[normalized_slot] = SavedPreset(label=clean_label, settings=settings.model_copy(deep=True))
    _set_runtime_state(_current_settings_copy(), user_presets)
    return _current_user_presets_copy()


def _load_user_preset(slot_name: str) -> RuntimeSettings:
    normalized_slot = _normalize_user_slot(slot_name)
    user_presets = _current_user_presets_copy()
    preset = user_presets.get(normalized_slot)
    if preset is None:
        raise KeyError(normalized_slot)
    loaded_settings = preset.settings.model_copy(deep=True)
    return _set_runtime_settings(loaded_settings)


def _load_graillon_plugin():
    if not GRAILLON_ENABLED:
        return None, "disabled"

    candidates = sorted(PLUGIN_DIR.rglob("*.vst3"))
    if not candidates:
        return None, "missing"

    plugin_path = str(candidates[0])
    try:
        plugin = load_plugin(plugin_path)
        logger.info("Loaded Graillon plugin from %s", plugin_path)
        _log_plugin_parameters(plugin)
        return plugin, "loaded"
    except Exception as exc:
        logger.warning("Graillon plugin unavailable, using fallback chain only: %s", exc)
        return None, f"error:{type(exc).__name__}"


def _apply_graillon_settings(plugin, settings: GraillonSettings) -> None:
    _fuzzy_set_parameter(plugin, ("correction",), settings.correction)
    _fuzzy_set_parameter(plugin, ("amount",), settings.amount)
    _set_named_parameter(plugin, "snap_max_st", settings.snap_max_st)
    _set_named_parameter(plugin, "snap_min_st", settings.snap_min_st)
    _fuzzy_set_parameter(plugin, ("smooth",), settings.smooth)
    _fuzzy_set_parameter(plugin, ("inertia",), settings.inertia)
    _set_named_parameter(plugin, "dry_mix_db", settings.dry_mix_db)
    _set_named_parameter(plugin, "wet_mix_db", settings.wet_mix_db)
    _set_named_parameter(plugin, "ptm_enabled", 1.0 if settings.ptm_enabled else 0.0)
    _set_named_parameter(plugin, "formant", settings.formant)
    _set_named_parameter(plugin, "chorus", settings.chorus)
    _set_named_parameter(plugin, "compressor", settings.compressor)
    _configure_scale(plugin, settings.key, settings.scale)


def _build_effect_chain(settings: RuntimeSettings) -> Pedalboard:
    chain = Pedalboard()
    if _graillon_plugin is not None and settings.graillon.enabled:
        _apply_graillon_settings(_graillon_plugin, settings.graillon)
        chain.append(_graillon_plugin)
    if settings.highpass.enabled:
        chain.append(HighpassFilter(cutoff_frequency_hz=settings.highpass.cutoff_frequency_hz))
    if settings.lowpass.enabled:
        chain.append(LowpassFilter(cutoff_frequency_hz=settings.lowpass.cutoff_frequency_hz))
    if settings.compressor.enabled:
        chain.append(
            Compressor(
                threshold_db=settings.compressor.threshold_db,
                ratio=settings.compressor.ratio,
                attack_ms=settings.compressor.attack_ms,
                release_ms=settings.compressor.release_ms,
            )
        )
        if abs(settings.compressor.makeup_gain_db) >= 0.01:
            chain.append(Gain(gain_db=settings.compressor.makeup_gain_db))
    if settings.chorus.enabled:
        chain.append(
            Chorus(
                rate_hz=settings.chorus.rate_hz,
                depth=settings.chorus.depth,
                centre_delay_ms=settings.chorus.centre_delay_ms,
                feedback=settings.chorus.feedback,
                mix=settings.chorus.mix,
            )
        )
    if settings.phaser.enabled:
        chain.append(
            Phaser(
                rate_hz=settings.phaser.rate_hz,
                depth=settings.phaser.depth,
                centre_frequency_hz=settings.phaser.centre_frequency_hz,
                feedback=settings.phaser.feedback,
                mix=settings.phaser.mix,
            )
        )
    if settings.delay.enabled:
        chain.append(
            Delay(
                delay_seconds=settings.delay.delay_seconds,
                feedback=settings.delay.feedback,
                mix=settings.delay.mix,
            )
        )
    if settings.reverb.enabled:
        chain.append(
            Reverb(
                room_size=settings.reverb.room_size,
                damping=settings.reverb.damping,
                wet_level=settings.reverb.wet_level,
                dry_level=settings.reverb.dry_level,
                width=settings.reverb.width,
            )
        )
    if abs(settings.pitch_shift_semitones) >= 0.01:
        chain.append(PitchShift(semitones=settings.pitch_shift_semitones))
    return chain


def _load_kokoro() -> Kokoro:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"missing Kokoro model file: {MODEL_PATH}")
    if not VOICES_PATH.exists():
        raise FileNotFoundError(f"missing Kokoro voices file: {VOICES_PATH}")

    logger.info("Loading Kokoro model from %s", MODEL_PATH)
    return Kokoro(str(MODEL_PATH), str(VOICES_PATH))


def _pocket_model_cache_key(settings: PocketTTSSettings) -> tuple[Any, ...]:
    return (
        settings.language,
        float(settings.temperature),
        int(settings.lsd_decode_steps),
        None if settings.noise_clamp is None else float(settings.noise_clamp),
        float(settings.eos_threshold),
        bool(settings.quantize),
    )


def _load_pocket_model(settings: PocketTTSSettings | None = None):
    global _pocket_model, _pocket_model_key, _pocket_status, _pocket_voice_states
    settings = settings or _current_settings_copy().pockettts
    model_key = _pocket_model_cache_key(settings)
    with _pocket_lock:
        if _pocket_model is not None and _pocket_model_key == model_key:
            return _pocket_model
        _pocket_status = "loading"
        try:
            from pocket_tts import TTSModel

            logger.info("Loading PocketTTS model settings=%s", model_key)
            _pocket_model = TTSModel.load_model(
                language=settings.language,
                temp=settings.temperature,
                lsd_decode_steps=settings.lsd_decode_steps,
                noise_clamp=settings.noise_clamp,
                eos_threshold=settings.eos_threshold,
                quantize=settings.quantize,
            )
            _pocket_model_key = model_key
            _pocket_voice_states = {}
            _pocket_status = "loaded"
            return _pocket_model
        except Exception:
            _pocket_status = "error"
            raise


def _get_pocket_voice_state(voice: str, settings: PocketTTSSettings | None = None):
    model = _load_pocket_model(settings)
    resolved_voice = _voice_for_backend("pockettts_fx", voice)
    with _pocket_lock:
        cached = _pocket_voice_states.get(resolved_voice)
        if cached is not None:
            return resolved_voice, cached
        logger.info("Loading PocketTTS voice state: %s", resolved_voice)
        state = model.get_state_for_audio_prompt(resolved_voice)
        _pocket_voice_states[resolved_voice] = state
        return resolved_voice, state


def _synthesize_pocket(text: str, voice: str, settings: PocketTTSSettings) -> tuple[np.ndarray, int, str]:
    model = _load_pocket_model(settings)
    resolved_voice, voice_state = _get_pocket_voice_state(voice, settings)
    audio_tensor = model.generate_audio(
        voice_state,
        text,
        max_tokens=settings.max_tokens,
        frames_after_eos=settings.frames_after_eos,
    )
    if hasattr(audio_tensor, "detach"):
        audio_tensor = audio_tensor.detach()
    if hasattr(audio_tensor, "cpu"):
        audio_tensor = audio_tensor.cpu()
    if hasattr(audio_tensor, "numpy"):
        audio = np.asarray(audio_tensor.numpy(), dtype=np.float32)
    else:
        audio = np.asarray(audio_tensor, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    sample_rate = int(getattr(model, "sample_rate", 24000) or 24000)
    return audio, sample_rate, resolved_voice


def _pocket_warm_voices() -> list[str]:
    raw = POCKETTTS_WARM_VOICES_RAW.strip().lower()
    if raw == "all":
        return list(POCKETTTS_VOICES)
    voices = [voice.strip().lower() for voice in raw.split(",") if voice.strip()]
    return [_voice_for_backend("pockettts_fx", voice) for voice in voices] or [DEFAULT_POCKETTTS_VOICE]


def _warm_pockettts() -> None:
    started = time.perf_counter()
    try:
        settings = _current_settings_copy().pockettts
        _load_pocket_model(settings)
        for voice in dict.fromkeys(_pocket_warm_voices()):
            _get_pocket_voice_state(voice, settings)
        logger.info(
            "PocketTTS warm complete voices=%s seconds=%.3f",
            sorted(_pocket_voice_states.keys()),
            time.perf_counter() - started,
        )
    except Exception:
        logger.exception("PocketTTS warm failed")


def _start_pocket_warmer() -> None:
    if not POCKETTTS_WARM_ON_START:
        return
    thread = threading.Thread(target=_warm_pockettts, name="pockettts-warmer", daemon=True)
    thread.start()


def _warm_pockettts_after_settings_change(settings: RuntimeSettings) -> None:
    if settings.tts_backend == "pockettts_fx":
        _start_pocket_warmer()


def _refresh_inworld_voices() -> None:
    global _inworld_voices, _inworld_voice_details, _inworld_status
    if not INWORLD_API_KEY:
        _inworld_voices = list(dict.fromkeys(INWORLD_FALLBACK_VOICES))
        _inworld_voice_details = [
            {"voiceId": voice, "displayName": voice, "source": "fallback"} for voice in _inworld_voices
        ]
        _inworld_status = "not_configured"
        return

    url = f"{INWORLD_VOICES_URL}?languages=EN_US"
    request = urllib_request.Request(
        url,
        headers={"Authorization": f"Basic {INWORLD_API_KEY}"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=min(INWORLD_TIMEOUT_SECONDS, 10.0)) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.warning("Inworld voice list unavailable: %s", exc)
        _inworld_status = f"voice_list_error:{type(exc).__name__}"
        return

    details = []
    voice_ids = []
    for item in data.get("voices") or []:
        voice_id = str(item.get("voiceId") or item.get("name") or "").strip()
        if not voice_id:
            continue
        details.append(
            {
                "voiceId": voice_id,
                "displayName": str(item.get("displayName") or voice_id),
                "description": str(item.get("description") or ""),
                "source": str(item.get("source") or ""),
                "tags": item.get("tags") or [],
            }
        )
        voice_ids.append(voice_id)

    if voice_ids:
        _inworld_voices = list(dict.fromkeys([INWORLD_DEFAULT_VOICE, *voice_ids]))
        _inworld_voice_details = details
        _inworld_status = "loaded"
    else:
        _inworld_status = "empty_voice_list"


def _decode_audio_payload(payload: bytes) -> tuple[np.ndarray, int]:
    try:
        audio, sample_rate = sf.read(io.BytesIO(payload), dtype="float32")
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as src:
            src_path = src.name
            src.write(payload)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as dst:
            dst_path = dst.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path, "-f", "wav", dst_path],
                check=True,
                timeout=60,
            )
            audio, sample_rate = sf.read(dst_path, dtype="float32")
        finally:
            for path in (src_path, dst_path):
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    pass
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    return audio, int(sample_rate)


def _synthesize_inworld(
    text: str,
    voice: str,
    settings: InworldSettings,
) -> tuple[np.ndarray, int, str, dict[str, Any]]:
    if not INWORLD_API_KEY:
        raise RuntimeError("INWORLD_API_KEY is not configured for Hermes Voice TTS")

    resolved_voice = _voice_for_backend("inworld_fx", voice)
    model = settings.model if settings.model in INWORLD_MODELS else INWORLD_DEFAULT_MODEL
    audio_encoding = settings.audio_encoding if settings.audio_encoding in INWORLD_AUDIO_ENCODINGS else "LINEAR16"
    timestamp_type = (
        settings.timestamp_type
        if settings.timestamp_type in INWORLD_TIMESTAMP_TYPES
        else "TIMESTAMP_TYPE_UNSPECIFIED"
    )
    text_normalization = (
        settings.apply_text_normalization
        if settings.apply_text_normalization in INWORLD_TEXT_NORMALIZATION_MODES
        else "APPLY_TEXT_NORMALIZATION_UNSPECIFIED"
    )
    body = {
        "text": text[:INWORLD_MAX_INPUT_CHARS],
        "voiceId": resolved_voice,
        "modelId": model,
        "audioConfig": {
            "audioEncoding": audio_encoding,
            "sampleRateHertz": int(settings.sample_rate_hertz),
            "speakingRate": float(settings.speaking_rate),
        },
        "temperature": float(settings.temperature),
        "timestampType": timestamp_type,
        "applyTextNormalization": text_normalization,
    }
    request = urllib_request.Request(
        INWORLD_SYNTH_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Basic {INWORLD_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=INWORLD_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Inworld endpoint {exc.code}: {detail[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Inworld endpoint unavailable: {type(exc).__name__}: {exc}") from exc

    audio_b64 = str(data.get("audioContent") or "")
    if not audio_b64:
        raise RuntimeError("Inworld response missing audioContent")
    audio, sample_rate = _decode_audio_payload(base64.b64decode(audio_b64))
    return audio, sample_rate, resolved_voice, data.get("usage") or {}


def _resolve_kokoro_voice_style(voice: str) -> str | np.ndarray:
    resolved_voice = _resolve_voice(voice)
    blend = CUSTOM_KOKORO_VOICE_BLENDS.get(resolved_voice)
    if blend is None:
        return resolved_voice

    if _kokoro is None:
        raise RuntimeError("kokoro model is not loaded")

    style = None
    total_weight = 0.0
    for source_voice, weight in blend.items():
        source_style = np.asarray(_kokoro.get_voice_style(source_voice), dtype=np.float32)
        if style is None:
            style = np.zeros_like(source_style, dtype=np.float32)
        style += source_style * float(weight)
        total_weight += float(weight)

    if style is None or total_weight <= 0:
        raise RuntimeError(f"invalid custom Kokoro voice blend: {resolved_voice}")
    return style / total_weight


def _synthesize(text: str, voice: str, speed: float) -> tuple[np.ndarray, int]:
    if _kokoro is None:
        raise RuntimeError("kokoro model is not loaded")

    kokoro_voice = _resolve_kokoro_voice_style(voice)
    samples, sample_rate = _kokoro.create(
        text,
        voice=kokoro_voice,
        speed=speed,
        lang=_resolve_lang(voice),
    )
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    return audio, int(sample_rate)


def _normalize_audio(audio: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.99:
        audio = audio / peak * 0.99
    return audio.astype(np.float32)


def _encode_with_ffmpeg(audio: np.ndarray, sample_rate: int, response_format: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as src:
        src_path = src.name
    with tempfile.NamedTemporaryFile(
        suffix=".ogg" if response_format in {"ogg", "opus"} else f".{response_format}",
        delete=False,
    ) as dst:
        dst_path = dst.name

    try:
        sf.write(src_path, audio, sample_rate, subtype="PCM_16")
        command = ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path]
        if response_format == "mp3":
            command += ["-codec:a", "libmp3lame", "-b:a", "128k"]
        elif response_format in {"ogg", "opus"}:
            command += ["-codec:a", "libopus", "-b:a", "64k"]
        elif response_format == "flac":
            command += ["-codec:a", "flac"]
        else:
            raise ValueError(f"unsupported response format: {response_format}")
        command.append(dst_path)
        subprocess.run(command, check=True, timeout=60)
        return Path(dst_path).read_bytes()
    finally:
        for path in (src_path, dst_path):
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass


def _encode_audio(audio: np.ndarray, sample_rate: int, response_format: str) -> bytes:
    response_format = response_format.lower()
    if response_format == "pcm":
        pcm = np.clip(audio, -1.0, 1.0)
        return (pcm * 32767.0).astype("<i2").tobytes()

    if response_format == "wav":
        buffer = io.BytesIO()
        sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue()

    return _encode_with_ffmpeg(audio, sample_rate, response_format)


def _settings_response(settings: RuntimeSettings | None = None) -> dict[str, Any]:
    current = settings or _current_settings_copy()
    user_presets = _current_user_presets_copy()
    voices_by_backend = _voices_by_backend()
    return {
        "settings": current.model_dump(mode="json"),
        "user_presets": {
            slot_name: {
                "label": preset.label,
                "effects_preset": preset.settings.effects_preset,
                "graillon_key": preset.settings.graillon.key,
                "graillon_scale": preset.settings.graillon.scale,
            }
            for slot_name, preset in user_presets.items()
        },
        "options": {
            "backends": AVAILABLE_BACKENDS,
            "voices": sorted({voice for voices in voices_by_backend.values() for voice in voices}),
            "voices_by_backend": voices_by_backend,
            "presets": AVAILABLE_PRESETS,
            "user_preset_slots": USER_PRESET_SLOTS,
            "keys": AVAILABLE_KEYS,
            "scales": AVAILABLE_SCALES,
            "response_formats": ["mp3", "wav", "ogg", "flac"],
            "inworld_models": INWORLD_MODELS,
            "inworld_audio_encodings": INWORLD_AUDIO_ENCODINGS,
            "inworld_timestamp_types": INWORLD_TIMESTAMP_TYPES,
            "inworld_text_normalization_modes": INWORLD_TEXT_NORMALIZATION_MODES,
            "inworld_voice_details": _inworld_voice_details,
        },
        "service": {
            "graillon": _graillon_status,
            "ui_url": "/ui",
            "settings_path": str(SETTINGS_PATH),
            "inworld_status": "disabled" if KOKORO_ONLY_MODE else _inworld_status,
            "inworld_configured": False if KOKORO_ONLY_MODE else bool(INWORLD_API_KEY),
            "inworld_voice_count": 0 if KOKORO_ONLY_MODE else len(_inworld_voices),
        },
    }


@app.on_event("startup")
def startup() -> None:
    global _graillon_plugin, _graillon_status, _kokoro, _runtime_settings, _user_presets
    _graillon_plugin, _graillon_status = _load_graillon_plugin()
    _kokoro = _load_kokoro()
    if not KOKORO_ONLY_MODE:
        _refresh_inworld_voices()
    _runtime_settings, _user_presets = _load_settings()
    _start_pocket_warmer()


@app.get("/")
def root() -> JSONResponse:
    payload = _settings_response()
    payload["service"].update(
        {
            "service": "kokoro-glados-voice",
            "status": "ok",
            "model_path": str(MODEL_PATH),
            "voices_path": str(VOICES_PATH),
            "default_voice": DEFAULT_VOICE,
            "default_lang": DEFAULT_LANG,
            "kokoro_only": KOKORO_ONLY_MODE,
            "inworld_default_voice": INWORLD_DEFAULT_VOICE,
            "inworld_default_model": INWORLD_DEFAULT_MODEL,
        }
    )
    return JSONResponse(payload)


@app.get("/ui", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> JSONResponse:
    settings = _current_settings_copy()
    return JSONResponse(
        {
            "status": "ok" if _kokoro is not None else "starting",
            "model_loaded": _kokoro is not None,
            "tts_backend": settings.tts_backend,
            "graillon": _graillon_status,
            "graillon_enabled": settings.graillon.enabled,
            "effects_preset": settings.effects_preset,
            "inworld_status": "disabled" if KOKORO_ONLY_MODE else _inworld_status,
            "inworld_configured": False if KOKORO_ONLY_MODE else bool(INWORLD_API_KEY),
            "inworld_voice_count": 0 if KOKORO_ONLY_MODE else len(_inworld_voices),
        }
    )


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    return JSONResponse(_settings_response())


@app.post("/api/settings")
def update_settings(payload: dict[str, Any]) -> JSONResponse:
    try:
        settings = _update_runtime_settings(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid settings payload: {exc}") from exc
    return JSONResponse(_settings_response(settings))


@app.post("/api/presets/{preset_name}")
def apply_preset(preset_name: str) -> JSONResponse:
    settings = _apply_preset(preset_name, _current_settings_copy())
    settings = _set_runtime_settings(settings)
    return JSONResponse(_settings_response(settings))


@app.post("/api/inworld/voices/refresh")
def refresh_inworld_voices() -> JSONResponse:
    _refresh_inworld_voices()
    return JSONResponse(_settings_response())


@app.post("/api/user-presets/{slot_name}/save")
def save_user_preset(slot_name: str, payload: dict[str, Any] | None = None) -> JSONResponse:
    payload = payload or {}
    label = str(payload.get("label") or "").strip()
    candidate_settings = payload.get("settings")
    settings = RuntimeSettings.model_validate(candidate_settings) if candidate_settings is not None else _current_settings_copy()
    try:
        _save_user_preset(slot_name, label, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(_settings_response())


@app.post("/api/user-presets/{slot_name}/load")
def load_user_preset(slot_name: str) -> JSONResponse:
    try:
        settings = _load_user_preset(slot_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"preset slot is empty: {slot_name}") from exc
    return JSONResponse(_settings_response(settings))


@app.get("/v1/audio/voices")
def list_voices() -> JSONResponse:
    voices_by_backend = _voices_by_backend()
    voices = [
        {"voice": voice, "backend": backend}
        for backend, backend_voices in voices_by_backend.items()
        for voice in backend_voices
    ]
    return JSONResponse({"voices": voices, "voices_by_backend": voices_by_backend})


@app.post("/v1/audio/speech")
def create_speech(request: SpeechRequest):
    text = (request.input or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="input is required")
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]

    settings = _current_settings_copy()
    response_format = request.response_format.lower().strip() or "mp3"

    try:
        synth_started = time.perf_counter()
        if settings.tts_backend == "inworld_fx":
            audio, sample_rate, voice, usage = _synthesize_inworld(text, request.voice or settings.voice, settings.inworld)
            backend_headers = {
                "x-tts-backend": "inworld_fx",
                "x-inworld-voice": voice,
                "x-inworld-model": settings.inworld.model,
                "x-inworld-audio-encoding": settings.inworld.audio_encoding,
                "x-inworld-sample-rate": str(settings.inworld.sample_rate_hertz),
                "x-inworld-processed-chars": str(usage.get("processedCharactersCount", "")),
            }
        else:
            voice = _voice_for_backend("kokoro_glados", request.voice or settings.voice)
            audio, sample_rate = _synthesize(text, voice, request.speed)
            backend_headers = {
                "x-tts-backend": "kokoro_glados",
                "x-glados-voice": voice,
                "x-glados-lang": _resolve_lang(voice),
            }
        synth_seconds = time.perf_counter() - synth_started
        effects_started = time.perf_counter()
        with _audio_lock:
            processed = _build_effect_chain(settings)(audio, sample_rate)
        effects_seconds = time.perf_counter() - effects_started
        payload = _encode_audio(_normalize_audio(processed), sample_rate, response_format)
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f"audio encoding failed: {exc}") from exc
    except Exception as exc:
        logger.exception("Speech synthesis failed")
        raise HTTPException(status_code=500, detail=f"speech synthesis failed: {type(exc).__name__}") from exc

    return StreamingResponse(
        _iter_chunks(payload),
        media_type=_media_type_for(response_format),
        headers={
            **backend_headers,
            "x-glados-graillon": _graillon_status,
            "x-glados-preset": settings.effects_preset,
            "x-glados-key": settings.graillon.key,
            "x-glados-scale": settings.graillon.scale,
            "x-glados-synth-seconds": f"{synth_seconds:.3f}",
            "x-glados-effects-seconds": f"{effects_seconds:.3f}",
        },
    )

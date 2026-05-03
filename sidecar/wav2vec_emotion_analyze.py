#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from shutil import which
from typing import Any

import numpy as np


def _add_pythonpath(path: str) -> None:
    if path and Path(path).exists() and path not in sys.path:
        sys.path.insert(0, path)


def _ffmpeg() -> str:
    return which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def _to_16k_wav(input_path: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="wav2vec_emotion_", suffix=".wav", delete=False)
    handle.close()
    subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            handle.name,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return handle.name


def _read_wav_float(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data


def _affect_from_label(label: str) -> dict[str, str]:
    normalized = label.lower().strip()
    normalized = normalized.replace("_", " ").replace("-", " ")
    if "ang" in normalized:
        return {"affect": "angry", "arousal": "high", "urgency": "medium"}
    if "sad" in normalized:
        return {"affect": "sad", "arousal": "low", "urgency": "low"}
    if "fear" in normalized:
        return {"affect": "fearful", "arousal": "high", "urgency": "medium"}
    if "surp" in normalized:
        return {"affect": "surprised", "arousal": "high", "urgency": "medium"}
    if "hap" in normalized or "joy" in normalized:
        return {"affect": "happy", "arousal": "medium", "urgency": "low"}
    if "disgust" in normalized:
        return {"affect": "annoyed", "arousal": "medium", "urgency": "medium"}
    return {"affect": "neutral", "arousal": "low", "urgency": "low"}


class Wav2VecEmotionAnalyzer:
    def __init__(self, args: argparse.Namespace):
        _add_pythonpath(args.pythonpath)
        os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
        self.cache_dir = Path(args.cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(self.cache_dir / "huggingface"))
        os.environ.setdefault("TRANSFORMERS_CACHE", str(self.cache_dir / "huggingface"))

        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from transformers import AutoModelForAudioClassification, AutoProcessor

            self.torch = torch
            self.processor = AutoProcessor.from_pretrained(args.model)
            self.model = AutoModelForAudioClassification.from_pretrained(args.model)
            self.model.eval()
        self.model_name = args.model

    def analyze_audio(self, audio_path: str) -> dict[str, Any]:
        started = time.monotonic()
        wav16 = _to_16k_wav(audio_path)
        try:
            audio = _read_wav_float(wav16)
            inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt", padding=True)
            with self.torch.no_grad():
                logits = self.model(**inputs).logits[0]
                probs = self.torch.softmax(logits, dim=-1).detach().cpu().numpy()
        finally:
            with contextlib.suppress(OSError):
                Path(wav16).unlink()

        id2label = getattr(self.model.config, "id2label", {}) or {}
        scores = []
        for index, score in enumerate(probs.tolist()):
            label = str(id2label.get(index, index))
            scores.append({"label": label, "score": float(score)})
        scores.sort(key=lambda item: item["score"], reverse=True)
        top = scores[0] if scores else {"label": "unknown", "score": 0.0}
        mapped = _affect_from_label(str(top["label"]))
        return {
            "success": True,
            "source": "wav2vec_emotion",
            "model": self.model_name,
            "affect": mapped["affect"],
            "arousal": mapped["arousal"],
            "pace": "unknown",
            "urgency": mapped["urgency"],
            "tone_summary": f"wav2vec classified voice as {top['label']}",
            "assistant_adjustment": "adapt tone gently to the detected speaker affect",
            "confidence": round(float(top["score"]), 4),
            "scores": scores[:7],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    analyzer = Wav2VecEmotionAnalyzer(args)
    return analyzer.analyze_audio(args.audio)


def worker(args: argparse.Namespace) -> int:
    analyzer = Wav2VecEmotionAnalyzer(args)
    print(json.dumps({"type": "ready", "success": True, "model": args.model}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            result = analyzer.analyze_audio(str(request["audio"]))
        except Exception as exc:
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio")
    parser.add_argument("--model", default="Dpngtm/wav2vec2-emotion-recognition")
    parser.add_argument("--pythonpath", default="")
    parser.add_argument("--cache-dir", default=str(Path.home() / ".hermes/emotion2vec/cache"))
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return worker(args)
    if not args.audio:
        parser.error("--audio is required unless --worker is set")
    try:
        result = analyze(args)
    except Exception as exc:
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())

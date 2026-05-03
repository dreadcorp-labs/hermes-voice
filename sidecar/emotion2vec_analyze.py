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
from pathlib import Path
from shutil import which
from typing import Any


LABEL_MAP = {
    "angry": "angry",
    "disgusted": "disgusted",
    "fearful": "fearful",
    "happy": "happy",
    "neutral": "neutral",
    "other": "other",
    "sad": "sad",
    "surprised": "surprised",
    "unknown": "unknown",
}


def _add_pythonpath(path: str) -> None:
    if path and Path(path).exists() and path not in sys.path:
        sys.path.insert(0, path)


def _ffmpeg() -> str:
    return which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def _to_16k_wav(input_path: str) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="emotion2vec_", suffix=".wav", delete=False)
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


def _score_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return []

    labels = raw.get("labels") or raw.get("label") or raw.get("emo_labels")
    scores = raw.get("scores") or raw.get("score") or raw.get("emo_scores")
    if isinstance(labels, list) and isinstance(scores, list):
        items = []
        for label, score in zip(labels, scores):
            try:
                value = float(score)
            except (TypeError, ValueError):
                continue
            items.append({"label": str(label), "score": value})
        return sorted(items, key=lambda item: item["score"], reverse=True)

    for key in ("value", "text", "emotion"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return [{"label": value, "score": 1.0}]
    return []


def _affect_from_label(label: str) -> dict[str, str]:
    normalized = label.lower().strip()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1].strip()
    for key in LABEL_MAP:
        if key in normalized:
            normalized = key
            break
    affect = LABEL_MAP.get(normalized, normalized or "unknown")
    if affect == "angry":
        return {"affect": "angry", "arousal": "high", "urgency": "medium"}
    if affect == "sad":
        return {"affect": "sad", "arousal": "low", "urgency": "low"}
    if affect in {"fearful", "surprised"}:
        return {"affect": affect, "arousal": "high", "urgency": "medium"}
    if affect == "happy":
        return {"affect": "happy", "arousal": "medium", "urgency": "low"}
    if affect == "disgusted":
        return {"affect": "annoyed", "arousal": "medium", "urgency": "medium"}
    return {"affect": "neutral", "arousal": "low", "urgency": "low"}


class Emotion2VecAnalyzer:
    def __init__(self, args: argparse.Namespace):
        _add_pythonpath(args.pythonpath)
        os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
        self.cache_dir = Path(args.cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MODELSCOPE_CACHE", str(self.cache_dir / "modelscope"))
        os.environ.setdefault("FUNASR_HOME", str(self.cache_dir / "funasr"))
        os.environ.setdefault("HF_HOME", str(self.cache_dir / "huggingface"))
        with contextlib.redirect_stdout(sys.stderr):
            from funasr import AutoModel

            self.model = AutoModel(model=args.model, disable_update=True)
        self.model_name = args.model

    def analyze_audio(self, audio_path: str) -> dict[str, Any]:
        started = time.monotonic()
        wav16 = _to_16k_wav(audio_path)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                raw = self.model.generate(
                    wav16,
                    output_dir=str(self.cache_dir / "outputs"),
                    granularity="utterance",
                    extract_embedding=False,
                )
        finally:
            with contextlib.suppress(OSError):
                Path(wav16).unlink()

        scores = _score_items(raw)
        if not scores:
            return {
                "success": False,
                "error": f"Could not parse emotion2vec output: {raw!r}"[:500],
                "raw": raw,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        top = scores[0]
        mapped = _affect_from_label(str(top["label"]))
        return {
            "success": True,
            "source": "emotion2vec",
            "model": self.model_name,
            "affect": mapped["affect"],
            "arousal": mapped["arousal"],
            "pace": "unknown",
            "urgency": mapped["urgency"],
            "tone_summary": f"emotion2vec classified voice as {top['label']}",
            "assistant_adjustment": "adapt tone gently to the detected speaker affect",
            "confidence": round(float(top["score"]), 4),
            "scores": scores[:5],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    analyzer = Emotion2VecAnalyzer(args)
    return analyzer.analyze_audio(args.audio)


def worker(args: argparse.Namespace) -> int:
    analyzer = Emotion2VecAnalyzer(args)
    warmup_audio = analyzer.cache_dir / "modelscope/models/iic" / args.model.rsplit("/", 1)[-1] / "example/test.wav"
    if warmup_audio.exists():
        with contextlib.suppress(Exception):
            analyzer.analyze_audio(str(warmup_audio))
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


def _legacy_analyze(args: argparse.Namespace) -> dict[str, Any]:
    _add_pythonpath(args.pythonpath)
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MODELSCOPE_CACHE", str(cache_dir / "modelscope"))
    os.environ.setdefault("FUNASR_HOME", str(cache_dir / "funasr"))
    os.environ.setdefault("HF_HOME", str(cache_dir / "huggingface"))

    from funasr import AutoModel

    started = time.monotonic()
    wav16 = _to_16k_wav(args.audio)
    try:
        model = AutoModel(model=args.model)
        raw = model.generate(
            wav16,
            output_dir=str(cache_dir / "outputs"),
            granularity="utterance",
            extract_embedding=False,
        )
    finally:
        with contextlib.suppress(OSError):
            Path(wav16).unlink()

    scores = _score_items(raw)
    if not scores:
        return {
            "success": False,
            "error": f"Could not parse emotion2vec output: {raw!r}"[:500],
            "raw": raw,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    top = scores[0]
    mapped = _affect_from_label(str(top["label"]))
    return {
        "success": True,
        "source": "emotion2vec",
        "model": args.model,
        "affect": mapped["affect"],
        "arousal": mapped["arousal"],
        "pace": "unknown",
        "urgency": mapped["urgency"],
        "tone_summary": f"emotion2vec classified voice as {top['label']}",
        "assistant_adjustment": "adapt tone gently to the detected speaker affect",
        "confidence": round(float(top["score"]), 4),
        "scores": scores[:5],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio")
    parser.add_argument("--model", default="iic/emotion2vec_plus_base")
    parser.add_argument("--pythonpath", default=str(Path.home() / ".hermes/emotion2vec/pythonpath"))
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

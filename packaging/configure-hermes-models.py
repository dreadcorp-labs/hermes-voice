#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path


FALLBACK_KIMI_MODELS = ("kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2-thinking-turbo")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip():
            values[key.strip()] = value
    return values


def fetch_kimi_models(base_url: str, api_key: str) -> list[str]:
    if not api_key:
        return []
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    models: list[str] = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id and (model_id.startswith("kimi-") or model_id.startswith("moonshot-")):
            models.append(model_id)
    return models


def provider_block(base_url: str, models: list[str]) -> str:
    default_model = "kimi-k2.6" if "kimi-k2.6" in models else (models[0] if models else "kimi-k2.6")
    lines = [
        "  kimi-coding:",
        "    name: Kimi / Moonshot",
        f"    base_url: {base_url.rstrip('/')}",
        "    key_env: KIMI_API_KEY",
        f"    default_model: {default_model}",
        "    models:",
    ]
    for model in models:
        lines.append(f"      {model}: {{}}")
    return "\n".join(lines) + "\n"


def add_or_replace_provider(config_text: str, block: str) -> tuple[str, bool]:
    if re.search(r"(?m)^providers:\s*\{\}\s*$", config_text):
        return re.sub(r"(?m)^providers:\s*\{\}\s*$", "providers:\n" + block.rstrip(), config_text), True

    provider_match = re.search(r"(?ms)^providers:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", config_text)
    if provider_match:
        body = provider_match.group("body")
        if re.search(r"(?m)^  kimi-coding:\s*$", body):
            return config_text, False
        insert_at = provider_match.start("body")
        return config_text[:insert_at] + block + config_text[insert_at:], True

    if config_text and not config_text.endswith("\n"):
        config_text += "\n"
    return config_text + "\nproviders:\n" + block, True


def main() -> int:
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        print(f"Hermes config not found at {config_path}; skipped")
        return 0

    env_values = {**load_env(hermes_home / ".env"), **os.environ}
    base_url = (env_values.get("KIMI_BASE_URL") or "https://api.moonshot.ai/v1").strip().rstrip("/")
    api_key = (env_values.get("KIMI_API_KEY") or env_values.get("KIMI_CODING_API_KEY") or "").strip()
    try:
        models = fetch_kimi_models(base_url, api_key)
    except Exception as exc:
        print(f"Could not verify Kimi model list ({type(exc).__name__}); using packaged catalog")
        models = []
    if not models:
        models = list(FALLBACK_KIMI_MODELS)

    preferred_order = [*FALLBACK_KIMI_MODELS]
    ordered = [model for model in preferred_order if model in models]
    ordered.extend(model for model in models if model not in ordered)

    original = config_path.read_text()
    updated, changed = add_or_replace_provider(original, provider_block(base_url, ordered))
    if not changed:
        print("Hermes Kimi provider already configured")
        return 0

    backup = config_path.with_name(f"{config_path.name}.bak-hermes-voice-kimi-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(config_path, backup)
    config_path.write_text(updated)
    os.chmod(config_path, 0o600)
    print(f"Configured Hermes Kimi provider with {len(ordered)} model(s); backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

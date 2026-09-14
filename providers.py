#!/usr/bin/env python3
"""Remote OpenAI-compatible model providers (e.g. llmapi.paratera.com).

Config lives in ~/.config/kami/providers.json (0600):

    {
      "paratera": {
        "base_url": "https://llmapi.paratera.com",
        "api_key": "sk-...",
        "enabled": true,
        "models": {                       # registry key -> upstream id
          "glm-4-flash": "GLM-4-Flash", ...
        }
      }
    }

The "models" mapping IS the allowlist: any upstream model id not listed here
is refused client-side, regardless of what /v1/models advertises.

NOTE: clients are built with trust_env=False — the system HTTP proxy breaks
or massively slows this endpoint on this machine.
"""

import base64
import json
import logging
import os
import stat
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

from paths import CONFIG_DIR
PROVIDERS_FILE = CONFIG_DIR / "providers.json"
MEDIA_DIR = CONFIG_DIR / "media"

DEFAULT_TIMEOUT_S = 180


def load_providers() -> dict:
    try:
        return json.loads(PROVIDERS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.error("Cannot read %s: %s", PROVIDERS_FILE, e)
        return {}


def save_providers(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDERS_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    try:
        os.chmod(PROVIDERS_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def _provider(name: str) -> dict:
    cfg = load_providers().get(name)
    if not cfg or not cfg.get("enabled", True):
        raise RuntimeError(f"provider '{name}' missing or disabled")
    return cfg


def _check_allowed(cfg: dict, model_id: str) -> None:
    """Allowlist gate — only ids mapped in the provider config may run."""
    allowed = set(cfg.get("models", {}).values())
    if model_id not in allowed:
        raise ValueError(
            f"model '{model_id}' is not in the provider allowlist"
        )


def chat_completion(
    provider_name: str,
    model_id: str,
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.6,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> str | None:
    """One OpenAI-style chat completion. Returns content or None on failure."""
    cfg = _provider(provider_name)
    _check_allowed(cfg, model_id)
    try:
        r = httpx.Client(trust_env=False).post(
            f"{cfg['base_url'].rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        return content or None
    except Exception as e:
        logger.warning("%s/%s chat failed: %s", provider_name, model_id, e)
        return None


def generate_image(
    provider_name: str,
    model_id: str,
    prompt: str,
    size: str = "1024x1024",
    timeout: int = 300,
) -> Path | None:
    """Text-to-image via /v1/images/generations; saves to media dir."""
    cfg = _provider(provider_name)
    _check_allowed(cfg, model_id)
    try:
        r = httpx.Client(trust_env=False).post(
            f"{cfg['base_url'].rstrip('/')}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={"model": model_id, "prompt": prompt, "size": size},
            timeout=timeout,
        )
        r.raise_for_status()
        item = (r.json().get("data") or [{}])[0]
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        out = MEDIA_DIR / f"gen_{int(time.time() * 1000)}.png"
        if item.get("b64_json"):
            out.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            dl = httpx.Client(trust_env=False).get(item["url"], timeout=120)
            dl.raise_for_status()
            out.write_bytes(dl.content)
        else:
            logger.warning("image response had no url/b64_json")
            return None
        logger.info("Generated image: %s", out.name)
        return out
    except Exception as e:
        logger.warning("%s/%s image failed: %s", provider_name, model_id, e)
        return None

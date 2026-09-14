#!/usr/bin/env python3
"""Local llama.cpp (llama-server) client for fast replies.

The daemon keeps llama-server.exe running at 127.0.0.1:8899 with an
OpenAI-compatible /v1/chat/completions endpoint. ask_local() returns the
reply text, or None when the server is unavailable / errored so the caller
can fall back to Claude.
"""

import base64
import logging
import mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LOCAL_LLM_URL = "http://127.0.0.1:8899"
VL_LLM_URL = "http://127.0.0.1:8188"  # llama-server with Qwen3-VL + mmproj
LOCAL_TIMEOUT_S = 60
VL_TIMEOUT_S = 180  # vision encoding + generation is slower
LOCAL_MAX_TOKENS = 512
VL_MAX_TOKENS = 1024

DEFAULT_SYSTEM = (
    "你是一个简洁直接的助手。用用户的语言回答，直接给答案，不要长篇大论，"
    "不要使用 markdown 格式。"
)

_health_ok = False  # sticky cache of the last health check


def is_local_up(timeout: float = 1.5) -> bool:
    """Cheap health probe for llama-server."""
    global _health_ok
    try:
        r = httpx.get(f"{LOCAL_LLM_URL}/health", timeout=timeout)
        _health_ok = r.status_code == 200
    except Exception:
        _health_ok = False
    return _health_ok


def is_vl_up(timeout: float = 1.5) -> bool:
    """Cheap health probe for the vision server."""
    try:
        return httpx.get(f"{VL_LLM_URL}/health", timeout=timeout).status_code == 200
    except Exception:
        return False


def ask_local(
    prompt: str,
    system: str | None = None,
    max_tokens: int = LOCAL_MAX_TOKENS,
) -> str | None:
    """One-shot chat completion on the local model. None on any failure."""
    messages = [{"role": "system", "content": system or DEFAULT_SYSTEM},
                {"role": "user", "content": prompt}]
    try:
        r = httpx.post(
            f"{LOCAL_LLM_URL}/v1/chat/completions",
            json={
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.4,
            },
            timeout=LOCAL_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        text = text.strip()
        if not text:
            logger.warning("Local LLM returned empty content")
            return None
        return text
    except Exception as e:
        logger.warning("Local LLM call failed: %s", e)
        return None


def _image_data_url(path: Path) -> str | None:
    """Encode an image file as a data: URL for the OpenAI vision format."""
    try:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{b64}"
    except OSError as e:
        logger.warning("Cannot read image %s: %s", path, e)
        return None


def ask_local_vision(
    prompt: str,
    image_paths: list[Path],
    system: str | None = None,
    max_tokens: int = VL_MAX_TOKENS,
) -> str | None:
    """Multimodal completion on the local VL model (Qwen3-VL). None on failure."""
    content: list[dict] = []
    n_ok = 0
    for p in image_paths:
        url = _image_data_url(p)
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
            n_ok += 1
    if n_ok == 0:
        return None
    if prompt:
        content.append({"type": "text", "text": prompt})
    messages = [{"role": "system", "content": system or DEFAULT_SYSTEM},
                {"role": "user", "content": content}]
    try:
        r = httpx.post(
            f"{VL_LLM_URL}/v1/chat/completions",
            json={"messages": messages, "max_tokens": max_tokens, "temperature": 0.4},
            timeout=VL_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        text = text.strip()
        if not text:
            logger.warning("Local VL returned empty content")
            return None
        return text
    except Exception as e:
        logger.warning("Local VL call failed: %s", e)
        return None

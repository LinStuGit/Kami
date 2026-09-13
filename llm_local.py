#!/usr/bin/env python3
"""Local llama.cpp (llama-server) client for fast replies.

The daemon keeps llama-server.exe running at 127.0.0.1:8899 with an
OpenAI-compatible /v1/chat/completions endpoint. ask_local() returns the
reply text, or None when the server is unavailable / errored so the caller
can fall back to Claude.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

LOCAL_LLM_URL = "http://127.0.0.1:8899"
LOCAL_TIMEOUT_S = 60
LOCAL_MAX_TOKENS = 512

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

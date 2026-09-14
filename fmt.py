#!/usr/bin/env python3
"""WeChat markdown formatting kit — one visual language for every reply.

WeChat renders markdown (verified via /mdtest): ## headings, **bold**,
inline code, fenced code cards with copy buttons, --- rules, links and
lists. Every bot output is built from these primitives: an ## icon
header, ▸ section markers, ▪ key-value lines (CJK-aware aligned labels)
and ① numbered steps.

    from fmt import block, kv, section, nums

    block("✅", "工单完成", kv("模型", "Claude Code"), "", body)

Renderer quirks (re-check with /mdtest before relying on new syntax):
- A heading only renders at a block start — keep a blank line before it
  (the very start of a message counts as one).
- Task-list syntax ``- [x]`` does NOT render; md_to_wechat rewrites it.
- Tables are unverified — keep alignment inside fenced text blocks.
"""

import unicodedata

LINE = "━" * 12      # legacy plain-text divider (plugin compat)
SUB = "┈" * 12       # legacy light divider (plugin compat)
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"

_WIDE_RANGES = (
    (0x1F300, 0x1FAFF),  # emoji
    (0x2600, 0x27BF),    # misc symbols / dingbats
    (0x2190, 0x21FF),    # arrows
    (0x2B00, 0x2BFF),    # misc symbols and arrows
    (0x260, 0x27F),      # letterlike: ✓ ✔ ✗ …
)


def _charw(ch: str) -> int:
    if any(lo <= ord(ch) <= hi for lo, hi in _WIDE_RANGES):
        return 2
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1


def dispw(s: str) -> int:
    """Display width: CJK/full-width/emoji count as 2 cells."""
    return sum(_charw(c) for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - dispw(s))


def kv(key: str, value, width: int = 6) -> str:
    """▪ Key  Value with the label column aligned."""
    return f"▪ {pad(key, width)}{value}"


def section(name: str) -> str:
    return f"▸ {name}"


def nums(items, start: int = 1) -> list[str]:
    """Number a list with ①②③… (falls back to 'n.' past ⑫)."""
    out = []
    for i, item in enumerate(items, start):
        mark = CIRCLED[i - start] if i - start < len(CIRCLED) else f"{i}."
        out.append(f"{mark} {item}")
    return out


def block(icon: str, title: str, *sections) -> str:
    """## header, blank line, then the sections."""
    parts = [f"## {icon} {title}", ""]
    parts += [s for s in sections if s not in ("", None)]
    return "\n".join(parts)


def tag_reply(icon: str, source: str, body: str, meta: str = "") -> str:
    """Letterhead for model replies: ## icon + name, body, meta."""
    out = f"## {icon} {source}\n\n{body.strip()}"
    if meta:
        out = f"{out}\n\n---\n{meta}"
    return out


def footer(*lines: str) -> str:
    """--- rule + lines. The leading blank line keeps --- an <hr>,
    not a setext underline glued to the previous line."""
    return "\n\n---\n" + "\n".join(lines)


def rule(n: int = 12) -> str:
    return LINE[:n]

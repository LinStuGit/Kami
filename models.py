#!/usr/bin/env python3
"""Model registry — the seam for plugging new models into the bridge.

Every reply-capable backend is a :class:`ModelSpec` in the registry. The
bridge ships with three (claude / local / vl); plugins or future channels can
add more at runtime::

    from models import ModelSpec, register_model

    register_model(ModelSpec(
        key="gpt",
        name="GPT-x",
        kind="text",                       # "text" | "vision" | "agent"
        runner=my_run,                     # (message, user_id, images, files) -> str
        supports_images=True,
    ))

Routing and the ticket system read the registry, so a newly registered model
is immediately usable (and labelled on every reply it produces).
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import fmt

# Runner contract: blocking call, returns the reply text (markdown ok).
Runner = Callable[[str, str, list[Path], list[Path]], str]


@dataclass
class ModelSpec:
    key: str                        # routing id: "claude", "local", "vl", ...
    name: str                       # display name shown in reply tags
    kind: str = "text"              # "text" | "vision" | "agent" | "imagegen"
    runner: Runner | None = None
    supports_images: bool = False
    notes: str = ""                 # shown by /models
    icon: str = "🤖"                # letterhead icon on every reply
    group: str = ""                 # specialty group key, e.g. "quick"
    extra: dict = field(default_factory=dict)


@dataclass
class GroupSpec:
    """A specialty group: models that share a domain, in escalation order.

    ``chain`` is the tiered-dispatch order for /task <group>: the head runs
    first, and each failure escalates the ticket to the next tier (usually
    ending at the agent model, which can attempt almost anything).
    """

    key: str                        # "quick", "reason", "vision", ...
    name: str                       # display name: "秒答"
    icon: str = "🧩"
    desc: str = ""                  # what this group is good at
    chain: list[str] = field(default_factory=list)  # escalation order


_REGISTRY: dict[str, ModelSpec] = {}
_GROUPS: dict[str, GroupSpec] = {}
_lock = threading.Lock()


def register_model(spec: ModelSpec) -> ModelSpec:
    """Register (or replace) a model. Returns the spec now in the registry."""
    with _lock:
        _REGISTRY[spec.key] = spec
    return spec


def get_model(key: str) -> ModelSpec | None:
    with _lock:
        return _REGISTRY.get(key)


def all_models() -> list[ModelSpec]:
    with _lock:
        return list(_REGISTRY.values())


def register_group(spec: GroupSpec) -> GroupSpec:
    """Register (or replace) a specialty group."""
    with _lock:
        _GROUPS[spec.key] = spec
    return spec


def get_group(key: str) -> GroupSpec | None:
    with _lock:
        return _GROUPS.get(key)


def all_groups() -> list[GroupSpec]:
    with _lock:
        return list(_GROUPS.values())


def describe_models() -> str:
    """Formatted, group-organized list for the /models command."""
    with _lock:
        models = list(_REGISTRY.values())
        groups = list(_GROUPS.values())
    if not models:
        return "🧩 尚无已注册模型。"

    def model_line(m: ModelSpec) -> str:
        caps = []
        if m.supports_images:
            caps.append("vision")
        if m.kind == "agent":
            caps.append("tools")
        cap_s = f" ｜{','.join(caps)}" if caps else ""
        note = f" — {m.notes}" if m.notes else ""
        return f"  {m.icon} {fmt.pad(m.key, 20)}{m.name}{cap_s}{note}"

    lines = [f"## 🧩 模型分组 · {len(groups) or 1} 组 / {len(models)} 模型", ""]
    claimed: set[str] = set()
    for g in groups:
        members = [m for m in models if m.group == g.key]
        claimed.update(m.key for m in members)
        chain = " → ".join(g.chain) if g.chain else g.key
        head = f"{g.icon} {g.name} · {g.desc}" if g.desc else f"{g.icon} {g.name}"
        lines.append(fmt.section(f"{head}"))
        lines.append(f"  ↗ 升级链: {chain}")
        lines += [model_line(m) for m in members]
    others = [m for m in models if m.key not in claimed]
    if others:
        lines.append(fmt.section("其他"))
        lines += [model_line(m) for m in others]
    lines.append(fmt.footer(
        "/task 分组 消息 按梯队派遣 · /task 模型 消息 指定模型 · /ask 直问"))
    return "\n".join(lines)

#!/usr/bin/env python3
"""Plugin system for WeChat-Claude Bridge.

Drop a Python file into the ``plugins/`` directory and it is loaded
automatically at startup (files starting with ``_`` are skipped).
Each file defines one or more subclasses of :class:`Plugin`:

    class WeatherPlugin(Plugin):
        name = "weather"
        description = "Query weather"
        commands = {"/weather": "weather <city> - query city weather"}

        def handle_command(self, cmd, args, ctx):
            return f"Weather in {args}: sunny"   # return str to reply

Available hooks (all optional):

    on_start()                       after the bridge is up
    on_stop()                        before the bridge exits
    handle_command(cmd, args, ctx)   slash-command dispatch (before built-ins)
    on_message(text, ctx)            messages headed to the agent; return a
                                     reply str to short-circuit, None to pass
    on_response(text, resp, ctx)     transform the agent reply before sending

``ctx`` is a :class:`PluginContext` with ``reply()``, ``ask_agent()``,
``memory``, ``scheduler``, ``user_id`` and the current working directory.
Exceptions raised inside one plugin never take down the bridge.
"""

import importlib.util
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent / "plugins"


# ── Plugin Context ──────────────────────────────────────────────


@dataclass
class PluginContext:
    """Per-message handles passed to plugin hooks. Do not store it."""

    send_text: Callable[[str, str, str], bool]  # (to_user, context_token, text)
    send_file_fn: Callable[[str, str, Path], bool]  # (to_user, context_token, path)
    call_agent: Callable[..., str]  # (message, user_id, working_dir, image_paths)
    memory: Any  # MemoryStore
    scheduler: Any  # Scheduler
    user_id: str
    context_token: str
    working_dir: str | None = None

    def reply(self, text: str) -> bool:
        """Send text back to the current user."""
        return self.send_text(self.user_id, self.context_token, text)

    def send_file(self, path: str | Path) -> bool:
        """Send a local file to the current user."""
        return self.send_file_fn(self.user_id, self.context_token, Path(path))

    def ask_agent(self, message: str) -> str:
        """Run a one-off query through the current AI agent."""
        return self.call_agent(message, self.user_id, self.working_dir, None)


# ── Plugin Base Class ───────────────────────────────────────────


class Plugin:
    """Base class — subclass it and override the hooks you need."""

    name: str = "plugin"
    description: str = ""
    # "/cmd" -> one-line usage shown by /plugins
    commands: ClassVar[dict[str, str]] = {}

    def on_start(self) -> None:
        """Called once when the bridge (re)loads plugins."""

    def on_stop(self) -> None:
        """Called once before the bridge exits or reloads plugins."""

    def handle_command(self, cmd: str, args: str, ctx: PluginContext) -> str | None:
        """Handle a registered slash command. Return reply text, or None."""
        return None

    def on_message(self, text: str, ctx: PluginContext) -> str | None:
        """See every message before it reaches the agent.

        Return a string to reply and short-circuit, or None to continue.
        """
        return None

    def on_response(self, text: str, response: str, ctx: PluginContext) -> str:
        """Post-process the agent reply. Return the (possibly new) text."""
        return response


# ── Plugin Manager ──────────────────────────────────────────────


class PluginManager:
    """Discovers, loads and dispatches to plugins in ``plugins/``."""

    def __init__(self) -> None:
        self._plugins: list[Plugin] = []
        self._lock = threading.Lock()
        self._handles: dict[str, Callable] = {}

    def bind(self, **handles: Callable) -> None:
        """Wire bridge capabilities into plugin contexts."""
        self._handles.update(handles)

    def _make_ctx(
        self, user_id: str, context_token: str, working_dir: str | None
    ) -> PluginContext:
        return PluginContext(
            send_text=self._handles["send_text"],
            send_file_fn=self._handles["send_file_fn"],
            call_agent=self._handles["call_agent"],
            memory=self._handles["memory"],
            scheduler=self._handles["scheduler"],
            user_id=user_id,
            context_token=context_token,
            working_dir=working_dir,
        )

    # ── Discovery / lifecycle ──

    def load(self) -> int:
        """Import every module in plugins/ and register Plugin subclasses.

        Returns the number of plugins loaded.
        """
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        loaded: list[Plugin] = []
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"weclaude_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("no loader")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                for obj in vars(module).values():
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, Plugin)
                        and obj is not Plugin
                    ):
                        loaded.append(obj())
            except Exception as e:
                logger.error("Failed to load plugin %s: %s", path.name, e)

        with self._lock:
            self._plugins = loaded
        for p in loaded:
            try:
                p.on_start()
                cmds = " ".join(p.commands) or "(hooks only)"
                logger.info("Plugin loaded: %s — %s %s", p.name, p.description, cmds)
            except Exception as e:
                logger.error("Plugin %s on_start failed: %s", p.name, e)
        return len(loaded)

    def unload(self) -> None:
        """Run on_stop on every plugin and clear the registry."""
        with self._lock:
            plugins, self._plugins = self._plugins, []
        for p in plugins:
            try:
                p.on_stop()
            except Exception as e:
                logger.error("Plugin %s on_stop failed: %s", p.name, e)

    def reload(self) -> int:
        self.unload()
        return self.load()

    # ── Dispatch (all exception-isolated) ──

    def dispatch_command(self, cmd: str, ctx: PluginContext) -> str | None:
        """Try plugin slash commands first. Returns reply text or None."""
        with self._lock:
            plugins = list(self._plugins)
        for p in plugins:
            for registered in p.commands:
                if cmd == registered or cmd.startswith(registered + " "):
                    args = cmd[len(registered):].strip()
                    try:
                        return p.handle_command(registered, args, ctx)
                    except Exception as e:
                        logger.error(
                            "Plugin %s handle_command(%s) failed: %s",
                            p.name,
                            registered,
                            e,
                        )
                        return f"[Plugin {p.name} error: {e}]"
        return None

    def dispatch_message(self, text: str, ctx: PluginContext) -> str | None:
        """Let plugins intercept a message before it goes to the agent."""
        with self._lock:
            plugins = list(self._plugins)
        for p in plugins:
            try:
                reply = p.on_message(text, ctx)
                if reply is not None:
                    return reply
            except Exception as e:
                logger.error("Plugin %s on_message failed: %s", p.name, e)
        return None

    def transform_response(
        self, text: str, response: str, ctx: PluginContext
    ) -> str:
        """Run the agent reply through every plugin's on_response."""
        with self._lock:
            plugins = list(self._plugins)
        for p in plugins:
            try:
                response = p.on_response(text, response, ctx)
            except Exception as e:
                logger.error("Plugin %s on_response failed: %s", p.name, e)
        return response

    # ── Info ──

    def list_plugins(self) -> str:
        """Formatted plugin list for the /plugins command."""
        with self._lock:
            plugins = list(self._plugins)
        if not plugins:
            return (
                "No plugins loaded.\n"
                f"Add .py files to: {PLUGIN_DIR}"
            )
        lines = [f"Loaded plugins ({len(plugins)}):\n"]
        for p in plugins:
            lines.append(f"  {p.name} — {p.description or '(no description)'}")
            for c, help_text in p.commands.items():
                lines.append(f"    {help_text or c}")
        return "\n".join(lines)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._plugins)

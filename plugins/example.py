#!/usr/bin/env python3
"""Example Kami plugin — demonstrates the plugin interface.

Copy this file, rename the class, and you have a working plugin.
See plugins.py for the full hook documentation.
"""

import random
from datetime import datetime
from typing import ClassVar

from plugins import Plugin, PluginContext


class ExamplePlugin(Plugin):
    name = "example"
    description = "Demo plugin: /echo /time /roll, and a global hook demo"
    commands: ClassVar[dict[str, str]] = {
        "/echo": "/echo <text> - repeat text back",
        "/time": "/time - show server time",
        "/roll": "/roll [sides] - roll a dice",
    }

    def handle_command(self, cmd: str, args: str, ctx: PluginContext) -> str | None:
        if cmd == "/echo":
            return args or "(nothing to echo)"
        if cmd == "/time":
            return f"Server time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if cmd == "/roll":
            sides = int(args) if args.isdigit() and int(args) > 1 else 6
            return f"🎲 {random.randint(1, sides)} (1-{sides})"
        return None

    def on_message(self, text: str, ctx: PluginContext) -> str | None:
        # Short-circuit demo: reply "ping" without reaching Claude.
        # Return None (default) to let the message pass through normally.
        if text.strip().lower() == "ping":
            return "pong"
        return None

    def on_response(self, text: str, response: str, ctx: PluginContext) -> str:
        # Post-process demo: append nothing, just show the hook.
        # e.g. filter words, log to external service, shorten links...
        return response

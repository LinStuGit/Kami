"""Shared on-disk locations for Kami + one-time legacy path migration.

Every module that needs the config directory imports CONFIG_DIR from here
so the rename (wechat-claude-bridge → kami) happens in exactly one place,
with a best-effort migration that moves an existing pre-rename directory
into place on first import.
"""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "kami"
LEGACY_NAME = "wechat-claude-bridge"  # pre-rename installs

CONFIG_DIR = Path.home() / ".config" / APP_NAME


def _migrate() -> None:
    """Move a pre-rename ~/.config/wechat-claude-bridge into place."""
    old = Path.home() / ".config" / LEGACY_NAME
    if old.exists() and not CONFIG_DIR.exists():
        try:
            shutil.move(str(old), str(CONFIG_DIR))
            logger.info("Migrated %s -> %s", old, CONFIG_DIR)
        except OSError as e:
            logger.warning("Config migration failed: %s", e)


_migrate()

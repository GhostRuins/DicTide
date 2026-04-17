"""Persist UI settings to %LOCALAPPDATA%\\DicTide\\settings.json."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

_DIR_NAME = "DicTide"
_LEGACY_DIR_NAME = "WhisperDictation"
_FILE_NAME = "settings.json"
_MIGRATION_MARKER = ".migrated_from_whisperdictation"
_MIGRATION_ATTEMPTED = False
_LOG = logging.getLogger(__name__)


def _migrate_legacy_dir_if_needed(base: Path) -> None:
    global _MIGRATION_ATTEMPTED
    if _MIGRATION_ATTEMPTED:
        return
    _MIGRATION_ATTEMPTED = True

    new_dir = base / _DIR_NAME
    old_dir = base / _LEGACY_DIR_NAME

    if new_dir.exists() or not old_dir.is_dir():
        return
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for pattern in ("settings.json", "app.log", "app.log.*"):
            for src in old_dir.glob(pattern):
                if not src.is_file():
                    continue
                dst = new_dir / src.name
                if dst.exists():
                    continue
                shutil.copy2(src, dst)
                copied += 1
        marker = new_dir / _MIGRATION_MARKER
        marker.write_text("legacy migration complete\n", encoding="utf-8")
        if copied > 0:
            _LOG.info(
                "Migrated %s file(s) from %s to %s",
                copied,
                old_dir,
                new_dir,
            )
    except OSError as e:
        _LOG.warning("Legacy settings migration failed: %s", e)


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        base_path = Path(base)
    else:
        base_path = Path.home()
    _migrate_legacy_dir_if_needed(base_path)
    return base_path / _DIR_NAME


def settings_path() -> Path:
    return data_dir() / _FILE_NAME


def load() -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(partial: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load()
    current.update(partial)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)

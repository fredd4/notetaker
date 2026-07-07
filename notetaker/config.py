"""Stores API keys in a local config file.

Uses ~/Library/Application Support/Notetaker/config.json with 0600
permissions (readable only by the current user), rather than the macOS
Keychain. Keychain access control lists are tied to the app's exact code
signature — with the free ad-hoc signing this app uses, every rebuild
produces a different signature, so the OS treats it as a new, unrecognized
app and re-prompts for the login password on every launch. A local file
avoids that friction entirely and is simpler for a personal, single-user
tool; the OS still protects it via normal filesystem permissions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Notetaker"
CONFIG_FILE = CONFIG_DIR / "config.json"

OPENAI_KEY_NAME = "openai_api_key"
NOTION_TOKEN_NAME = "notion_token"
NOTION_PARENT_NAME = "notion_parent_page_id"


def _load() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(CONFIG_FILE, 0o600)


def _get(name: str) -> Optional[str]:
    return _load().get(name) or None


def _set(name: str, value: str) -> None:
    data = _load()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    _save(data)


def get_openai_key() -> Optional[str]:
    return _get(OPENAI_KEY_NAME)


def set_openai_key(value: str) -> None:
    _set(OPENAI_KEY_NAME, value.strip())


def get_notion_token() -> Optional[str]:
    return _get(NOTION_TOKEN_NAME)


def set_notion_token(value: str) -> None:
    _set(NOTION_TOKEN_NAME, value.strip())


def get_notion_parent_page() -> Optional[str]:
    return _get(NOTION_PARENT_NAME)


def set_notion_parent_page(value: str) -> None:
    _set(NOTION_PARENT_NAME, value.strip())


def status() -> dict:
    """Key status for the UI — never returns the actual values."""
    return {
        "openai_set": bool(get_openai_key()),
        "notion_set": bool(get_notion_token()),
        "notion_parent_set": bool(get_notion_parent_page()),
    }

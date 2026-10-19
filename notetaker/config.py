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
NOTION_CLIENT_ID_NAME = "notion_oauth_client_id"
NOTION_CLIENT_SECRET_NAME = "notion_oauth_client_secret"
NOTION_OAUTH_TOKEN_NAME = "notion_oauth_access_token"
NOTION_OAUTH_REFRESH_NAME = "notion_oauth_refresh_token"
NOTION_OAUTH_WORKSPACE_NAME = "notion_oauth_workspace_name"

# Sentinel "parent" meaning "top level of the user's Private section" —
# only possible with a user-owned OAuth token (public integration), since
# Notion refuses workspace-level pages for internal integrations.
NOTION_WORKSPACE_PARENT = "workspace"


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


def get_notion_client_id() -> Optional[str]:
    return _get(NOTION_CLIENT_ID_NAME)


def set_notion_client_id(value: str) -> None:
    _set(NOTION_CLIENT_ID_NAME, value.strip())


def get_notion_client_secret() -> Optional[str]:
    return _get(NOTION_CLIENT_SECRET_NAME)


def set_notion_client_secret(value: str) -> None:
    _set(NOTION_CLIENT_SECRET_NAME, value.strip())


def get_notion_oauth_token() -> Optional[str]:
    return _get(NOTION_OAUTH_TOKEN_NAME)


def get_notion_oauth_refresh_token() -> Optional[str]:
    return _get(NOTION_OAUTH_REFRESH_NAME)


def set_notion_oauth(access_token: str, refresh_token: str = "", workspace_name: str = "") -> None:
    """Stores (or, with empty values, clears) the OAuth connection in one write."""
    data = _load()
    for name, value in (
        (NOTION_OAUTH_TOKEN_NAME, access_token),
        (NOTION_OAUTH_REFRESH_NAME, refresh_token),
        (NOTION_OAUTH_WORKSPACE_NAME, workspace_name),
    ):
        if value:
            data[name] = value
        else:
            data.pop(name, None)
    _save(data)


def get_notion_destination() -> tuple[Optional[str], Optional[str]]:
    """Returns (token, parent) for Notion sync.

    An OAuth connection wins: notes go to the top level of the user's
    Private section (parent == NOTION_WORKSPACE_PARENT). Otherwise falls
    back to the internal integration token + parent page ID. (None, None)
    when Notion isn't configured.
    """
    oauth_token = get_notion_oauth_token()
    if oauth_token:
        return oauth_token, NOTION_WORKSPACE_PARENT
    token, parent = get_notion_token(), get_notion_parent_page()
    if token and parent:
        return token, parent
    return None, None


def status() -> dict:
    """Key status for the UI — never returns the actual values."""
    return {
        "openai_set": bool(get_openai_key()),
        "notion_set": bool(get_notion_token()),
        "notion_parent_set": bool(get_notion_parent_page()),
        "notion_client_set": bool(get_notion_client_id() and get_notion_client_secret()),
        "notion_oauth_connected": bool(get_notion_oauth_token()),
        "notion_oauth_workspace": _get(NOTION_OAUTH_WORKSPACE_NAME) or "",
    }

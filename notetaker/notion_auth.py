"""Notion OAuth (public integration) — lets notes land at the top level of
the user's Private section, which an internal integration token can't do.

One-time setup in Notion (https://www.notion.so/profile/integrations):
create a *public* integration with capabilities Read / Update / Insert
content and redirect URI REDIRECT_URI below, then paste its OAuth client
ID and secret into Settings and click "Connect Notion".

The flow: open the authorize URL in the browser → the user approves →
Notion redirects to a one-shot HTTP server on localhost → we exchange the
code for a user-owned access token and store it in config.

CLI (same flow, for dev):  .venv/bin/python -m notetaker.notion_auth
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config

logger = logging.getLogger(__name__)

REDIRECT_PORT = 53682
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"
TIMEOUT_SECONDS = 300

_PAGE = """<!doctype html><meta charset="utf-8"><title>Notetaker</title>
<body style="font-family:-apple-system,sans-serif;background:#1c1c1e;color:#f2f2f7;
display:flex;align-items:center;justify-content:center;height:90vh">
<div style="text-align:center"><h2>{title}</h2><p style="color:#8e8e93">{message}</p></div>"""


def authorize_url(client_id: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })


def _token_request(payload: dict) -> dict:
    client_id = config.get_notion_client_id()
    client_secret = config.get_notion_client_secret()
    if not (client_id and client_secret):
        raise RuntimeError("Notion OAuth client ID/secret not set (Settings).")
    resp = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        json=payload,
        headers={"Notion-Version": "2022-06-28"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Notion token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def exchange_code(code: str) -> dict:
    """Exchanges an authorization code for tokens and stores them."""
    data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    config.set_notion_oauth(
        data["access_token"],
        data.get("refresh_token") or "",
        data.get("workspace_name") or "",
    )
    logger.info("Notion OAuth connected (workspace=%s)", data.get("workspace_name"))
    return data


def refresh_access_token() -> Optional[str]:
    """Trades the stored refresh token for a new access token. Returns the
    new token, or None when there is no refresh token to use."""
    refresh_token = config.get_notion_oauth_refresh_token()
    if not refresh_token:
        return None
    data = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    config.set_notion_oauth(
        data["access_token"],
        data.get("refresh_token") or refresh_token,
        data.get("workspace_name") or config.status()["notion_oauth_workspace"],
    )
    logger.info("Notion OAuth access token refreshed")
    return data["access_token"]


def disconnect() -> None:
    config.set_notion_oauth("", "", "")


def run_authorization(on_done: Optional[Callable[[dict], None]] = None) -> None:
    """Opens the browser for consent and waits (in a background thread) for
    the redirect. on_done gets {"ok": True, "workspace": ...} or
    {"ok": False, "error": ...}. Raises right away if the client ID/secret
    is missing or the callback port is taken."""
    client_id = config.get_notion_client_id()
    if not (client_id and config.get_notion_client_secret()):
        raise RuntimeError("Paste the Notion OAuth client ID and secret first.")

    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if qs.get("state") != state:
                result.update(ok=False, error="State mismatch — try connecting again.")
            elif "error" in qs:
                result.update(ok=False, error=f"Notion returned: {qs['error']}")
            elif "code" not in qs:
                result.update(ok=False, error="No authorization code in the redirect.")
            else:
                try:
                    data = exchange_code(qs["code"])
                    result.update(ok=True, workspace=data.get("workspace_name") or "")
                except Exception as exc:
                    logger.exception("Notion OAuth code exchange failed")
                    result.update(ok=False, error=str(exc))

            if result.get("ok"):
                body = _PAGE.format(title="Notion connected", message="You can close this tab and return to Notetaker.")
            else:
                body = _PAGE.format(title="Connection failed", message=result.get("error", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.timeout = 1

    def wait() -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        try:
            # Keep serving until /callback arrives (stray requests like
            # /favicon.ico just get a 404) or we give up.
            while not result and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not result:
            result.update(ok=False, error="Timed out waiting for Notion authorization.")
        if not result.get("ok"):
            logger.warning("Notion OAuth failed: %s", result.get("error"))
        if on_done:
            on_done(result)

    threading.Thread(target=wait, daemon=True).start()
    subprocess.run(["open", authorize_url(client_id, state)], check=False)


def main() -> int:
    done = threading.Event()
    outcome: dict = {}

    def on_done(res: dict) -> None:
        outcome.update(res)
        done.set()

    run_authorization(on_done)
    print("Browser opened — approve access in Notion…")
    done.wait(TIMEOUT_SECONDS + 5)
    if outcome.get("ok"):
        print(f"Connected to Notion workspace: {outcome.get('workspace')}")
        return 0
    print(f"Failed: {outcome.get('error')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

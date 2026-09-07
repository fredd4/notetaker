"""GUI application (pywebview) — window with a record button and progress.

Run:  ./.venv/bin/python -m notetaker.app
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Optional

import webview

from . import ROOT, check_ffmpeg_installed, config, ensure_ffmpeg_on_path, fix_ssl_cert_env
from .audio import list_input_devices
from .log import setup_logging
from .session import RecordingSession

UI_FILE = ROOT / "ui" / "index.html"

logger = logging.getLogger(__name__)


class Api:
    """Methods called from JavaScript through the pywebview bridge (window.pywebview.api.*)."""

    def __init__(self) -> None:
        self.session: Optional[RecordingSession] = None
        self.window: Optional[webview.Window] = None
        self._polling = False
        self._poller: Optional[threading.Thread] = None

    # --- API for JS ---

    def list_devices(self) -> list[dict]:
        try:
            return list_input_devices()
        except Exception:
            return []

    def start(self, mic_device: Optional[int] = None) -> dict:
        ffmpeg_error = check_ffmpeg_installed()
        if ffmpeg_error:
            return {"ok": False, "error": ffmpeg_error}
        try:
            self.session = RecordingSession(mic_device=mic_device)
            self.session.start()
        except Exception as exc:  # missing permission, missing helper, etc.
            logger.exception("Failed to start recording session")
            return {"ok": False, "error": str(exc)}
        self._start_polling()
        return {"ok": True}

    def stop(self) -> dict:
        self._polling = False
        if self._poller:
            self._poller.join(timeout=1.0)
        if not self.session:
            return {"ok": False, "error": "no active session"}
        try:
            path = self.session.stop()
        except Exception as exc:
            logger.exception("Failed to stop/mix recording session")
            self._push({"state": "error", "error": str(exc)})
            return {"ok": False, "error": str(exc)}

        st = self.session.status()
        st["folder"] = str(self.session.dir)
        st["path"] = str(path)
        self._push(st)

        api_key = config.get_openai_key()
        if not api_key:
            self.session.state = "done"
            st = {"state": "done", "folder": str(self.session.dir), "no_key": True}
            self._push(st)
            return {"ok": True, "path": str(path)}

        notion_token = config.get_notion_token()
        notion_parent = config.get_notion_parent_page()
        threading.Thread(
            target=self._run_pipeline,
            args=(self.session, api_key, notion_token, notion_parent),
            daemon=True,
        ).start()
        return {"ok": True, "path": str(path)}

    def _run_pipeline(
        self,
        session: RecordingSession,
        api_key: str,
        notion_token: Optional[str],
        notion_parent: Optional[str],
    ) -> None:
        # This runs on a background thread — an uncaught exception here
        # would otherwise vanish silently (daemon threads don't print
        # tracebacks anywhere the user would see, and nothing would ever
        # update the UI out of "transcribing..."). session.process() already
        # catches and reports its own errors, but this is a safety net for
        # anything unexpected (e.g. a bug in on_event itself).
        def on_event(state: str, extra: dict) -> None:
            data = {"state": state, "folder": str(session.dir)}
            data.update(extra)
            if state == "done":
                data["notes_path"] = str(session.notes_path)
                data["transcript_path"] = str(session.transcript_path)
                data["title"] = (session.notes or {}).get("title")
                data["notion_url"] = session.notion_url
                data["notion_error"] = session.notion_error
            self._push(data)

        try:
            session.process(api_key, notion_token, notion_parent, on_event=on_event)
        except Exception as exc:
            logger.exception("Uncaught exception in pipeline thread for session %s", session.dir)
            self._push({
                "state": "error",
                "folder": str(session.dir),
                "error": f"Unexpected internal error: {exc}. See log file for details.",
            })

    # --- settings / API keys ---

    def get_key_status(self) -> dict:
        return config.status()

    def save_keys(
        self,
        openai_key: str = "",
        notion_token: str = "",
        notion_parent: str = "",
    ) -> dict:
        try:
            if openai_key:
                config.set_openai_key(openai_key)
            if notion_token:
                config.set_notion_token(notion_token)
            if notion_parent:
                config.set_notion_parent_page(notion_parent)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "status": config.status()}

    def clear_key(self, name: str) -> dict:
        setters = {
            "openai": config.set_openai_key,
            "notion": config.set_notion_token,
            "notion_parent": config.set_notion_parent_page,
        }
        fn = setters.get(name)
        if not fn:
            return {"ok": False, "error": f"unknown key: {name}"}
        fn("")
        return {"ok": True, "status": config.status()}

    def open_folder(self) -> dict:
        if self.session and self.session.dir:
            subprocess.run(["open", str(self.session.dir)])
            return {"ok": True}
        return {"ok": False}

    def open_url(self, url: str) -> dict:
        if url:
            subprocess.run(["open", url])
            return {"ok": True}
        return {"ok": False}

    # --- internal ---

    def _start_polling(self) -> None:
        self._polling = True

        def loop() -> None:
            while self._polling and self.session:
                self._push(self.session.status())
                time.sleep(0.4)

        self._poller = threading.Thread(target=loop, daemon=True)
        self._poller.start()

    def _push(self, data: dict) -> None:
        if self.window:
            try:
                self.window.evaluate_js(f"window.onStatus({json.dumps(data)})")
            except Exception:
                logger.debug("Failed to push status to webview (window closed?)", exc_info=True)


def main() -> None:
    setup_logging()
    logger.info("Notetaker starting")
    ensure_ffmpeg_on_path()
    fix_ssl_cert_env()
    api = Api()
    window = webview.create_window(
        "Notetaker",
        str(UI_FILE),
        js_api=api,
        width=420,
        height=600,
        min_size=(380, 520),
        background_color="#1c1c1e",
    )
    api.window = window

    # On macOS, closing the window normally leaves the process running in
    # the background (standard Mac app behavior) — but that means a stale
    # process from before a permission grant keeps getting reused instead
    # of a fresh one that can see the new grant. For this single-window
    # tool, closing the window should just quit the app entirely.
    def _quit() -> None:
        os._exit(0)

    window.events.closed += _quit

    webview.start()


if __name__ == "__main__":
    main()

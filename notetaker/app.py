"""GUI application (pywebview) — window with a record button and progress.

Run:  ./.venv/bin/python -m notetaker.app
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

import webview

from . import ROOT, check_ffmpeg_installed, config, ensure_ffmpeg_on_path, fix_ssl_cert_env
from .audio import list_input_devices
from .log import LOG_FILE, setup_logging
from .session import RecordingSession

UI_FILE = ROOT / "ui" / "index.html"

logger = logging.getLogger(__name__)

# Matches the standard log line format from notetaker.log.setup_logging:
# "YYYY-MM-DD HH:MM:SS LEVEL   name: message"
_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\w+)\s")
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# How much of the tail of the log file to read before filtering/slicing —
# generous enough to reliably find the last few hundred lines without ever
# loading the whole (potentially multi-MB) file into memory.
_LOG_READ_WINDOW = 256 * 1024


def _notify(title: str, message: str) -> None:
    """Fires a native macOS notification in a background thread.

    Best-effort only: failures (e.g. no `osascript`, notifications disabled)
    are swallowed and logged at debug level, matching how _push already
    treats a missing/closed webview.
    """

    def run() -> None:
        try:
            # Escape backslashes first, then quotes, so the AppleScript
            # string literal stays well-formed.
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        except Exception:
            logger.debug("Failed to show native notification", exc_info=True)

    threading.Thread(target=run, daemon=True).start()


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
        if os.environ.get("NOTETAKER_DEBUG_ERROR") == "1":
            logger.info("NOTETAKER_DEBUG_ERROR=1: will push a sample error state in 3s")
            threading.Thread(target=self._debug_push_sample_error, daemon=True).start()
        return {"ok": True}

    def _debug_push_sample_error(self) -> None:
        """Dev-only helper (NOTETAKER_DEBUG_ERROR=1) for exercising the error
        panel in the UI without a real failure or any paid API calls."""
        time.sleep(3)
        self._push({
            "state": "error",
            "folder": str(self.session.dir) if self.session and self.session.dir else "",
            "error": "DEBUG: sample error for UI testing (NOTETAKER_DEBUG_ERROR=1)",
        })

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
                if session.cleanup_error:
                    data["cleanup_error"] = session.cleanup_error
            if state == "error":
                _notify("Notetaker — error", extra.get("error") or "Recording failed. See the app for details.")
            self._push(data)

        try:
            session.process(api_key, notion_token, notion_parent, on_event=on_event)
            # Cleanup (which can set session.cleanup_error) runs *after*
            # process() has already emitted its "done" event, so a failure
            # there can't be included in that event's payload — push a
            # follow-up update now that it's known.
            if session.cleanup_error:
                self._push({
                    "state": "done",
                    "folder": str(session.dir),
                    "notion_url": session.notion_url,
                    "title": (session.notes or {}).get("title"),
                    "cleanup_error": session.cleanup_error,
                })
                _notify(
                    "Notetaker — cleanup warning",
                    "Notes are in Notion, but local files could not be fully removed.",
                )
            elif session.state == "done":
                if session.notion_error:
                    _notify(
                        "Notetaker — Notion sync failed",
                        "Notes were saved locally, but Notion sync failed.",
                    )
                else:
                    title = (session.notes or {}).get("title") or "Notes"
                    _notify("Notetaker — done", f'"{title}" is ready.')
        except Exception as exc:
            logger.exception("Uncaught exception in pipeline thread for session %s", session.dir)
            _notify("Notetaker — error", "Unexpected internal error. See the app for details.")
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

    def open_log(self) -> dict:
        """Opens the log file in whatever app is registered for .log files
        (Console.app or TextEdit, typically) so the user can inspect it
        without a terminal."""
        try:
            subprocess.Popen(["open", str(LOG_FILE)])
            return {"ok": True, "path": str(LOG_FILE)}
        except Exception as exc:
            logger.exception("Failed to open log file %s", LOG_FILE)
            return {"ok": False, "error": str(exc)}

    def get_log_tail(self, lines: int = 200, min_level: str = "INFO") -> dict:
        """Returns the last `lines` log lines (after filtering by level) for
        the in-app log viewer.

        Reads only the last _LOG_READ_WINDOW bytes of the file so this stays
        fast even once the log has rotated up to its 5 MB cap — no need to
        load the whole file to show a couple hundred lines.
        """
        try:
            n_lines = int(lines)
        except (TypeError, ValueError):
            n_lines = 200
        n_lines = max(1, min(n_lines, 5000))

        min_level = (min_level or "INFO").upper()
        min_idx = _LOG_LEVELS.index(min_level) if min_level in _LOG_LEVELS else 1

        if not LOG_FILE.exists():
            return {"ok": True, "exists": False, "path": str(LOG_FILE), "lines": []}

        try:
            size = LOG_FILE.stat().st_size
            read_size = min(size, _LOG_READ_WINDOW)
            with open(LOG_FILE, "rb") as f:
                f.seek(size - read_size)
                raw = f.read()
            text = raw.decode("utf-8", errors="replace")
            all_lines = text.splitlines()

            filtered: list[str] = []
            # A line without a recognized "LEVEL" token (e.g. a traceback
            # continuation line) inherits the level of the line before it,
            # so multi-line errors don't get silently cut out of the
            # "Warnings+Errors" filter.
            current_idx = 1  # INFO, in case the read window starts mid-record
            for line in all_lines:
                m = _LOG_LINE_RE.match(line)
                if m:
                    lvl = m.group(1).upper()
                    current_idx = _LOG_LEVELS.index(lvl) if lvl in _LOG_LEVELS else current_idx
                if current_idx >= min_idx:
                    filtered.append(line)

            return {
                "ok": True,
                "exists": True,
                "path": str(LOG_FILE),
                "lines": filtered[-n_lines:],
                "truncated": size > read_size,
            }
        except OSError as exc:
            logger.exception("Failed to read log file %s", LOG_FILE)
            return {"ok": False, "error": str(exc), "path": str(LOG_FILE)}

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

"""Persistent logging for Notetaker.

Historically all pipeline errors went only to the live webview (via
on_event/_push in app.py) and were lost the moment the window closed or the
error happened before anyone was looking — which is exactly how a stuck
Whisper call on a 100-minute recording went unnoticed. This module sets up
the standard library `logging` package with a rotating file handler so
every run leaves a trail on disk, in addition to stderr.

Call setup_logging() once, as early as possible in each entry point
(notetaker.app.main() and notetaker.reprocess.main()). It is idempotent, so
calling it more than once (e.g. accidentally from both a module and its
caller) is harmless.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / "Library" / "Logs" / "Notetaker"
LOG_FILE = LOG_DIR / "notetaker.log"

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 3  # keep notetaker.log + 3 rotated backups

_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    """Configures the root logger with a rotating file handler + stderr.

    Never pass API keys or other secrets to logging calls — this only
    controls where log records go, not what callers choose to log.
    """
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    _configured = True
    logging.getLogger(__name__).info("Logging initialized -> %s", LOG_FILE)

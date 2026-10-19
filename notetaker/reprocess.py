"""CLI to resume/redo processing of an existing Notetaker session folder.

Handles exactly the failure mode that motivated this module: a recording
finished (mic.wav/system.wav or recording.mp3 exist on disk) but the
pipeline never reached Notion — e.g. a Whisper API call hung, the app
crashed, or the machine slept mid-run. Since previously there was no
logging and no way to resume, such a session just silently never arrived.

Run it the same way session.process() is driven by the app after stop():
  1. mix mic/system wavs into recording.mp3, if that hasn't happened yet
  2. transcribe recording.mp3 (resuming any already-transcribed chunks),
     unless transcript.txt already exists
  3. summarize the transcript
  4. write local outputs (notes.md, transcript.txt)
  5. sync to Notion, unless --no-notion
  6. delete the local session folder, only if Notion sync succeeded and
     --no-cleanup was not given

Usage:
    .venv/bin/python -m notetaker.reprocess <session_dir_or_name> [--no-notion] [--no-cleanup]

<session_dir_or_name> may be an absolute/relative path to the session
folder, or just its folder name (e.g. 2026-09-04_122558), resolved under
~/Documents/Notetaker.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import RECORDINGS_DIR, check_ffmpeg_installed, config, ensure_ffmpeg_on_path, fix_ssl_cert_env
from .log import LOG_FILE, setup_logging
from .session import RecordingSession

logger = logging.getLogger(__name__)


def _resolve_session_dir(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.exists():
        return p.resolve()
    candidate = RECORDINGS_DIR / arg
    if candidate.exists():
        return candidate.resolve()
    raise SystemExit(
        f"Session folder not found: {arg!r} (looked for {p} and {candidate})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m notetaker.reprocess",
        description=(
            "Resume/redo processing (mix -> transcribe -> summarize -> "
            "local outputs -> Notion) for an existing Notetaker session folder."
        ),
    )
    parser.add_argument(
        "session",
        help="Path to a session folder, or its folder name under ~/Documents/Notetaker",
    )
    parser.add_argument(
        "--no-notion",
        action="store_true",
        help="Skip syncing to Notion even if credentials are configured",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep the local session folder even if Notion sync succeeds",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reprocess even if state.json says it was already synced to "
            "Notion — WILL create a duplicate page"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    setup_logging()
    ensure_ffmpeg_on_path()
    fix_ssl_cert_env()

    ffmpeg_error = check_ffmpeg_installed()
    if ffmpeg_error:
        print(f"Error: {ffmpeg_error}", file=sys.stderr)
        return 1

    session_dir = _resolve_session_dir(args.session)
    print(f"Reprocessing session: {session_dir}")
    logger.info("reprocess CLI starting for %s (no_notion=%s no_cleanup=%s)",
                session_dir, args.no_notion, args.no_cleanup)

    api_key = config.get_openai_key()
    if not api_key:
        print(
            "Error: no OpenAI API key configured. Run the app once and save "
            "it in Settings, or set it via notetaker.config.set_openai_key().",
            file=sys.stderr,
        )
        return 1

    notion_token = None
    notion_parent = None
    if not args.no_notion:
        notion_token, notion_parent = config.get_notion_destination()
        if not notion_token:
            print("Notion not configured — skipping Notion sync (local outputs only).")

    session = RecordingSession.from_existing_dir(session_dir)

    def on_event(state: str, extra: dict) -> None:
        msg = f"[{state}]"
        if extra:
            msg += " " + ", ".join(f"{k}={v}" for k, v in extra.items())
        print(msg)

    session.process(
        api_key,
        notion_token=notion_token,
        notion_parent=notion_parent,
        on_event=on_event,
        cleanup=not args.no_cleanup,
        force=args.force,
    )

    if session.error:
        print(f"FAILED: {session.error}", file=sys.stderr)
        print(f"See log file for details: {LOG_FILE}", file=sys.stderr)
        return 1

    if session.cleanup_error:
        print(f"Cleanup error: {session.cleanup_error}", file=sys.stderr)
        return 1

    print(f"Done. state={session.state}")
    if session.notes_path and session.notes_path.exists():
        print(f"Notes: {session.notes_path}")
    if session.transcript_path and session.transcript_path.exists():
        print(f"Transcript: {session.transcript_path}")
    if session.notion_url:
        print(f"Notion: {session.notion_url}")
    elif session.notion_error:
        print(f"Notion sync failed: {session.notion_error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

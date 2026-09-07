"""Recording session: ties together mic + system audio and mixes the result.

Each session gets its own timestamped folder under ~/Documents/Notetaker.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import RECORDINGS_DIR
from .audio import MicRecorder, SystemAudioRecorder, mix_to_mp3
from .outputs import create_notion_page, write_local_outputs
from .pipeline import summarize, transcribe
from .pipeline.summarize import MODEL as SUMMARY_MODEL

DEFAULT_REC_DIR = RECORDINGS_DIR

logger = logging.getLogger(__name__)


class RecordingSession:
    def __init__(
        self,
        base_dir: str | Path = DEFAULT_REC_DIR,
        mic_device: Optional[int] = None,
    ):
        self.base_dir = Path(base_dir)
        self.mic_device = mic_device
        self.dir: Optional[Path] = None
        self.mic: Optional[MicRecorder] = None
        self.system: Optional[SystemAudioRecorder] = None
        self.started_at: Optional[float] = None
        self.state = "idle"  # idle|recording|mixing|transcribing|summarizing|done|error
        self.mixed_path: Optional[Path] = None

        self.mic_wav: Optional[Path] = None
        self.sys_wav: Optional[Path] = None

        self.transcript: Optional[str] = None
        self.notes: Optional[dict] = None
        self.notes_path: Optional[Path] = None
        self.transcript_path: Optional[Path] = None
        self.error: Optional[str] = None
        self.notion_url: Optional[str] = None
        self.notion_error: Optional[str] = None
        self.cleanup_error: Optional[str] = None

    @classmethod
    def from_existing_dir(cls, path: str | Path) -> "RecordingSession":
        """Rehydrates a session object for a folder that already exists on
        disk — a finished (or half-finished) recording, possibly from a
        previous process that crashed or hung before reaching Notion.

        Used by the `notetaker.reprocess` CLI so it can drive the exact
        same `process()` code path the app uses after stop(), instead of
        duplicating the pipeline logic.
        """
        path = Path(path).resolve()
        session = cls(base_dir=path.parent)
        session.dir = path
        session.mic_wav = path / "mic.wav"
        session.sys_wav = path / "system.wav"
        session.mixed_path = path / "recording.mp3"
        session.transcript_path = path / "transcript.txt"
        session.notes_path = path / "notes.md"

        # Best effort: recover started_at from the folder name
        # (YYYY-MM-DD_HHMMSS) so a Notion date prefix still matches the
        # actual recording date; fall back to the folder's mtime.
        try:
            session.started_at = datetime.strptime(path.name, "%Y-%m-%d_%H%M%S").timestamp()
        except ValueError:
            try:
                session.started_at = path.stat().st_mtime
            except OSError:
                session.started_at = None

        if session.mixed_path.exists():
            session.state = "mixed"
        return session

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def start(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.dir = self.base_dir / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mic_wav = self.dir / "mic.wav"
        self.sys_wav = self.dir / "system.wav"

        logger.info("Starting recording session in %s", self.dir)

        # Najpierw dźwięk systemowy — start() blokuje do READY i rzuca przy braku zgody.
        self.system = SystemAudioRecorder(self.sys_wav)
        self.system.start()
        self.mic = MicRecorder(self.mic_wav, device=self.mic_device)
        self.mic.start()

        self.started_at = time.time()
        self.state = "recording"
        logger.info("Recording started (session %s)", self.dir.name)

    @staticmethod
    def _size(p: Optional[Path]) -> int:
        return p.stat().st_size if p and p.exists() else 0

    def status(self) -> dict:
        return {
            "state": self.state,
            "elapsed": round(self.elapsed, 1),
            "mic_bytes": self._size(self.mic_wav),
            "system_bytes": self._size(self.sys_wav),
        }

    def _write_state(self, stage: str, **extra) -> None:
        """Persists a small state.json in the session dir so a failed or
        interrupted session can be diagnosed (or resumed) after the fact,
        without needing the live webview that was open at the time.
        """
        if not self.dir or not self.dir.exists():
            return
        data = {
            "stage": stage,
            "chunk": extra.get("chunk", extra.get("index")),
            "chunks": extra.get("chunks", extra.get("total")),
            "error": self.error,
            "notion_url": self.notion_url,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            (self.dir / "state.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Failed to write state.json in %s", self.dir)

    def _read_existing_state(self) -> Optional[dict]:
        """Best-effort read of state.json from a session dir that may
        already exist on disk (a resumed/reprocessed session). Returns
        None if there is no dir, no state.json, or it can't be parsed.
        """
        if not self.dir:
            return None
        state_path = self.dir / "state.json"
        if not state_path.exists():
            return None
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("Failed to read existing state.json in %s", self.dir)
            return None

    def _cleanup_session_dir(self, cleanup: bool) -> None:
        """Removes self.dir once it is safe to do so (Notion sync
        succeeded). Tracks any per-file removal failures instead of
        silently swallowing them (previously `ignore_errors=True`), and
        verifies afterwards that the directory is actually gone.
        """
        if not self.notion_url:
            return
        if not cleanup:
            logger.info(
                "Notion sync succeeded but cleanup disabled; keeping %s", self.dir
            )
            return

        assert self.dir is not None
        logger.info(
            "Cleaning up session dir (Notion sync succeeded, starting): %s", self.dir
        )

        failures: list[tuple[str, str]] = []

        def _on_rmtree_error(func, path, exc_info) -> None:
            # `onexc` (3.12+) passes the exception instance itself;
            # `onerror` (<3.12) passes a sys.exc_info() tuple.
            exc = exc_info if isinstance(exc_info, BaseException) else exc_info[1]
            logger.warning("Cleanup failed to remove %s: %s", path, exc)
            failures.append((str(path), str(exc)))

        if sys.version_info >= (3, 12):
            shutil.rmtree(self.dir, onexc=_on_rmtree_error)
        else:
            shutil.rmtree(self.dir, onerror=_on_rmtree_error)

        if self.dir.exists():
            remaining = sorted(str(p) for p in self.dir.rglob("*"))
            self.cleanup_error = (
                f"Cleanup incomplete, {len(remaining)} files remain in {self.dir} "
                f"(Notion URL: {self.notion_url}): {remaining}"
            )
            logger.error(
                "Cleanup incomplete, %d files remain in %s (Notion URL: %s): %s",
                len(remaining), self.dir, self.notion_url, remaining,
            )
        else:
            logger.info("Cleanup done: %s", self.dir)

    def _ensure_mixed(self) -> None:
        """Makes sure self.mixed_path exists, mixing mic/system wavs into
        it if not. Used both right after a live recording (stop()) and by
        the reprocess CLI resuming a session whose mix step never
        completed (recording.mp3 missing but mic.wav/system.wav present).
        """
        assert self.dir is not None
        if self.mixed_path is None:
            self.mixed_path = self.dir / "recording.mp3"
        if self.mixed_path.exists():
            return

        self.state = "mixing"
        self._write_state("mixing")
        logger.info(
            "Mixing audio for %s (mic=%s system=%s)",
            self.dir.name, self.mic_wav, self.sys_wav,
        )
        mix_to_mp3(self.mixed_path, mic_wav=self.mic_wav, system_wav=self.sys_wav)
        logger.info(
            "Mix done: mic=%d bytes, system=%d bytes, mixed=%d bytes",
            self._size(self.mic_wav), self._size(self.sys_wav), self._size(self.mixed_path),
        )
        self.state = "mixed"

    def stop(self) -> Path:
        logger.info("Stopping recording session %s", self.dir)
        if self.mic:
            self.mic.stop()
        if self.system:
            self.system.stop()

        assert self.dir is not None
        self.mixed_path = self.dir / "recording.mp3"
        self._ensure_mixed()
        return self.mixed_path

    def process(
        self,
        api_key: str,
        notion_token: Optional[str] = None,
        notion_parent: Optional[str] = None,
        on_event=None,
        cleanup: bool = True,
        force: bool = False,
    ) -> None:
        """Transkrypcja + notatki + zapis lokalny (+ opcjonalnie Notion).

        Dopóki synchronizacja z Notion się nie powiedzie, zapis lokalny jest
        źródłem prawdy — jeśli wysyłka do Notion zawiedzie, nie przerywa to
        całego procesu (notatki i tak są bezpieczne na dysku), tylko zgłasza
        błąd osobno. Gdy wysyłka się powiedzie, Notion staje się źródłem
        prawdy i lokalny folder sesji (nagrania audio, transkrypt, notatki)
        jest usuwany, żeby nie zajmować miejsca na dysku (chyba że
        cleanup=False, np. z CLI --no-cleanup).

        This is also the entry point used by the `notetaker.reprocess` CLI
        to resume a session that already exists on disk: if recording.mp3
        is missing it gets mixed from mic/system wavs, and if transcript.txt
        already exists it is reused instead of calling Whisper again (the
        transcribe() call itself additionally resumes at the chunk level).

        If the session dir's state.json already records a `notion_url`
        (a previous run synced to Notion successfully but the folder
        survived — e.g. a crash before cleanup, or --no-cleanup), this
        skips straight to the cleanup step instead of re-running the
        pipeline, to avoid creating a duplicate Notion page. Pass
        force=True to bypass that guard and reprocess anyway.
        """

        def emit(state: str, **extra) -> None:
            self.state = state
            self._write_state(state, **extra)
            if on_event:
                on_event(state, extra)

        try:
            assert self.dir is not None

            if not force:
                existing_state = self._read_existing_state()
                existing_url = (existing_state or {}).get("notion_url")
                if existing_url:
                    self.notion_url = existing_url
                    logger.info(
                        "Session already synced to Notion (%s); skipping processing",
                        existing_url,
                    )
                    emit("done", resumed=True, notion_url=existing_url)
                    self._cleanup_session_dir(cleanup)
                    return

            if self.mixed_path is None or not Path(self.mixed_path).exists():
                self._ensure_mixed()

            transcript_txt_path = self.dir / "transcript.txt"

            def transcribe_event(kind: str, kw: dict) -> None:
                if kind == "chunk_start":
                    logger.info("Transcribe chunk %s/%s: start", kw.get("index"), kw.get("total"))
                elif kind == "chunk_done":
                    logger.info(
                        "Transcribe chunk %s/%s: done in %.1fs",
                        kw.get("index"), kw.get("total"), kw.get("duration", 0.0),
                    )
                elif kind == "chunk_skipped":
                    logger.info(
                        "Transcribe chunk %s/%s: already done, skipped (resume)",
                        kw.get("index"), kw.get("total"),
                    )
                emit("transcribing", **kw)

            if transcript_txt_path.exists():
                logger.info(
                    "transcript.txt already present, reusing instead of re-transcribing: %s",
                    transcript_txt_path,
                )
                self.transcript = transcript_txt_path.read_text(encoding="utf-8")
                emit("transcribing", resumed=True)
            else:
                emit("transcribing")
                logger.info("Transcribe start: %s", self.mixed_path)
                self.transcript = transcribe(
                    self.mixed_path, api_key=api_key, on_event=transcribe_event
                )
                logger.info("Transcribe done: %d chars", len(self.transcript or ""))

                # Persist the raw transcript immediately, before summarizing.
                # write_local_outputs() below will overwrite this later with
                # the reflowed version once summarize() succeeds — but if the
                # GPT call fails, this is what keeps an expensive Whisper
                # result (money + time) from surviving only as
                # transcript_chunk_NNN.txt files.
                transcript_txt_path.write_text(self.transcript, encoding="utf-8")
                logger.info("Raw transcript written: %s", transcript_txt_path)

            emit("summarizing")
            logger.info("Summarize start (model=%s)", SUMMARY_MODEL)

            def summarize_event(kind: str, kw: dict) -> None:
                emit("summarizing", **kw)

            self.notes = summarize(self.transcript, api_key=api_key, on_event=summarize_event)
            logger.info("Summarize done (model=%s): title=%r", SUMMARY_MODEL, (self.notes or {}).get("title"))

            out = write_local_outputs(self.dir, self.notes, self.transcript)
            self.notes_path = Path(out["notes_path"])
            self.transcript_path = Path(out["transcript_path"])
            logger.info("Local outputs written: %s, %s", self.notes_path, self.transcript_path)

            if notion_token and notion_parent:
                emit("syncing_notion")
                logger.info("Notion sync start (parent=%s)", notion_parent)
                try:
                    date_prefix = (
                        datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d")
                        if self.started_at
                        else None
                    )
                    result = create_notion_page(
                        self.notes,
                        self.transcript,
                        notion_parent,
                        notion_token,
                        date_prefix=date_prefix,
                    )
                    self.notion_url = result.get("url")
                    logger.info("Notion sync done: %s", self.notion_url)
                except Exception as exc:
                    logger.exception("Notion sync failed")
                    self.notion_error = str(exc)

            # Record "done" (incl. state.json) while the dir still exists —
            # cleanup below may delete it, and state.json has nothing left
            # to diagnose once the whole folder is gone anyway.
            emit("done")

            self._cleanup_session_dir(cleanup)
        except Exception as exc:
            logger.exception("Pipeline failed for session %s", self.dir)
            self.error = str(exc)
            emit("error", error=str(exc))

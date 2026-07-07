"""Recording session: ties together mic + system audio and mixes the result.

Each session gets its own timestamped folder under ~/Documents/Notetaker.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import RECORDINGS_DIR
from .audio import MicRecorder, SystemAudioRecorder, mix_to_mp3
from .outputs import create_notion_page, write_local_outputs
from .pipeline import summarize, transcribe

DEFAULT_REC_DIR = RECORDINGS_DIR


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

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def start(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.dir = self.base_dir / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mic_wav = self.dir / "mic.wav"
        self.sys_wav = self.dir / "system.wav"

        # Najpierw dźwięk systemowy — start() blokuje do READY i rzuca przy braku zgody.
        self.system = SystemAudioRecorder(self.sys_wav)
        self.system.start()
        self.mic = MicRecorder(self.mic_wav, device=self.mic_device)
        self.mic.start()

        self.started_at = time.time()
        self.state = "recording"

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

    def stop(self) -> Path:
        self.state = "mixing"
        if self.mic:
            self.mic.stop()
        if self.system:
            self.system.stop()

        assert self.dir is not None
        self.mixed_path = self.dir / "recording.mp3"
        mix_to_mp3(self.mixed_path, mic_wav=self.mic_wav, system_wav=self.sys_wav)
        self.state = "mixed"
        return self.mixed_path

    def process(
        self,
        api_key: str,
        notion_token: Optional[str] = None,
        notion_parent: Optional[str] = None,
        on_event=None,
    ) -> None:
        """Transkrypcja + notatki + zapis lokalny (+ opcjonalnie Notion).

        Zapis lokalny jest źródłem prawdy — jeśli synchronizacja z Notion
        się nie powiedzie, nie przerywa to całego procesu (notatki i tak
        są bezpieczne na dysku), tylko zgłasza błąd osobno.
        """

        def emit(state: str, **extra) -> None:
            self.state = state
            if on_event:
                on_event(state, extra)

        try:
            assert self.mixed_path is not None and self.dir is not None

            def transcribe_event(kind: str, kw: dict) -> None:
                emit("transcribing", **kw)

            emit("transcribing")
            self.transcript = transcribe(
                self.mixed_path, api_key=api_key, on_event=transcribe_event
            )

            def summarize_event(kind: str, kw: dict) -> None:
                emit("summarizing", **kw)

            emit("summarizing")
            self.notes = summarize(self.transcript, api_key=api_key, on_event=summarize_event)

            out = write_local_outputs(self.dir, self.notes, self.transcript)
            self.notes_path = Path(out["notes_path"])
            self.transcript_path = Path(out["transcript_path"])

            if notion_token and notion_parent:
                emit("syncing_notion")
                try:
                    result = create_notion_page(
                        self.notes, self.transcript, notion_parent, notion_token
                    )
                    self.notion_url = result.get("url")
                except Exception as exc:
                    self.notion_error = str(exc)

            emit("done")
        except Exception as exc:
            self.error = str(exc)
            emit("error", error=str(exc))

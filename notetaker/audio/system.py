"""Wrapper na natywny helper Swift (build/sysaudio) do dźwięku systemowego.

Uruchamia helper jako podproces, czyta jego tokeny stanu ze stderr
(READY/AUDIO/FRAMES/STOPPED/ERROR) w osobnym wątku i udostępnia prosty
interfejs start()/stop() z callbackiem postępu.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .. import SYSAUDIO_HELPER

# Sygnatura callbacku: (typ_zdarzenia, wartość) — np. ("frames", 96000) albo ("ready", None)
EventCallback = Callable[[str, Optional[int]], None]

SAMPLE_RATE = 48000  # helper nagrywa 48 kHz stereo


class SystemAudioRecorder:
    def __init__(
        self,
        out_path: str | Path,
        helper_path: str | Path = SYSAUDIO_HELPER,
        on_event: Optional[EventCallback] = None,
    ):
        self.out_path = str(out_path)
        self.helper_path = str(helper_path)
        self.on_event = on_event

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self.frames = 0
        self.saw_audio = False

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    def _emit(self, kind: str, value: Optional[int] = None) -> None:
        if self.on_event:
            self.on_event(kind, value)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            line = raw.strip()
            if not line:
                continue
            if line == "READY":
                self._ready.set()
                self._emit("ready")
            elif line == "AUDIO":
                self.saw_audio = True
                self._emit("audio")
            elif line.startswith("FRAMES "):
                try:
                    self.frames = int(line.split(" ", 1)[1])
                    self._emit("frames", self.frames)
                except ValueError:
                    pass
            elif line == "STOPPED":
                self._emit("stopped")
            elif line.startswith("ERROR"):
                self._error = line[6:].strip() if len(line) > 6 else line
                self._ready.set()  # odblokuj oczekujący start()
                self._emit("error")

    def start(self, ready_timeout: float = 10.0) -> None:
        """Starts the helper and blocks until READY or ERROR.

        Raises RuntimeError if the helper reports an error (e.g. screen
        recording permission not granted) or fails to start in time.
        """
        if not Path(self.helper_path).exists():
            raise FileNotFoundError(
                f"Helper not found: {self.helper_path} — run scripts/build_helper.sh"
            )
        self._proc = subprocess.Popen(
            [self.helper_path, self.out_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # liniowe buforowanie
        )
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()

        if not self._ready.wait(timeout=ready_timeout):
            self.stop()
            raise RuntimeError("System audio helper did not start in time")
        if self._error:
            self.stop()
            raise RuntimeError(f"System audio: {self._error}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stops the helper with SIGINT so it closes the WAV file cleanly."""
        if not self._proc:
            return
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if self._reader:
            self._reader.join(timeout=2.0)
        self._proc = None

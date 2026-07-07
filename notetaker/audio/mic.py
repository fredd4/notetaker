"""Nagrywanie mikrofonu do pliku WAV przez sounddevice.

Dane z callbacku audio trafiają do kolejki, a osobny wątek zapisuje je do
pliku — dzięki temu w callbacku (czas rzeczywisty) nie ma blokującego I/O.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Optional

import sounddevice as sd
import soundfile as sf

SAMPLE_RATE = 48000  # domyślny, gdy nie znamy natywnego samplerate urządzenia


class MicRecorder:
    def __init__(
        self,
        out_path: str | Path,
        samplerate: Optional[int] = None,
        channels: int = 1,
        device: Optional[int] = None,
    ):
        self.out_path = str(out_path)
        self.channels = channels
        self.device = device

        # Nagrywaj w natywnym samplerate urządzenia — unika sztucznego
        # przepróbkowania (np. 24 kHz z AirPods udawane jako 48 kHz).
        if samplerate is None:
            try:
                info = sd.query_devices(device, kind="input")
                samplerate = int(info["default_samplerate"])
            except Exception:
                samplerate = SAMPLE_RATE
        self.samplerate = samplerate

        self._queue: "queue.Queue" = queue.Queue()
        self._stream: Optional[sd.InputStream] = None
        self._file: Optional[sf.SoundFile] = None
        self._writer: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames / self.samplerate

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        # Uwaga: to kod czasu rzeczywistego — tylko szybka kopia do kolejki.
        self._queue.put(indata.copy())
        self.frames += frames

    def _write_loop(self) -> None:
        assert self._file is not None
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                block = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._file.write(block)

    def start(self) -> None:
        self._file = sf.SoundFile(
            self.out_path,
            mode="w",
            samplerate=self.samplerate,
            channels=self.channels,
            subtype="PCM_16",
        )
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._stop.set()
        if self._writer:
            self._writer.join(timeout=5.0)
            self._writer = None
        if self._file:
            self._file.close()
            self._file = None

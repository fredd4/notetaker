"""Nagrywanie mikrofonu do pliku WAV przez sounddevice.

Dane z callbacku audio trafiają do kolejki, a osobny wątek zapisuje je do
pliku — dzięki temu w callbacku (czas rzeczywisty) nie ma blokującego I/O.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000  # domyślny, gdy nie znamy natywnego samplerate urządzenia

# How often the write loop flushes buffered audio to disk (seconds). This
# does NOT rewrite the RIFF header's size fields — libsndfile only does that
# on close() — but it does push written frames out of internal buffers, so a
# crash/hang leaves as little unflushed audio as possible. Even a file left
# with a stale/zero header (e.g. because close() never ran — an orphaned
# recorder, a crash) is still perfectly readable by ffmpeg/ffprobe: both
# recover by reading to EOF instead of trusting the RIFF chunk sizes, which
# is exactly why mix_to_mp3()/transcribe() have always worked on such files.
FLUSH_INTERVAL_SECONDS = 10.0

# Module-level registry of every MicRecorder that has started but not yet
# been cleanly stop()ped. Exists so a caller that's about to hard-exit the
# process (see app.py's window-closed handler) can force-close any recorder
# left running — finalizing its WAV header — instead of leaving it orphaned
# with mic.wav growing forever and never getting a valid header at all (the
# incident this module was hardened after: a second Api.start() call
# overwrote the session holding the only reference to a live MicRecorder,
# so nothing ever called stop() on it again).
_open_recorders: "set[MicRecorder]" = set()
_open_recorders_lock = threading.Lock()


def close_all_open_recorders() -> None:
    """Force-closes every MicRecorder still registered as open. Best-effort:
    each recorder is closed independently so one failure doesn't stop the
    rest, and any exception is logged rather than raised (this is meant to
    be called from a shutdown path, e.g. right before os._exit())."""
    with _open_recorders_lock:
        recorders = list(_open_recorders)
    for rec in recorders:
        try:
            rec.stop()
        except Exception:
            logger.exception("Failed to close orphaned MicRecorder for %s", rec.out_path)


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
        last_flush = time.monotonic()
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                block = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._file.write(block)
            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL_SECONDS:
                # Pushes buffered frames to disk periodically so a crash or
                # an orphaned recorder loses as little audio as possible.
                # Does not finalize the RIFF header (only close() does that)
                # — see FLUSH_INTERVAL_SECONDS docstring above.
                self._file.flush()
                last_flush = now

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
        with _open_recorders_lock:
            _open_recorders.add(self)

    def stop(self) -> None:
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            self._stop.set()
            if self._writer:
                self._writer.join(timeout=5.0)
                self._writer = None
        finally:
            # Always attempt to close the file, even if stopping the stream
            # or joining the writer thread raised — an unfinalized WAV
            # header (only written on close()) is exactly the failure mode
            # this module was hardened against.
            if self._file:
                self._file.close()
                self._file = None
            with _open_recorders_lock:
                _open_recorders.discard(self)

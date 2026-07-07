"""Transcription via the OpenAI Whisper API.

Large-file chunking logic ported from ~/.claude/scripts/transcribe.py. Our
input files are already mono 16 kHz 64 kbps (mixer.py), so we skip that
script's audio-extraction step — here we only check the size and split
if needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

API_LIMIT_BYTES = 24 * 1024 * 1024  # OpenAI's limit is ~25 MB, leave headroom

EventCallback = Callable[[str, dict], None]


def _get_audio_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        text=True,
    )
    return float(out.strip())


def _split_audio(src: Path, chunk_seconds: int) -> list[Path]:
    out_pattern = src.with_name(src.stem + "_chunk_%03d.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-c", "copy", str(out_pattern),
        ],
        check=True,
    )
    return sorted(src.parent.glob(src.stem + "_chunk_*.mp3"))


def transcribe(
    mp3_path: str | Path,
    api_key: str,
    language: str = "",
    model: str = "whisper-1",
    on_event: Optional[EventCallback] = None,
) -> str:
    """Transcribes the audio file, splitting it if needed. Returns the full text.

    language defaults to "" (auto-detect) so Whisper transcribes in whatever
    language was actually spoken, instead of forcing a single language —
    meetings may switch between Polish, English, or others.
    """
    from openai import OpenAI

    def emit(kind: str, **kw) -> None:
        if on_event:
            on_event(kind, kw)

    client = OpenAI(api_key=api_key)
    mp3_path = Path(mp3_path)
    size = mp3_path.stat().st_size

    if size <= API_LIMIT_BYTES:
        chunks = [mp3_path]
    else:
        duration = _get_audio_duration(mp3_path)
        ratio = API_LIMIT_BYTES / size
        chunk_seconds = max(60, int(duration * ratio * 0.9))
        chunks = _split_audio(mp3_path, chunk_seconds)

    lang = "" if language.lower() in ("auto", "") else language
    parts = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        emit("chunk_start", index=i, total=total)
        with open(chunk, "rb") as f:
            kwargs = dict(model=model, file=f, response_format="text")
            if lang:
                kwargs["language"] = lang
            text = client.audio.transcriptions.create(**kwargs)
        parts.append(text)
        emit("chunk_done", index=i, total=total)
        if chunk != mp3_path:
            chunk.unlink(missing_ok=True)

    return "\n\n".join(parts)

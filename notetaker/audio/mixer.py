"""Mixes two WAV tracks (microphone + system audio) into a single MP3.

Output: mono 16 kHz, 64 kbps — the format expected by Whisper (matches the
existing transcribe.py script). Either one or two tracks may be present.

Microphone and system audio can end up at very different loudness levels
(distance from the mic, how loud the other party is in Zoom/Chrome, etc.)
— which source is quieter isn't predictable ahead of time. So BOTH tracks
are normalized independently to the same level (EBU R128) before mixing,
and a limiter on the sum guards against clipping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

LOUDNORM = "loudnorm=I=-18:TP=-2:LRA=11"  # target: speech at an even, safe level


def mix_to_mp3(
    out_mp3: str | Path,
    mic_wav: Optional[str | Path] = None,
    system_wav: Optional[str | Path] = None,
    normalize: bool = True,
) -> Path:
    mic = str(mic_wav) if mic_wav and Path(mic_wav).exists() else None
    sysw = str(system_wav) if system_wav and Path(system_wav).exists() else None
    if not mic and not sysw:
        raise ValueError("No input tracks to mix")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    index = {}
    n = 0
    if mic:
        cmd += ["-i", mic]
        index["mic"] = n
        n += 1
    if sysw:
        cmd += ["-i", sysw]
        index["sys"] = n
        n += 1

    parts: list[str] = []
    labels: list[str] = []

    for key, label in (("mic", "mic"), ("sys", "sys")):
        if key not in index:
            continue
        src = f"[{index[key]}:a]"
        if normalize:
            # Each track is normalized INDEPENDENTLY to the same level —
            # which source is quieter (mic or system) depends on the
            # situation, so both need the same treatment.
            parts.append(f"{src}{LOUDNORM}[{label}]")
            labels.append(f"[{label}]")
        else:
            labels.append(src)

    if len(labels) == 2:
        parts.append(
            f"{labels[0]}{labels[1]}amix=inputs=2:duration=longest:normalize=0[mx]"
        )
        parts.append("[mx]alimiter=limit=0.95[a]")  # safety against clipping
    else:
        parts.append(f"{labels[0]}alimiter=limit=0.95[a]")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[a]"]
    cmd += ["-ac", "1", "-ar", "16000", "-b:a", "64k", str(out_mp3)]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mix failed:\n{proc.stderr}")
    return Path(out_mp3)

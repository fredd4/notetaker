"""Transcription via the OpenAI Whisper API.

Large-file chunking logic ported from ~/.claude/scripts/transcribe.py. Our
input files are already mono 16 kHz 64 kbps (mixer.py), so we skip that
script's audio-extraction step — here we only check duration/size and
split if needed.

Robustness notes (added after a 100-minute recording's first Whisper call
hung forever with no logging and no way to resume):
  - The OpenAI client gets an explicit generous timeout and its own SDK
    retries, on top of our own retry loop per chunk.
  - Each chunk's transcript is saved to disk (transcript_chunk_NNN.txt)
    right after it succeeds, so a crash/hang partway through doesn't lose
    already-finished work — a later call with the same input resumes by
    skipping chunks that already have that file.
  - A chunk's audio file is only deleted once its transcript has been
    saved successfully; a failed chunk's audio is left in place.

Splitting notes (added after empirical evidence that a 47-minute / 22.6 MB
chunk failed the Whisper API twice — once with a 500, once with a 400
"something went wrong reading your request" — while 10-minute / ~5 MB
chunks made with `ffmpeg -f segment -segment_time 600 -c copy` transcribed
reliably in ~60s each):
  - Splitting is DURATION-based, not size-based: any recording longer than
    CHUNK_SECONDS (600s / 10 min) is always split into CHUNK_SECONDS
    segments, regardless of how small the resulting files are. Files at or
    under that duration are sent whole.
  - API_LIMIT_BYTES is kept only as a defensive safety net for the
    (expected-never) case where a 10-minute segment still exceeds the
    API's ~25 MB limit — e.g. an unexpectedly high source bitrate. In that
    case we fall back to splitting that one segment further; if it's
    still too big, we raise a clear error instead of sending a doomed
    request.

Quality notes:
  - Each API call passes temperature=0 for deterministic, less
    hallucination-prone output.
  - Each chunk (after the first) passes the last ~200 characters of the
    previous chunk's transcript as `prompt=` context, so Whisper has
    continuity across the cut point instead of starting cold. On resume,
    this is derived from the previous chunk's saved transcript_chunk file
    exactly the same way (both live and resumed chunks feed the same
    `parts` list that continuity is read from).
  - Each chunk's raw transcript is post-processed by
    collapse_repetition_loops() before being saved, to strip Whisper
    "hallucination loops" (the model getting stuck repeating the same
    sentence, short sentence block, or filler phrase over and over).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

API_LIMIT_BYTES = 24 * 1024 * 1024  # OpenAI's limit is ~25 MB, leave headroom

# Any recording longer than this is split into CHUNK_SECONDS-long segments
# (ffmpeg stream-copy, no re-encoding). Chosen from empirical evidence that
# 10-minute / ~5 MB chunks transcribe reliably in ~60s, while a 47-minute /
# ~22.6 MB chunk (close to the API's byte limit) reliably failed.
CHUNK_SECONDS = 600  # 10 minutes

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [5, 15, 45]  # one entry per retry (attempt 1 -> 2, 2 -> 3)

# How much of the previous chunk's transcript to feed the next chunk as
# `prompt=` context, for continuity across the cut point.
PROMPT_CONTEXT_CHARS = 200

# --- Repetition-loop collapsing -------------------------------------------
# Two conservative passes, both touching ONLY strictly consecutive repeats
# (a repeat separated by other content is never touched):
#   1. Sentence-level (split on . ! ?): any consecutive run of one or more
#      identical (normalized) sentences repeating as a block collapses to a
#      single occurrence of that block. Threshold is 2 (any literal
#      back-to-back repeat) since this is a well-known Whisper failure mode.
#   2. Phrase-level: a short (<= 4 word) phrase with no sentence-ending
#      punctuation that repeats consecutively MORE than 5 times (i.e. 6+) —
#      e.g. "okay okay okay okay okay okay" — collapses to a single
#      occurrence. High threshold so ordinary short repeats in real speech
#      ("no no no") are left alone.
SENTENCE_BLOCK_MAX_N = 4
PHRASE_MAX_N_WORDS = 4
PHRASE_MIN_REPEATS = 6

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

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

    # Clear any stale chunk files from a previous split of this same source
    # at different boundaries (e.g. the target chunk duration changed, or a
    # prior run left extra trailing files) — otherwise leftover files from
    # an old split geometry could be miscounted as chunks of this run.
    # Chunks that already have a saved transcript get regenerated here too
    # (ffmpeg stream-copies, so this is cheap) and are simply skipped again
    # by the resume check in transcribe().
    for stale in src.parent.glob(src.stem + "_chunk_*.mp3"):
        stale.unlink(missing_ok=True)

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


def _enforce_size_limit(
    chunks: list[Path], chunk_seconds: int, protect: Optional[Path] = None,
) -> list[Path]:
    """Safety net: a CHUNK_SECONDS segment at 64 kbps is only a few MB, far
    under the ~25 MB API limit, so this should never trigger in practice.
    But if a segment is still oversized (e.g. an unexpectedly high source
    bitrate), try one fallback split at half the duration; if that's still
    not enough, raise a clear error rather than sending a doomed request.

    `protect` (if given) is never deleted, even if it needs to be split —
    used to guard the original input file from being unlinked.
    """
    result: list[Path] = []
    for c in chunks:
        size = c.stat().st_size
        if size <= API_LIMIT_BYTES:
            result.append(c)
            continue

        half = max(30, chunk_seconds // 2)
        logger.warning(
            "Chunk %s is %d bytes, over the %d-byte API limit even at %ds "
            "segments; retrying split at %ds",
            c.name, size, API_LIMIT_BYTES, chunk_seconds, half,
        )
        sub_chunks = _split_audio(c, half)
        if c != protect:
            c.unlink(missing_ok=True)
        for sub in sub_chunks:
            sub_size = sub.stat().st_size
            if sub_size > API_LIMIT_BYTES:
                raise RuntimeError(
                    f"Audio chunk {sub.name} is {sub_size} bytes, still over "
                    f"the {API_LIMIT_BYTES}-byte API limit even after "
                    f"splitting to {half}s segments. The source bitrate may "
                    "be unexpectedly high."
                )
            result.append(sub)
    return result


def _normalize(s: str) -> str:
    """lowercase, strip punctuation entirely, collapse whitespace."""
    s = s.lower()
    s = _NON_WORD_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def _split_sentences(text: str) -> list[str]:
    return _SENTENCE_SPLIT_RE.split(text)


def _collapse_ngram(units: list[str], max_n: int, min_repeats: int) -> tuple[list[str], int]:
    """Collapses consecutive runs of a repeating n-gram (1..max_n units
    long) down to a single occurrence, for any n-gram that repeats
    consecutively at least `min_repeats` times. Never touches non-adjacent
    repeats. Returns (new_units, removed_count) where removed_count is the
    number of repeated occurrences dropped (i.e. repeats - 1 per run).
    """
    n_units = len(units)
    normalized = [_normalize(u) for u in units]
    result: list[str] = []
    removed = 0
    i = 0
    while i < n_units:
        matched = False
        for n in range(1, max_n + 1):
            if i + n > n_units:
                break
            gram = normalized[i:i + n]
            if all(g == "" for g in gram):
                continue
            repeats = 1
            j = i + n
            while j + n <= n_units and normalized[j:j + n] == gram:
                repeats += 1
                j += n
            if repeats >= min_repeats:
                result.extend(units[i:i + n])
                removed += repeats - 1
                i = j
                matched = True
                break
        if not matched:
            result.append(units[i])
            i += 1
    return result, removed


def collapse_repetition_loops(text: str) -> tuple[str, int]:
    """Strips Whisper "hallucination loops" from a chunk's transcript.

    Two passes (see module docstring for the full rationale):
      1. Sentence-level: collapses a consecutive run of one or more
         identical (normalized) sentences/sentence-blocks to a single
         occurrence.
      2. Phrase-level: collapses a short (<=4 word) phrase, with no
         sentence punctuation, that repeats consecutively more than 5
         times, to a single occurrence.

    Pure function, no I/O. Returns (clean_text, removed_count). When
    nothing is collapsed, clean_text is returned unchanged (verbatim,
    including original whitespace) and removed_count is 0. Logs a WARNING
    with the removed count when removed_count > 0.
    """
    if not text or not text.strip():
        return text, 0

    sentences = _split_sentences(text)
    collapsed_sentences, sentence_removed = _collapse_ngram(
        sentences, SENTENCE_BLOCK_MAX_N, min_repeats=2,
    )
    if sentence_removed:
        text_after_sentences = _WHITESPACE_RE.sub(
            " ", " ".join(collapsed_sentences)
        ).strip()
    else:
        text_after_sentences = text

    words = text_after_sentences.split()
    collapsed_words, phrase_removed = _collapse_ngram(
        words, PHRASE_MAX_N_WORDS, min_repeats=PHRASE_MIN_REPEATS,
    )
    clean_text = " ".join(collapsed_words) if phrase_removed else text_after_sentences

    removed_count = sentence_removed + phrase_removed
    if removed_count:
        logger.warning(
            "collapse_repetition_loops: collapsed %d repeated segment(s) "
            "(%d sentence-level, %d phrase-level)",
            removed_count, sentence_removed, phrase_removed,
        )
    return clean_text, removed_count


def _transcribe_chunk_with_retry(
    client, chunk: Path, model: str, lang: str, prompt: str = "",
) -> str:
    """Calls the Whisper API for one chunk, retrying on any failure.

    3 attempts total with exponential backoff (5s / 15s / 45s between
    attempts). Each failure is logged with the full exception. Raises the
    last exception if every attempt fails.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with open(chunk, "rb") as f:
                kwargs = dict(model=model, file=f, response_format="text", temperature=0)
                if lang:
                    kwargs["language"] = lang
                if prompt:
                    kwargs["prompt"] = prompt
                return client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            logger.exception(
                "Transcription attempt %d/%d failed for %s",
                attempt, RETRY_ATTEMPTS, chunk.name,
            )
            if attempt < RETRY_ATTEMPTS:
                backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.info("Retrying %s in %ds", chunk.name, backoff)
                time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


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

    Resumable: if this is called again for the same mp3_path after a
    previous partial run, chunks that already produced a
    transcript_chunk_NNN.txt file (saved alongside mp3_path) are reused
    instead of calling the API again.
    """
    from openai import OpenAI

    def emit(kind: str, **kw) -> None:
        if on_event:
            on_event(kind, kw)

    # Generous explicit timeout (a chunk can legitimately take minutes) plus
    # the SDK's own retries, on top of our own retry loop below — belt and
    # suspenders after a call that hung indefinitely with the SDK's default
    # timeout.
    client = OpenAI(api_key=api_key, timeout=600, max_retries=3)
    mp3_path = Path(mp3_path)
    size = mp3_path.stat().st_size
    duration = _get_audio_duration(mp3_path)

    if duration <= CHUNK_SECONDS:
        chunks = [mp3_path]
        if size > API_LIMIT_BYTES:
            # Very unlikely (10 minutes at 64 kbps is only a few MB) but
            # handle it defensively rather than sending a doomed request.
            chunks = _enforce_size_limit(chunks, CHUNK_SECONDS, protect=mp3_path)
    else:
        chunks = _split_audio(mp3_path, CHUNK_SECONDS)
        chunks = _enforce_size_limit(chunks, CHUNK_SECONDS)

    lang = "" if language.lower() in ("auto", "") else language
    parts = []
    total = len(chunks)
    logger.info("Transcribing %s in %d chunk(s)", mp3_path.name, total)

    for i, chunk in enumerate(chunks, start=1):
        transcript_chunk_path = mp3_path.parent / f"transcript_chunk_{i:03d}.txt"

        # Continuity context for this chunk's API call: the tail of the
        # previous chunk's (already collapsed/final) transcript. Works the
        # same whether the previous chunk was just transcribed live or
        # loaded from disk on resume, since both paths append to `parts`.
        prompt_ctx = parts[-1][-PROMPT_CONTEXT_CHARS:] if parts else ""

        if transcript_chunk_path.exists():
            logger.info(
                "Chunk %d/%d already transcribed, skipping (resume): %s",
                i, total, transcript_chunk_path.name,
            )
            parts.append(transcript_chunk_path.read_text(encoding="utf-8"))
            emit("chunk_skipped", index=i, total=total)
            if chunk != mp3_path and chunk.exists():
                chunk.unlink(missing_ok=True)
            continue

        emit("chunk_start", index=i, total=total)
        logger.info("Chunk %d/%d start: %s", i, total, chunk.name)
        t0 = time.time()
        try:
            text = _transcribe_chunk_with_retry(client, chunk, model, lang, prompt=prompt_ctx)
        except Exception:
            logger.exception(
                "Chunk %d/%d failed after %d attempts, leaving audio in place: %s",
                i, total, RETRY_ATTEMPTS, chunk.name,
            )
            raise
        elapsed = time.time() - t0

        text, removed = collapse_repetition_loops(text)
        if removed:
            logger.warning(
                "Chunk %d/%d: collapsed %d repeated segment(s)", i, total, removed,
            )

        transcript_chunk_path.write_text(text, encoding="utf-8")
        parts.append(text)
        emit("chunk_done", index=i, total=total, duration=round(elapsed, 1))
        logger.info(
            "Chunk %d/%d done in %.1fs -> %s", i, total, elapsed, transcript_chunk_path.name,
        )

        # Only delete the chunk's audio now that its transcript is safely
        # on disk. Never delete the original mixed mp3 (chunk == mp3_path
        # when the file wasn't split at all).
        if chunk != mp3_path:
            chunk.unlink(missing_ok=True)

    full_text = "\n\n".join(parts)

    # All chunks succeeded and the caller now persists the full transcript
    # itself (session.py writes transcript.txt right after this returns), so
    # the per-chunk resume files are no longer needed. Only reached on the
    # success path — any failure above raises before this, leaving the
    # chunk files in place for a future resume.
    removed_chunk_files = 0
    for stale in mp3_path.parent.glob("transcript_chunk_*.txt"):
        stale.unlink(missing_ok=True)
        removed_chunk_files += 1
    logger.info(
        "Transcribe success: removed %d transcript_chunk_*.txt file(s) for %s",
        removed_chunk_files, mp3_path.name,
    )

    return full_text

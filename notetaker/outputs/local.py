"""Saves the results (transcript + notes) locally in the session folder."""

from __future__ import annotations

import re
from pathlib import Path

SENTENCE_RE = re.compile(r"[^.!?]*[.!?]+(?:\s+|$)|[^.!?]+$")


def _reflow_into_paragraphs(text: str, sentences_per_paragraph: int = 5) -> str:
    """Reformats a transcript into readable paragraphs.

    Whisper returns long recordings as one continuous block, and for
    recordings long enough to exceed the API's size limit, our own chunked
    transcription joins pieces at a fixed audio-duration cutoff that has no
    regard for word boundaries. This collapses all of that existing
    whitespace and regroups the text into paragraphs, always breaking on
    sentence-ending punctuation instead — never mid-word.
    """
    collapsed = " ".join(text.split())
    sentences = [s.strip() for s in SENTENCE_RE.findall(collapsed) if s.strip()]
    paragraphs = [
        " ".join(sentences[i : i + sentences_per_paragraph])
        for i in range(0, len(sentences), sentences_per_paragraph)
    ]
    return "\n\n".join(paragraphs)


def _point_text_and_subpoints(point) -> tuple[str, list[str]]:
    """Tolerates both the old shape (point is a plain string) and the new
    shape (point is {"text": ..., "subpoints": [...]})."""
    if isinstance(point, dict):
        return point.get("text", ""), point.get("subpoints") or []
    return str(point), []


def _render_markdown(notes: dict) -> str:
    lines = [f"# {notes.get('title', 'Meeting notes')}", ""]

    tldr = notes.get("tldr")
    if tldr:
        lines += ["## TL;DR", tldr, ""]

    action_items = notes.get("action_items") or []
    if action_items:
        lines.append("## Action Items")
        for item in action_items:
            due = item.get("due")
            task = item.get("task", "")
            # The task sentence usually already works the deadline in
            # (per the prompt); only append it separately if it isn't
            # already mentioned in the sentence, to avoid duplication.
            suffix = ""
            if due and due.lower() not in task.lower():
                suffix = f" (due: {due})"
            lines.append(f"- [ ] {task}{suffix}")
        lines.append("")

    topics = notes.get("topics") or []
    if topics:
        for topic in topics:
            heading = topic.get("heading", "")
            points = topic.get("points") or []
            lines.append(f"### {heading}")
            for point in points:
                text, subpoints = _point_text_and_subpoints(point)
                lines.append(f"- {text}")
                for sub in subpoints:
                    lines.append(f"  - {sub}")
            lines.append("")

    decisions = notes.get("decisions") or []
    if decisions:
        lines.append("## Decisions")
        lines += [f"- {d}" for d in decisions]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_local_outputs(session_dir: str | Path, notes: dict, transcript: str) -> dict:
    """Writes notes.md and transcript.txt in the session folder. Returns paths."""
    session_dir = Path(session_dir)
    notes_path = session_dir / "notes.md"
    transcript_path = session_dir / "transcript.txt"

    notes_path.write_text(_render_markdown(notes), encoding="utf-8")
    transcript_path.write_text(_reflow_into_paragraphs(transcript), encoding="utf-8")

    return {"notes_path": str(notes_path), "transcript_path": str(transcript_path)}

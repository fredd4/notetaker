"""Saves the results (transcript + notes) locally in the session folder."""

from __future__ import annotations

from pathlib import Path


def _render_markdown(notes: dict) -> str:
    lines = [f"# {notes.get('title', 'Meeting notes')}", ""]

    tldr = notes.get("tldr")
    if tldr:
        lines += ["## TL;DR", tldr, ""]

    action_items = notes.get("action_items") or []
    if action_items:
        lines.append("## Action Items")
        for item in action_items:
            owner = item.get("owner")
            due = item.get("due")
            task = item.get("task", "")
            tags = " ".join(
                f"**{v}**" for v in (owner, f"due: {due}" if due else None) if v
            )
            lines.append(f"- [ ] {task}" + (f" — {tags}" if tags else ""))
        lines.append("")

    decisions = notes.get("decisions") or []
    if decisions:
        lines.append("## Decisions")
        lines += [f"- {d}" for d in decisions]
        lines.append("")

    topics = notes.get("topics") or []
    if topics:
        lines.append("## Topics")
        for topic in topics:
            heading = topic.get("heading", "")
            points = topic.get("points") or []
            lines.append(f"### {heading}")
            lines += [f"- {p}" for p in points]
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_local_outputs(session_dir: str | Path, notes: dict, transcript: str) -> dict:
    """Writes notes.md and transcript.txt in the session folder. Returns paths."""
    session_dir = Path(session_dir)
    notes_path = session_dir / "notes.md"
    transcript_path = session_dir / "transcript.txt"

    notes_path.write_text(_render_markdown(notes), encoding="utf-8")
    transcript_path.write_text(transcript, encoding="utf-8")

    return {"notes_path": str(notes_path), "transcript_path": str(transcript_path)}

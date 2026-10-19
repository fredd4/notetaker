"""Saves meeting notes as a child page in Notion.

Requires the Notion integration (token) to be shared with the parent page —
in Notion, open the parent page, click "..." → Connections → add our
integration, otherwise the API returns 404 (no access, not "doesn't exist").
"""

from __future__ import annotations

import re
from typing import Optional

CHUNK_CHARS = 1900  # Notion's rich_text limit is 2000 chars; leave headroom
BATCH_BLOCKS = 90  # API limit is 100 blocks per request; leave headroom
WORKSPACE_PARENT = "workspace"  # mirrors config.NOTION_WORKSPACE_PARENT

_BOLD_RE = re.compile(r"(\*\*[^*]+\*\*)")


def _rich_text(content: str) -> list[dict]:
    """Builds a Notion rich_text array, turning **bold** markdown spans
    into proper bold annotations instead of leaving literal asterisks.

    Any single segment (bold or plain) longer than CHUNK_CHARS is split
    across multiple rich_text objects, since each object's own content is
    capped at Notion's 2000-char limit.
    """
    segments: list[dict] = []
    for part in _BOLD_RE.split(content):
        if not part:
            continue
        is_bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        text = part[2:-2] if is_bold else part
        for i in range(0, len(text), CHUNK_CHARS):
            piece = text[i : i + CHUNK_CHARS]
            if not piece:
                continue
            obj = {"type": "text", "text": {"content": piece}}
            if is_bold:
                obj["annotations"] = {"bold": True}
            segments.append(obj)
    return segments or [{"type": "text", "text": {"content": ""}}]


def _point_text_and_subpoints(point) -> tuple[str, list[str]]:
    """Tolerates both the old shape (point is a plain string) and the new
    shape (point is {"text": ..., "subpoints": [...]})."""
    if isinstance(point, dict):
        return point.get("text", ""), point.get("subpoints") or []
    return str(point), []


def _paragraph(content: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _rich_text(content)}}


def _heading(content: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"type": key, key: {"rich_text": _rich_text(content)}}


def _bulleted(content: str, children: Optional[list[dict]] = None) -> dict:
    block: dict = {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(content)},
    }
    if children:
        block["bulleted_list_item"]["children"] = children
    return block


def _todo(content: str, checked: bool = False) -> dict:
    return {
        "type": "to_do",
        "to_do": {"rich_text": _rich_text(content), "checked": checked},
    }


def _build_summary_blocks(notes: dict) -> list[dict]:
    blocks: list[dict] = []

    tldr = notes.get("tldr")
    if tldr:
        blocks.append(_heading("TL;DR"))
        blocks.append(_paragraph(tldr))

    action_items = notes.get("action_items") or []
    if action_items:
        blocks.append(_heading("Action Items"))
        for item in action_items:
            due = item.get("due")
            task = item.get("task", "")
            # task is already an owner-first sentence (per the prompt) that
            # usually works the deadline in — only append it separately if
            # it isn't already mentioned, to avoid duplication.
            if due and due.lower() not in task.lower():
                task = f"{task} (due: {due})"
            blocks.append(_todo(task))

    topics = notes.get("topics") or []
    for topic in topics:
        blocks.append(_heading(topic.get("heading", ""), level=3))
        for point in topic.get("points") or []:
            text, subpoints = _point_text_and_subpoints(point)
            children = [_bulleted(sp) for sp in subpoints] if subpoints else None
            blocks.append(_bulleted(text, children=children))

    decisions = notes.get("decisions") or []
    if decisions:
        blocks.append(_heading("Decisions"))
        for d in decisions:
            blocks.append(_bulleted(d))

    return blocks


def _transcript_toggle_blocks(transcript: str) -> list[dict]:
    chunks = [transcript[i : i + CHUNK_CHARS] for i in range(0, len(transcript), CHUNK_CHARS)] or [""]
    children = [_paragraph(c) for c in chunks]
    return [
        {
            "type": "toggle",
            "toggle": {"rich_text": _rich_text("Full transcript"), "children": children},
        }
    ]


def create_notion_page(
    notes: dict,
    transcript: str,
    parent_page_id: str,
    token: str,
    date_prefix: Optional[str] = None,
) -> dict:
    """Creates a page in Notion under parent_page_id (or at the top level of
    the user's Private section when it is "workspace"). Returns {url, page_id}.

    date_prefix (e.g. "2026-07-08"), if given, is prepended to the title —
    ISO order so pages sort chronologically in Notion's page list.
    """
    from notion_client import Client

    client = Client(auth=token)
    title = notes.get("title") or "Meeting notes"
    if date_prefix:
        title = f"{date_prefix} — {title}"

    summary_blocks = _build_summary_blocks(notes)
    first_batch, rest = summary_blocks[:BATCH_BLOCKS], summary_blocks[BATCH_BLOCKS:]

    # "workspace" = top level of the user's Private section (OAuth token only).
    if parent_page_id == WORKSPACE_PARENT:
        parent = {"type": "workspace", "workspace": True}
    else:
        parent = {"page_id": parent_page_id}

    page = client.pages.create(
        parent=parent,
        properties={"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        children=first_batch,
    )
    page_id = page["id"]

    # Append remaining summary blocks in further batches (100/request limit).
    while rest:
        batch, rest = rest[:BATCH_BLOCKS], rest[BATCH_BLOCKS:]
        client.blocks.children.append(block_id=page_id, children=batch)

    # Transcript as a collapsed toggle, appended separately (can be long).
    transcript_blocks = _transcript_toggle_blocks(transcript)
    client.blocks.children.append(block_id=page_id, children=transcript_blocks)

    return {"url": page.get("url"), "page_id": page_id}

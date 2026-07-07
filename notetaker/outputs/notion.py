"""Saves meeting notes as a child page in Notion.

Requires the Notion integration (token) to be shared with the parent page —
in Notion, open the parent page, click "..." → Connections → add our
integration, otherwise the API returns 404 (no access, not "doesn't exist").
"""

from __future__ import annotations

from typing import Optional

CHUNK_CHARS = 1900  # Notion's rich_text limit is 2000 chars; leave headroom
BATCH_BLOCKS = 90  # API limit is 100 blocks per request; leave headroom


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content[:CHUNK_CHARS]}}]


def _paragraph(content: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _rich_text(content)}}


def _heading(content: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"type": key, key: {"rich_text": _rich_text(content)}}


def _bulleted(content: str) -> dict:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(content)},
    }


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
            owner = item.get("owner")
            due = item.get("due")
            task = item.get("task", "")
            tail = " ".join(f"({v})" for v in (owner, due) if v)
            blocks.append(_todo(f"{task} {tail}".strip()))

    decisions = notes.get("decisions") or []
    if decisions:
        blocks.append(_heading("Decisions"))
        for d in decisions:
            blocks.append(_bulleted(d))

    topics = notes.get("topics") or []
    if topics:
        blocks.append(_heading("Topics"))
        for topic in topics:
            blocks.append(_heading(topic.get("heading", ""), level=3))
            for p in topic.get("points") or []:
                blocks.append(_bulleted(p))

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
) -> dict:
    """Creates a page in Notion under parent_page_id. Returns {url, page_id}."""
    from notion_client import Client

    client = Client(auth=token)
    title = notes.get("title") or "Meeting notes"

    summary_blocks = _build_summary_blocks(notes)
    first_batch, rest = summary_blocks[:BATCH_BLOCKS], summary_blocks[BATCH_BLOCKS:]

    page = client.pages.create(
        parent={"page_id": parent_page_id},
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

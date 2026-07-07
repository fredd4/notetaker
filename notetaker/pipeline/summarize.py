"""Generates structured meeting notes from a transcript (GPT).

Format modeled on Notion's AI meeting notes: concrete topics with factual
bullet points (not a meta-summary of "what the meeting was about"), plus
decisions and action items.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "You write meeting notes in the style of Notion's AI meeting notes: "
    "factual, concrete, focused on content and action items.\n\n"
    "LANGUAGE: write the notes in the same language the meeting was spoken "
    "in. If the transcript mixes languages or the language is unclear, "
    "default to English.\n\n"
    "KEY RULE: do not write a META-summary like 'budget was discussed' or "
    "'the conversation covered X'. Instead, state the actual concrete "
    "information that was said: numbers, names, decisions, arguments, "
    "deadlines. The reader should learn WHAT was actually said, not that "
    "'there was a topic about it'.\n\n"
    "Bad note: 'The project budget was discussed.'\n"
    "Good note: 'The project budget is $50,000, of which $20,000 has already "
    "been spent on licenses.'\n\n"
    "Detailed rules:\n"
    "- Split the content into topics in the order they came up. Each topic: "
    "a short heading (2-5 words) plus a list of concrete points under it.\n"
    "- Decisions: only actual resolutions ('it was decided that...'), not "
    "guesses.\n"
    "- Action items: concrete, action-verb phrasing, with an owner and "
    "deadline if mentioned in the conversation. Don't invent an owner or "
    "deadline if none was given — leave it null.\n"
    "- Skip small talk, digressions, repetition.\n"
    "- If a category has no content in the transcript, return an empty list "
    "instead of making something up."
)

SCHEMA = {
    "name": "meeting_notes",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short meeting title (3-8 words)"},
            "tldr": {
                "type": "string",
                "description": (
                    "1-2 sentences with the single most important concrete "
                    "outcome of the meeting (not a meta-description of the "
                    "topic, just the key fact/decision)"
                ),
            },
            "topics": {
                "type": "array",
                "description": "Topics discussed, in order, with concrete points",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Concrete information, facts, numbers, decisions — not generalities",
                        },
                    },
                    "required": ["heading", "points"],
                    "additionalProperties": False,
                },
            },
            "decisions": {"type": "array", "items": {"type": "string"}},
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": ["string", "null"], "description": "Responsible person, if mentioned"},
                        "task": {"type": "string"},
                        "due": {"type": ["string", "null"], "description": "Deadline, if mentioned"},
                    },
                    "required": ["owner", "task", "due"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "tldr", "topics", "decisions", "action_items"],
        "additionalProperties": False,
    },
}

EventCallback = Callable[[str, dict], None]


def summarize(
    transcript: str,
    api_key: str,
    model: str = MODEL,
    on_event: Optional[EventCallback] = None,
) -> dict:
    from openai import OpenAI

    if on_event:
        on_event("summarize_start", {})

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Meeting transcript:\n\n{transcript}"},
        ],
        response_format={"type": "json_schema", "json_schema": SCHEMA},
    )
    data = json.loads(resp.choices[0].message.content)

    if on_event:
        on_event("summarize_done", {})
    return data

"""Generates structured meeting notes from a transcript (GPT).

Format modeled on Notion's built-in AI Meeting Notes: a specific headline
title, action items up front (owner-first full sentences), then several
narrow topic sections with concrete, numbers-and-names bullets (never a
meta-summary of "what was discussed"), each bullet carrying exactly one
bolded key fact, with nested sub-bullets for parallel named items, plus a
short separate list of firm decisions.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

# Model tiers. DEFAULT_MODEL is what summarize() uses unless the caller
# passes model= explicitly or the NOTETAKER_SUMMARY_MODEL env var is set
# (config.py has no generic settings getter, so the env var is the only
# non-code override point).
#
# BASELINE_MODEL is kept only as a reference point for the old behavior
# (see notetaker/pipeline/summarize.py history / eval scripts) — it is not
# used anywhere at runtime.
BASELINE_MODEL = "gpt-4o-mini"
MID_MODEL = "gpt-5.4-mini"
FLAGSHIP_MODEL = "gpt-5.5"

DEFAULT_MODEL = FLAGSHIP_MODEL

# Backwards-compat alias — some older code/tests may still import MODEL.
MODEL = DEFAULT_MODEL

SYSTEM_PROMPT = (
    "LANGUAGE — READ THIS FIRST: The notes MUST be in the language "
    "predominantly spoken in the transcript — e.g. a Polish conversation "
    "gets Polish notes even if it contains English technical terms or "
    "product/tool names. Only use English when the transcript is mostly "
    "English or truly mixed 50/50. Do not default to English just because "
    "the transcript contains English jargon. Role labels and every other "
    "word of the notes must also be in that language (e.g. 'Właściciel', "
    "not 'Owner', in Polish notes) — only product/tool names stay as-is.\n\n"
    "You write meeting notes in the style of Notion's built-in AI Meeting "
    "Notes: a specific headline, action items up front, then several "
    "narrow topic sections packed with concrete facts. The reader missed "
    "the meeting and should walk away knowing exactly what was said — "
    "numbers, names, decisions — not that 'a topic was discussed'.\n\n"
    "KEY RULE — NO META-SUMMARY: never write sentences like 'the budget "
    "was discussed' or 'the conversation covered X' or 'issues with Y were "
    "highlighted'. State the actual concrete information instead.\n"
    "Bad: 'The paywall performance was discussed.'\n"
    "Good: 'The paywall's viewed rate was **zero** for the new onboarding "
    "flow, meaning nobody who reached it actually saw the paywall screen.'\n"
    "Bad: 'There was a discussion about creative rejections.'\n"
    "Good: 'Marek reported **4 purchases** from the Nadia campaign this "
    "week, up from zero the week before.'\n\n"
    "TITLE: max 8-10 words, at most ONE ' — ' separator (the app prepends "
    "the date with its own dash). Pattern: '<Project/product> — <specific "
    "focus>' or '<Project/product> — <specific focus> (<meeting role>)'. "
    "E.g. 'Mirillis Action — Strategia AI i strona' or 'Checkout Redesign "
    "— Sprint Kickoff'. Never a generic title like 'Team Meeting' or "
    "'Product Update', and never a second dash or a second clause.\n\n"
    "TOPICS: split the meeting into narrow topic sections, in the order "
    "they came up — aim for 5-7 when the meeting has enough distinct "
    "content, but use fewer for a short or narrowly-scoped meeting. A "
    "narrow topic names the specific thing being discussed — 'Paywall A/B "
    "Test Results', 'Age Gate / 18+ Gateway' — never a broad umbrella like "
    "'Product Updates' or 'General Discussion'. Each topic gets 3-8 "
    "points. NEVER split one real subject into multiple overlapping "
    "topics just to hit a target count, and never repeat the same fact "
    "under a second heading — every topic must cover genuinely distinct "
    "ground. A short meeting with only two real subjects should produce "
    "exactly two topics, not padding.\n\n"
    "POINTS: each point is 1-2 complete sentences — not a clipped "
    "fragment, not a paragraph. Bold AT MOST one key span per point, "
    "written as **bold** markdown, and only when the point contains a "
    "genuinely key figure, name, date, or decision word (a metric, a "
    "count, a person's name tied to a specific finding, a hard deadline, "
    "a decision). A point that states no such fact gets no bold at all — "
    "do not force bold onto an arbitrary word just to satisfy this rule, "
    "and never bold whole sentences or more than one span. If a point "
    "lists 2 or more parallel named items (e.g. several tools, variants, "
    "or campaigns), prefer putting the shared context in the point's text "
    "and each named item as its own short subpoint, rather than listing "
    "them all inline.\n\n"
    "COMMITMENTS BELONG IN ACTION ITEMS ONLY: if something is a to-do or "
    "next step someone will do after the meeting, it goes ONLY in "
    "action_items — never restate it as a topic point too. A topic point "
    "may describe the decision or context behind it, but not the to-do "
    "itself.\n\n"
    "OWNERS: if the transcript does not name speakers, use short, "
    "consistent role labels for owners (e.g. 'Marketing Partner', 'App "
    "Owner') instead of names. Never invent a name that isn't in the "
    "transcript.\n\n"
    "DECISIONS: when a topic reaches a firm resolution ('it was decided "
    "that...', 'we're going with...'), state it inline as a point in that "
    "topic, optionally prefixed with the word for 'Decision' in the notes' language (e.g. 'Decision: ' in English, 'Decyzja: ' in Polish). Separately, the top-level "
    "decisions list should contain ONLY the firm resolutions of the whole "
    "meeting, each restated briefly in different wording than the inline "
    "point (don't copy the same sentence twice) — if there were no firm "
    "decisions, return an empty list. Do not put guesses, proposals, or "
    "open questions in decisions.\n\n"
    "ACTION ITEMS: each one is a full, owner-first sentence naming who "
    "does what, e.g. 'Ville to check with the creative team about the new "
    "video specs.' — that English phrasing is an EXAMPLE ONLY; in another "
    "language use the natural native construction for stating who has to "
    "do what, never a literal transplant of English grammar. Polish "
    "example: 'Partner marketingowy ma wysłać listę dostępów na WhatsAppie "
    "do jutra.' (or 'Partner marketingowy: wysłać listę…'). Work the "
    "deadline into the sentence itself when one was given, e.g. '...by "
    "Friday.' Also fill the separate owner/due fields when known. Don't "
    "invent an owner or deadline that wasn't mentioned — leave the field "
    "null and just phrase the task sentence naturally without a fake "
    "deadline.\n\n"
    "OTHER RULES:\n"
    "- Skip small talk, digressions, and repeated restatements of the same "
    "fact.\n"
    "- Never invent a fact, number, or name that isn't in the transcript.\n"
    "- If a category has no content, return an empty list rather than "
    "inventing something.\n"
    "- The tldr is 1-2 sentences with the single most important concrete "
    "outcome — not a description of the meeting's topic."
)

SCHEMA = {
    "name": "meeting_notes",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Max 8-10 words, at most one ' — ' separator: "
                    "'<Project/product> — <specific focus>' or "
                    "'<Project/product> — <specific focus> (<meeting role>)'"
                ),
            },
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
                "minItems": 1,
                "maxItems": 9,
                "description": (
                    "Narrow topic sections, in the order they came up — 5-7 "
                    "when the meeting has enough distinct content, fewer for "
                    "a short/narrow meeting, each with 3-8 concrete points. "
                    "Never split one subject into overlapping topics just to "
                    "hit a count."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {
                            "type": "string",
                            "description": "Short, specific topic name (2-6 words), not a broad umbrella",
                        },
                        "points": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {
                                        "type": "string",
                                        "description": (
                                            "1-2 complete sentences. Bold **at most "
                                            "one** span, and only if the sentence "
                                            "contains a genuinely key figure, name, "
                                            "date, or decision word; no bold at all "
                                            "if there isn't one. Never restate a "
                                            "to-do that belongs in action_items."
                                        ),
                                    },
                                    "subpoints": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "Use when this point lists 2+ parallel "
                                            "named items (e.g. several tools, "
                                            "variants, campaigns) — one item per "
                                            "subpoint; empty list if none"
                                        ),
                                    },
                                },
                                "required": ["text", "subpoints"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["heading", "points"],
                    "additionalProperties": False,
                },
            },
            "decisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short list of ONLY the firm resolutions of the whole "
                    "meeting, each in different wording than its inline "
                    "mention in topics; empty list if none"
                ),
            },
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": ["string", "null"], "description": "Responsible person, if mentioned"},
                        "task": {
                            "type": "string",
                            "description": (
                                "Full owner-first sentence, e.g. 'Ville to check "
                                "with the creative team about the new video "
                                "specs by Friday.' Include the deadline in the "
                                "sentence when one was given."
                            ),
                        },
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


def _post_check(data: dict, on_event: Optional[EventCallback]) -> None:
    """Soft validation: logs a warning event if the model drifted from the
    shape we asked for, without failing the whole summarization run.

    (Structured Outputs strict mode did accept minItems/maxItems on arrays
    for every model tested at write time, so this is a belt-and-suspenders
    check rather than a required fallback — kept in case a future model or
    SDK version silently drops those constraints.)
    """
    warnings = []
    topics = data.get("topics") or []
    if not (1 <= len(topics) <= 9):
        warnings.append(f"topics count out of expected range: {len(topics)}")
    for t in topics:
        n = len(t.get("points") or [])
        if not (1 <= n <= 8):
            warnings.append(f"topic {t.get('heading')!r} has {n} points")
    if warnings and on_event:
        on_event("summarize_warning", {"warnings": warnings})


def summarize(
    transcript: str,
    api_key: str,
    model: Optional[str] = None,
    on_event: Optional[EventCallback] = None,
) -> dict:
    from openai import OpenAI

    resolved_model = model or os.environ.get("NOTETAKER_SUMMARY_MODEL") or DEFAULT_MODEL

    if on_event:
        on_event("summarize_start", {})

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Meeting transcript:\n\n{transcript}\n\n"
                    "Reminder: write the notes in the predominant language "
                    "of the transcript above."
                ),
            },
        ],
        response_format={"type": "json_schema", "json_schema": SCHEMA},
    )
    data = json.loads(resp.choices[0].message.content)
    _post_check(data, on_event)

    if on_event:
        on_event("summarize_done", {})
    return data

"""Shared rendering for AskUserQuestion multi-question prompts.

The hook module fires the FIRST question of a multi-question dialog
when Claude Code's ``Notification`` event arrives. The bot module
fires the SECOND through Nth as the user answers each one, since
Claude only emits one Notification at the start of the tool call.
Both code paths render identically — same HTML body, same keyboard
shape — so the rendering lives here in a small dependency-free
module that both can import.
"""

from __future__ import annotations

import html
from typing import Any

# Maximum option count per single Telegram inline keyboard. Anything
# beyond this is unlikely from real AskUserQuestion calls and would
# fight Telegram's narrow on-mobile keyboard rendering anyway.
_MAX_OPTIONS = 8


def _esc(text: str, limit: int | None = None) -> str:
    out = html.escape(text, quote=False)
    if limit is not None and len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out


def render_question_html(question: dict[str, Any], idx: int, total: int) -> str:
    """Return Telegram-HTML body for ``question[idx]`` of ``total``.

    Adds a ``(idx+1/total) ❓`` prefix when this is part of a multi-
    question chain so the user sees their progress through the dialog
    at a glance. Multi-select questions get a ``(select all that
    apply)`` hint.
    """
    q_text = str(question.get("question") or "").strip()
    is_multi = bool(question.get("multiSelect"))
    options = question.get("options") or []

    progress = f"({idx + 1}/{total}) " if total > 1 else ""
    multi_hint = " <i>(select all that apply)</i>" if is_multi else ""
    header = (
        f"{progress}❓ <b>{_esc(q_text)}</b>{multi_hint}"
        if q_text
        else f"{progress}❓ <b>Question needs an answer</b>{multi_hint}"
    )
    lines: list[str] = [header]

    if isinstance(options, list):
        for i, opt in enumerate(options, start=1):
            if isinstance(opt, dict):
                label = str(opt.get("label") or f"Option {i}")
                desc = str(opt.get("description") or "").strip()
            elif isinstance(opt, str):
                label = opt
                desc = ""
            else:
                continue
            line = f"<b>{i}. {_esc(label, 120)}</b>"
            if desc:
                line += f"\n    <i>{_esc(desc, 200)}</i>"
            lines.append(line)

    return "\n\n".join(lines)


def question_keyboard_rows(
    pane_id: str, question: dict[str, Any]
) -> list[list[dict[str, Any]]]:
    """Return inline_keyboard rows (raw dicts) for a single question.

    Single-select → ``1. <label>`` / ``2. <label>`` etc. with
    ``ans:%pane:N`` callbacks (one tap submits that choice).

    Multi-select → ``☐ 1`` / ``☐ 2`` toggle buttons with ``mtg:`` plus
    a ``✅ Submit`` button with ``msub:``.

    Returns an empty list if there are no usable options. Caller wraps
    in whatever shape its Telegram API expects (raw dict for the hook
    module's ``_call``; ``InlineKeyboardMarkup`` for the bot module's
    python-telegram-bot client).
    """
    options = question.get("options") or []
    if not isinstance(options, list) or not options:
        return []
    n = min(len(options), _MAX_OPTIONS)
    is_multi = bool(question.get("multiSelect"))

    if is_multi:
        rows: list[list[dict[str, Any]]] = []
        for i in range(1, n + 1):
            rows.append(
                [
                    {
                        "text": f"☐ {i}",
                        "callback_data": f"mtg:{pane_id}:{i}:0",
                    }
                ]
            )
        rows.append([{"text": "✅ Submit", "callback_data": f"msub:{pane_id}"}])
        return rows

    rows = []
    for i, opt in enumerate(options[:n], start=1):
        label = ""
        if isinstance(opt, dict):
            label = str(opt.get("label") or "")
        elif isinstance(opt, str):
            label = opt
        if not label:
            label = f"Option {i}"
        if len(label) > 40:
            label = label[:37] + "…"
        rows.append(
            [
                {
                    "text": f"{i}. {label}",
                    "callback_data": f"ans:{pane_id}:{i}",
                }
            ]
        )
    return rows

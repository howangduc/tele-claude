"""Claude Code hook handlers for tele-claude.

Three modes, invoked as `python3 tele_claude_hooks.py <mode>`:

  notify   — Notification hook. Sends a Telegram message when Claude
             needs attention. For permission_prompt notifications,
             adds inline keyboard buttons [1 Allow] [2 Always] [3 Deny]
             that send the corresponding digit to the pane.

  reply    — Stop hook. Reads the last assistant message from the
             transcript JSONL, converts markdown→Telegram HTML,
             dedup-checks against recent identical replies, splits
             long messages at paragraph/fence boundaries, attaches
             inline buttons (URL open + quick replies), and either
             edits an existing ⏳ progress message or sends a new one.

  progress — UserPromptSubmit hook. Sends an ⏳ placeholder message
             containing a preview of the submitted prompt, and records
             the message_id so the reply hook can edit it in place.

The bash hook scripts are thin wrappers that exec this module. All
logic lives here so we can unit-test it and avoid curl+jq JSON
acrobatics for inline keyboards.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import tele_claude_format
import tele_claude_state as state


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = (
    4000  # Telegram's hard limit is 4096; leave room for header + HTML margin
)

# Markdown → HTML can inflate text by 20-50% (adding <b>, <code>, <pre> tags).
# Start the split at this budget and iteratively shrink if conversion overshoots.
_RAW_SPLIT_BUDGET = 2500
_MIN_RAW_SPLIT = 800

# Idle notifications get suppressed unless this many seconds have passed since
# the session's last real activity (UserPromptSubmit or Stop). Claude Code's
# own idle_prompt fires at 60s (hardcoded upstream; see anthropics/claude-code#13922).
_IDLE_SUPPRESS_SECONDS = float(os.environ.get("TELE_CLAUDE_IDLE_MIN_SECONDS", "900"))

# Typing-indicator pumper: sendChatAction lasts 5 s per call, so the pumper
# re-sends every _TYPING_PUMP_INTERVAL seconds while a turn is active.
# Capped at _TYPING_PUMP_MAX_SECONDS so an orphaned progress file (Stop hook
# failed to clear) doesn't leave the pumper running forever.
_TYPING_PUMP_INTERVAL = 4.0
_TYPING_PUMP_MAX_SECONDS = 600.0


# ---------- HTTP ----------


def _token() -> str:
    token = os.environ.get("CLAUDE_TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(0)
    return token


def _chat_ids() -> list[str]:
    raw = os.environ.get("CLAUDE_TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _call(method: str, data: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, str] = {}
    for k, v in data.items():
        if v is None:
            continue
        body[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    url = TELEGRAM_API.format(token=_token(), method=method)
    req = Request(url, data=urlencode(body).encode())
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"ok": False}


def send_message(
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
    disable_notification: bool = False,
) -> int | None:
    """Send a Telegram message.

    When ``disable_notification=True`` the message lands silently —
    still visible in the chat, but no push notification, no sound,
    no badge increment. Used for ⏳ placeholders and secondary chunks
    of a split reply so the user only gets ONE phone buzz per turn
    (when the actual response arrives).
    """
    resp = _call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "disable_notification": disable_notification or None,
        },
    )
    if resp.get("ok"):
        return int(resp["result"]["message_id"])
    return None


def edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    resp = _call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        },
    )
    return bool(resp.get("ok"))


def delete_message(chat_id: str, message_id: int) -> bool:
    """Delete a previously-sent message. Used to remove the ⏳ placeholder
    before sending the real reply — that way the real reply is a fresh
    send (which pushes a notification) rather than a silent edit.
    """
    resp = _call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    return bool(resp.get("ok"))


# ---------- Shared helpers ----------


def _stdin_json() -> dict[str, Any]:
    try:
        return json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return {}


def _project(cwd: str) -> str:
    return os.path.basename(cwd.rstrip("/")) if cwd else ""


def _build_header(cwd: str, pane_id: str, emoji: str) -> str:
    parts = [emoji]
    project = _project(cwd)
    if project:
        parts.append(f"<code>{html.escape(project)}</code>")
    if pane_id:
        parts.append(f"<code>{html.escape(pane_id)}</code>")
    return " · ".join(parts)


# ---------- Smart split ----------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?.*?```", re.DOTALL)


def _tokenize(md: str) -> list[tuple[str, str]]:
    """Split markdown into (kind, text) tokens; fences are atomic."""
    tokens: list[tuple[str, str]] = []
    cursor = 0
    for match in _FENCE_RE.finditer(md):
        if match.start() > cursor:
            tokens.append(("text", md[cursor : match.start()]))
        tokens.append(("fence", match.group(0)))
        cursor = match.end()
    if cursor < len(md):
        tokens.append(("text", md[cursor:]))
    return tokens


def _chunk_for_telegram(raw_md: str, html_budget: int) -> list[str]:
    """Split raw markdown so that each chunk's HTML form fits within html_budget.

    Markdown → HTML inflates by 20-50%, which is tough to predict without
    doing the conversion. We start with a generous raw budget, convert, and
    shrink iteratively until no chunk exceeds the HTML budget.
    """
    budget = _RAW_SPLIT_BUDGET
    while True:
        chunks = _split_markdown(raw_md, budget)
        worst = max((len(tele_claude_format.convert(c)) for c in chunks), default=0)
        if worst <= html_budget or budget <= _MIN_RAW_SPLIT:
            return chunks
        budget = max(_MIN_RAW_SPLIT, int(budget * 0.75))


def _split_markdown(md: str, max_len: int) -> list[str]:
    """Chunk markdown at paragraph boundaries, never inside fenced blocks."""
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current.rstrip())
            current = ""

    def append_atom(atom: str) -> None:
        nonlocal current
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(atom) > max_len and current:
            flush()
        current += sep + atom if current else atom

    for kind, text in _tokenize(md):
        if kind == "fence":
            if len(text) > max_len:
                flush()
                # Fence alone is bigger than max — hard-split on newlines.
                buf = ""
                for line in text.split("\n"):
                    line_with_nl = line + "\n"
                    if len(buf) + len(line_with_nl) > max_len:
                        chunks.append(buf.rstrip())
                        buf = ""
                    buf += line_with_nl
                if buf:
                    chunks.append(buf.rstrip())
            else:
                append_atom(text)
        else:
            for para in text.split("\n\n"):
                if not para.strip():
                    continue
                if len(para) > max_len:
                    flush()
                    while len(para) > max_len:
                        chunks.append(para[:max_len])
                        para = para[max_len:]
                    if para:
                        current = para
                else:
                    append_atom(para)
    flush()
    return chunks


# ---------- URL + quick-reply keyboards ----------

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _url_label(url: str) -> str:
    try:
        parsed = urlparse(url)
        tail = parsed.path.rstrip("/").split("/")[-1]
        return f"Open {tail or parsed.netloc}"[:40]
    except Exception:
        return ("Open " + url)[:40]


def _url_buttons(text: str, max_buttons: int = 4) -> list[list[dict[str, Any]]]:
    seen: list[str] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?)]")
        if url and url not in seen:
            seen.append(url)
        if len(seen) >= max_buttons:
            break
    return [[{"text": f"🔗 {_url_label(u)}", "url": u}] for u in seen]


def _quick_reply_keyboard(pane_id: str) -> list[list[dict[str, Any]]]:
    """Quick-reply buttons are currently disabled — just type into the chat
    to send to the active pane. Flip this to return a populated list to
    enable them again."""
    _ = pane_id
    return []


# ---------- Transcript ----------


def _wait_for_stable_text(
    transcript_path: Path,
    max_wait_seconds: float = 1.5,
    poll_interval_seconds: float = 0.3,
) -> str:
    """Read assistant text, re-read until two consecutive reads agree.

    Claude Code's transcript writer is buffered — the Stop hook often
    fires milliseconds before the final text block has been flushed to
    disk, so a naive read returns partial content. We poll until two
    reads in a row return the same text (stable), or we hit max_wait.
    Typical cost: one extra 300ms sleep; worst case max_wait_seconds.
    """
    prev = _last_assistant_text(transcript_path)
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        current = _last_assistant_text(transcript_path)
        if current == prev and current:
            return current
        prev = current
    return prev


def _find_pending_context(
    transcript_path: Path,
) -> tuple[dict[str, Any] | None, str]:
    """Return (most-recent tool_use, text written just before it).

    The "preamble" is the assistant's free-form narration between the
    previous boundary (tool_use or real user prompt) and the current
    tool_use — exactly what the user would see on-pane just above the
    permission dialog. Knowing that context is crucial when approving
    `AskUserQuestion` or `ExitPlanMode` from the phone.

    Tool_result entries (user role, content type=tool_result) don't
    reset the accumulator because they're part of the same assistant
    turn. Only a real user prompt (role=user with a text block or
    string content) clears everything.
    """
    current_texts: list[str] = []
    last_tool: dict[str, Any] | None = None
    last_context: str = ""
    try:
        with transcript_path.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role")
                blocks = msg.get("content") or []
                if role == "user":
                    is_real_prompt = any(
                        isinstance(b, dict) and b.get("type") == "text" for b in blocks
                    ) or isinstance(msg.get("content"), str)
                    if is_real_prompt:
                        current_texts = []
                        last_tool = None
                        last_context = ""
                    continue
                if role != "assistant":
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text") or ""
                        if text:
                            current_texts.append(text)
                    elif btype == "tool_use":
                        last_tool = block
                        last_context = "\n\n".join(current_texts).strip()
                        current_texts = []  # reset for any following tool_use
    except OSError:
        return None, ""
    return last_tool, last_context


def _find_last_tool_use(transcript_path: Path) -> dict[str, Any] | None:
    """Back-compat shim — most callers want the tuple variant now."""
    tool, _ = _find_pending_context(transcript_path)
    return tool


def _describe_tool_use(tool: dict[str, Any]) -> str | None:
    """Render a tool_use block as a Telegram HTML snippet.

    Used by the Notification hook to tell you exactly what Claude wants
    approval for. Known tools get bespoke renderers (Bash → command,
    Edit → file + diff size, ExitPlanMode → plan body, AskUserQuestion
    → question text); unknowns fall back to a shape hint.
    """
    name = str(tool.get("name") or "?")
    inp = tool.get("input") or {}
    if not isinstance(inp, dict):
        return f"<b>{html.escape(name)}</b>"

    def esc(s: Any, limit: int = 400) -> str:
        text = str(s)
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return html.escape(text, quote=False)

    if name == "Bash":
        cmd = str(inp.get("command") or "").strip()
        desc = str(inp.get("description") or "").strip()
        if cmd:
            body = f"<pre>$ {esc(cmd, 800)}</pre>"
            if desc:
                body += f"\n<i>{esc(desc, 200)}</i>"
            return body
        return "<b>Bash</b>"
    if name == "ExitPlanMode":
        # Plans can get big; Telegram caps messages at 4096 chars AFTER
        # HTML conversion (which inflates by ~30 %). A single-message
        # budget of ~3000 raw chars fits comfortably even with the
        # outer framing (header, blockquote preamble, buttons). Longer
        # plans go into a <blockquote expandable> which is collapsible
        # on recent Telegram clients — still limited to 4096 total, but
        # the visual footprint is compact so the buttons aren't scrolled
        # off-screen on mobile.
        plan = str(inp.get("plan") or "").strip()
        if not plan:
            return "📋 <b>Plan approval requested</b>"
        truncated = False
        if len(plan) > 3000:
            plan = (
                plan[:3000].rstrip()
                + "\n\n… _(plan truncated at 3000 chars — open pane to see full)_"
            )
            truncated = True
        rendered = tele_claude_format.convert(plan)
        # Expandable blockquote keeps long plans tidy and lets the user
        # tap to expand. On older clients it gracefully degrades to a
        # regular blockquote.
        wrapper = (
            f"<blockquote expandable>{rendered}</blockquote>"
            if len(rendered) > 400
            else rendered
        )
        header = "📋 <b>Plan to execute</b>"
        if truncated:
            header += " <i>(truncated)</i>"
        return f"{header}\n{wrapper}"
    if name == "AskUserQuestion":
        questions = inp.get("questions") or []
        if isinstance(questions, list) and questions:
            first = questions[0] if isinstance(questions[0], dict) else {}
            q_text = str(first.get("question") or "").strip()
            multi = first.get("multiSelect")
            options = first.get("options") or []
            suffix = " <i>(select all that apply)</i>" if multi else ""
            lines: list[str] = []
            if q_text:
                lines.append(f"❓ <b>{esc(q_text)}</b>{suffix}")
            else:
                lines.append(f"❓ <b>Question needs an answer</b>{suffix}")
            # Render each option with its description so the user can
            # pick intelligently — the inline-keyboard buttons carry
            # only the label + number.
            if isinstance(options, list):
                for idx, opt in enumerate(options, start=1):
                    if isinstance(opt, dict):
                        label = str(opt.get("label") or f"Option {idx}")
                        desc = str(opt.get("description") or "").strip()
                    elif isinstance(opt, str):
                        label = opt
                        desc = ""
                    else:
                        continue
                    line = f"<b>{idx}. {esc(label, 120)}</b>"
                    if desc:
                        line += f"\n    <i>{esc(desc, 200)}</i>"
                    lines.append(line)
            return "\n\n".join(lines)
        return "❓ <b>Question needs an answer</b>"
    if name == "Write":
        path = inp.get("file_path") or "?"
        content = str(inp.get("content") or "")
        lines = content.count("\n") + (1 if content else 0)
        return f"📝 <b>Write</b> <code>{esc(path)}</code> ({lines} lines)"
    if name == "Edit":
        path = inp.get("file_path") or "?"
        old = str(inp.get("old_string") or "").splitlines()
        new = str(inp.get("new_string") or "").splitlines()
        return (
            f"✏️ <b>Edit</b> <code>{esc(path)}</code> (−{len(old)} / +{len(new)} lines)"
        )
    if name == "Read":
        path = inp.get("file_path") or "?"
        return f"📖 <b>Read</b> <code>{esc(path)}</code>"
    if name == "Glob":
        pattern = inp.get("pattern") or "?"
        return f"🔍 <b>Glob</b> <code>{esc(pattern)}</code>"
    if name == "Grep":
        pattern = inp.get("pattern") or "?"
        path = inp.get("path") or ""
        suffix = f" in <code>{esc(path)}</code>" if path else ""
        return f"🔍 <b>Grep</b> <code>{esc(pattern)}</code>{suffix}"
    if name == "Task":
        subagent = inp.get("subagent_type") or "?"
        desc = str(inp.get("description") or "").strip()
        return f"🧑‍💻 <b>Task</b> agent=<code>{esc(subagent)}</code>" + (
            f"\n<i>{esc(desc, 200)}</i>" if desc else ""
        )
    if name == "WebFetch":
        url = inp.get("url") or "?"
        return f"🌐 <b>WebFetch</b> <code>{esc(url)}</code>"

    # Unknown tool — show name + first few input keys as shape hint.
    keys = ", ".join(list(inp.keys())[:3])
    return f"🛠 <b>{esc(name)}</b>({esc(keys, 120)})"


def _build_permission_keyboard(
    pane_id: str, tool: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Pick the right inline keyboard for a pending permission.

    AskUserQuestion gets one button per declared option (1..N) so
    tapping sends the matching digit that Claude's TUI expects.
    ExitPlanMode uses 2 buttons (Approve / Keep planning). Everything
    else falls back to Allow once / Always / Deny.
    """
    if not pane_id:
        return None

    if tool and tool.get("name") == "AskUserQuestion":
        inp = tool.get("input") or {}
        questions = inp.get("questions") if isinstance(inp, dict) else None
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            options = questions[0].get("options")
            if isinstance(options, list) and options:
                rows: list[list[dict[str, Any]]] = []
                for idx, opt in enumerate(options[:8], start=1):
                    label = ""
                    if isinstance(opt, dict):
                        label = str(opt.get("label") or "")
                    elif isinstance(opt, str):
                        label = opt
                    if not label:
                        label = f"Option {idx}"
                    if len(label) > 40:
                        label = label[:37] + "…"
                    rows.append(
                        [
                            {
                                "text": f"{idx}. {label}",
                                "callback_data": f"ans:{pane_id}:{idx}",
                            }
                        ]
                    )
                return {"inline_keyboard": rows}

    if tool and tool.get("name") == "ExitPlanMode":
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Approve plan",
                        "callback_data": f"ans:{pane_id}:1",
                    },
                    {
                        "text": "📝 Keep planning",
                        "callback_data": f"ans:{pane_id}:2",
                    },
                ]
            ]
        }

    return {
        "inline_keyboard": [
            [
                {"text": "1 · Allow once", "callback_data": f"ans:{pane_id}:1"},
                {"text": "2 · Always", "callback_data": f"ans:{pane_id}:2"},
                {"text": "3 · Deny", "callback_data": f"ans:{pane_id}:3"},
            ]
        ]
    }


def _last_assistant_text(transcript_path: Path) -> str:
    """Return all assistant text from the most recent turn.

    Claude Code writes each content block (thinking / text / tool_use) as
    its own transcript entry. A single turn spans many such entries.
    Tool-result entries from Claude's tool calls are stored with role=user
    but type=tool_result — they are PART of the current assistant turn,
    not a new user prompt, so we must not reset on them. We only reset on
    real user prompts (role=user with type=text blocks).
    """
    texts: list[str] = []
    try:
        with transcript_path.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role")
                blocks = msg.get("content") or []
                if role == "user":
                    is_real_prompt = any(
                        isinstance(b, dict) and b.get("type") == "text" for b in blocks
                    ) or (isinstance(msg.get("content"), str))
                    if is_real_prompt:
                        texts.clear()
                    continue
                if role != "assistant":
                    continue
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text") or ""
                        if text:
                            texts.append(text)
    except OSError:
        return ""
    return "\n\n".join(texts)


# ---------- Mode: reply ----------


def _clear_heartbeat_if_session(session_id: str) -> None:
    """Drop the throttle marker so the next turn's heartbeat fires immediately."""
    state.clear_heartbeat(session_id)


def main_reply() -> None:
    data = _stdin_json()
    transcript_raw = data.get("transcript_path")
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    pane_id = os.environ.get("TMUX_PANE", "")

    state.touch_activity(session_id)

    if not transcript_raw:
        return
    transcript_path = Path(str(transcript_raw))
    if not transcript_path.exists():
        return
    # Subscription gate — hooks only forward from panes the user has
    # interacted with via the bot. Panes with no TMUX_PANE at all are
    # allowed through (best-effort degradation for edge cases).
    if pane_id and not state.is_subscribed(pane_id):
        return
    if pane_id and state.is_muted(pane_id):
        return

    raw_md = _wait_for_stable_text(transcript_path)
    if not raw_md:
        return

    # Dedup: skip if the same body was sent within the TTL.
    if state.check_and_set_fingerprint(session_id, raw_md):
        return

    header = _build_header(cwd, pane_id, "🤖")
    header_overhead = len(header) + 20  # "\n\n" + (i/n) prefix margin

    chunks_md = _chunk_for_telegram(raw_md, MAX_MESSAGE_LEN - header_overhead)
    total = len(chunks_md)

    url_markup = _url_buttons(raw_md)
    quick_markup = _quick_reply_keyboard(pane_id)
    last_markup = (
        {"inline_keyboard": url_markup + quick_markup}
        if (url_markup or quick_markup)
        else None
    )

    for chat_id in _chat_ids():
        progress_key = f"{session_id}:{chat_id}"
        progress_id = state.get_progress_msg_id(progress_key)

        # Delete the ⏳ placeholder (if any) so the real reply arrives as
        # a fresh sendMessage — which triggers a push notification.
        # Editing the placeholder in place was silent (Telegram doesn't
        # push on edits), meaning users missed responses on their phone.
        if progress_id is not None:
            _ = delete_message(chat_id, progress_id)

        for idx, piece_md in enumerate(chunks_md):
            piece_html = tele_claude_format.convert(piece_md)
            prefix = f"({idx + 1}/{total}) " if total > 1 else ""
            body = f"{prefix}{header}\n\n{piece_html}"
            is_last = idx == total - 1
            markup = last_markup if is_last else None
            # First chunk pushes the notification (the user's signal that
            # the turn completed). Subsequent chunks are silent so a
            # multi-part reply only buzzes the phone once.
            silent = idx > 0
            send_message(
                chat_id,
                body,
                parse_mode="HTML",
                reply_markup=markup,
                disable_notification=silent,
            )

        state.clear_progress(progress_key)
    # Turn done — reset the heartbeat throttle for the next turn.
    _clear_heartbeat_if_session(session_id)


# ---------- Mode: notify ----------


def main_notify() -> None:
    data = _stdin_json()
    notif_type = str(data.get("notification_type") or "unknown")
    msg_text = str(data.get("message") or "").strip()
    transcript_raw = data.get("transcript_path")
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    pane_id = os.environ.get("TMUX_PANE", "")

    if pane_id and not state.is_subscribed(pane_id):
        return
    if pane_id and state.is_muted(pane_id):
        return

    # Suppress idle_prompt if the session has had recent activity. Claude Code
    # currently fires idle_prompt at a hardcoded 60s; this gate lets us behave
    # as if the threshold is TELE_CLAUDE_IDLE_MIN_SECONDS (default 15 min).
    if notif_type == "idle_prompt":
        since = state.seconds_since_activity(session_id)
        if since is not None and since < _IDLE_SUPPRESS_SECONDS:
            return

    labels: dict[str, tuple[str, str]] = {
        "permission_prompt": ("🔐", "Permission needed"),
        "idle_prompt": ("💤", "Waiting for input"),
    }
    emoji, label = labels.get(notif_type, ("🔔", "Notification"))

    # Header: emoji + label + project + pane on one line.
    header_parts = [f"{emoji} <b>{html.escape(label)}</b>"]
    project = _project(cwd)
    if project:
        header_parts.append(f"<code>{html.escape(project)}</code>")
    if pane_id:
        header_parts.append(f"<code>{html.escape(pane_id)}</code>")
    header = "  ·  ".join(header_parts)

    # For permission / elicitation notifications we augment the body with
    # (1) whatever Claude wrote just before the tool call (crucial for
    # multi-choice prompts), (2) a rich description of the pending tool
    # (command / plan body / question + option descriptions), and (3)
    # a tailored reply keyboard. The preamble is quoted in a blockquote
    # so it reads as "what Claude said" separate from our own framing.
    pending_tool: dict[str, Any] | None = None
    pending_context: str = ""
    if notif_type in ("permission_prompt", "elicitation_dialog") and transcript_raw:
        path = Path(str(transcript_raw))
        if path.exists():
            pending_tool, pending_context = _find_pending_context(path)

    body_parts: list[str] = []
    if msg_text:
        body_parts.append(html.escape(msg_text))
    if pending_context:
        preview = pending_context
        if len(preview) > 1200:
            preview = preview[:1200].rstrip() + "…"
        body_parts.append(f"<blockquote>{html.escape(preview)}</blockquote>")
    if pending_tool:
        detail = _describe_tool_use(pending_tool)
        if detail:
            body_parts.append(detail)

    text = header
    if body_parts:
        text = header + "\n\n" + "\n\n".join(body_parts)

    reply_markup: dict[str, Any] | None = None
    if notif_type == "permission_prompt":
        reply_markup = _build_permission_keyboard(pane_id, pending_tool)

    for chat_id in _chat_ids():
        send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


# ---------- Mode: progress ----------


def _spawn_typing_pumper(session_id: str, chat_id: str) -> None:
    """Launch a detached pumper that keeps the 'typing…' indicator alive.

    Runs as a separate process with its own session so parent shells
    exiting don't kill it. The pumper itself exits when the progress
    file disappears (Stop hook cleared it) or the max-time cap fires.
    Environment is inherited so the child sees the bot token.
    """
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "pump", session_id, chat_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        pass  # failing here shouldn't block the progress hook


def main_progress() -> None:
    data = _stdin_json()
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    prompt = str(data.get("prompt") or "")
    pane_id = os.environ.get("TMUX_PANE", "")

    state.touch_activity(session_id)

    if pane_id and not state.is_subscribed(pane_id):
        return
    if pane_id and state.is_muted(pane_id):
        return

    header = _build_header(cwd, pane_id, "⏳")
    preview = prompt.strip()
    if len(preview) > 160:
        preview = preview[:160].rstrip() + "…"
    body = html.escape(preview, quote=False) if preview else "<i>Claude is working…</i>"
    text = f"{header}\n\n{body}"

    for chat_id in _chat_ids():
        # ⏳ placeholders go SILENT — the user just sent the prompt,
        # they don't need a phone buzz confirming that. Only the final
        # 🤖 reply (Stop hook) fires a push notification.
        msg_id = send_message(
            chat_id, text, parse_mode="HTML", disable_notification=True
        )
        if msg_id is not None:
            state.set_progress_msg_id(f"{session_id}:{chat_id}", msg_id)
            _spawn_typing_pumper(session_id, chat_id)


def _summarise_in_progress(
    transcript_path: Path,
) -> tuple[int, str, str | None]:
    """Return (tool_count, last_tool_name, latest_text) for the current turn.

    Counts assistant tool_use blocks since the last real user prompt and
    captures the most recent text block so the heartbeat update can
    preview what Claude has been saying along the way.
    """
    tool_count = 0
    last_tool = ""
    latest_text: str | None = None
    try:
        with transcript_path.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role")
                blocks = msg.get("content") or []
                if role == "user":
                    is_real_prompt = any(
                        isinstance(b, dict) and b.get("type") == "text" for b in blocks
                    ) or isinstance(msg.get("content"), str)
                    if is_real_prompt:
                        tool_count = 0
                        last_tool = ""
                        latest_text = None
                    continue
                if role != "assistant":
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        tool_count += 1
                        last_tool = str(block.get("name") or "")
                    elif btype == "text":
                        text = block.get("text") or ""
                        if text:
                            latest_text = text
    except OSError:
        pass
    return tool_count, last_tool, latest_text


def main_post_tool_use() -> None:
    """Update the ⏳ placeholder with a live progress snapshot.

    Fires after every tool call Claude runs, but throttled to one
    update every ~5 s per session (Telegram rate-limits edits and
    the user doesn't need every single edit reflected). Skipped
    when the pane isn't subscribed, is muted, or no ⏳ progress
    placeholder is tracked for this session.
    """
    data = _stdin_json()
    session_id = str(data.get("session_id") or "unknown")
    transcript_raw = data.get("transcript_path")
    cwd = str(data.get("cwd") or "")
    pane_id = os.environ.get("TMUX_PANE", "")

    state.touch_activity(session_id)

    if pane_id and not state.is_subscribed(pane_id):
        return
    if pane_id and state.is_muted(pane_id):
        return
    if not transcript_raw:
        return
    transcript_path = Path(str(transcript_raw))
    if not transcript_path.exists():
        return

    # Don't heartbeat unless there's actually a ⏳ placeholder to edit.
    chat_ids = _chat_ids()
    any_pending = any(
        state.get_progress_msg_id(f"{session_id}:{c}") is not None for c in chat_ids
    )
    if not any_pending:
        return

    if not state.should_heartbeat(session_id):
        return  # throttled — another update came <5 s ago

    tool_count, last_tool, latest_text = _summarise_in_progress(transcript_path)
    header = _build_header(cwd, pane_id, "⏳")

    # Body: tool counter + optional preview of the most recent text block.
    lines = [
        f"<i>Working… {tool_count} tool call{'' if tool_count == 1 else 's'}"
        + (f", last: <code>{html.escape(last_tool)}</code>" if last_tool else "")
        + "</i>"
    ]
    if latest_text:
        preview = latest_text.strip()
        if len(preview) > 600:
            preview = preview[:600].rstrip() + "…"
        lines.append(f"<blockquote expandable>{html.escape(preview)}</blockquote>")
    text = f"{header}\n\n" + "\n\n".join(lines)

    for chat_id in chat_ids:
        msg_id = state.get_progress_msg_id(f"{session_id}:{chat_id}")
        if msg_id is None:
            continue
        edit_message(chat_id, msg_id, text, parse_mode="HTML")


def main_pump() -> None:
    """Entry point for the typing-indicator pumper subprocess.

    Expected argv: [hooks.py, "pump", <session_id>, <chat_id>].
    """
    if len(sys.argv) < 4:
        return
    session_id = sys.argv[2]
    chat_id = sys.argv[3]
    progress_key = f"{session_id}:{chat_id}"
    deadline = time.monotonic() + _TYPING_PUMP_MAX_SECONDS
    while time.monotonic() < deadline:
        if state.get_progress_msg_id(progress_key) is None:
            return
        try:
            _call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass
        time.sleep(_TYPING_PUMP_INTERVAL)


# ---------- Entry ----------


def main() -> None:
    # `pump` has a different signature (positional session_id + chat_id),
    # so it bypasses argparse to keep the argument parsing simple.
    if len(sys.argv) >= 2 and sys.argv[1] == "pump":
        try:
            main_pump()
        except Exception as exc:
            sys.stderr.write(f"tele-claude pump failed: {exc}\n")
        return

    parser = argparse.ArgumentParser()
    _ = parser.add_argument(
        "mode", choices=["notify", "reply", "progress", "post_tool_use"]
    )
    args = parser.parse_args()
    handlers = {
        "notify": main_notify,
        "reply": main_reply,
        "progress": main_progress,
        "post_tool_use": main_post_tool_use,
    }
    try:
        handlers[args.mode]()
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"tele-claude hook {args.mode} failed: {exc}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

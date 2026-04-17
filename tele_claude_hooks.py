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
) -> int | None:
    resp = _call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
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

        for idx, piece_md in enumerate(chunks_md):
            piece_html = tele_claude_format.convert(piece_md)
            prefix = f"({idx + 1}/{total}) " if total > 1 else ""
            body = f"{prefix}{header}\n\n{piece_html}"
            is_last = idx == total - 1
            markup = last_markup if is_last else None

            if idx == 0 and progress_id is not None:
                ok = edit_message(
                    chat_id, progress_id, body, parse_mode="HTML", reply_markup=markup
                )
                if not ok:
                    send_message(chat_id, body, parse_mode="HTML", reply_markup=markup)
            else:
                send_message(chat_id, body, parse_mode="HTML", reply_markup=markup)

        state.clear_progress(progress_key)


# ---------- Mode: notify ----------


def main_notify() -> None:
    data = _stdin_json()
    notif_type = str(data.get("notification_type") or "unknown")
    session_id = str(data.get("session_id") or "unknown")
    cwd = str(data.get("cwd") or "")
    pane_id = os.environ.get("TMUX_PANE", "")

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

    parts = [f"{emoji} <b>{html.escape(label)}</b>"]
    project = _project(cwd)
    if project:
        parts.append(f"<code>{html.escape(project)}</code>")
    if pane_id:
        parts.append(f"<code>{html.escape(pane_id)}</code>")
    text = "  ·  ".join(parts)

    reply_markup: dict[str, Any] | None = None
    if notif_type == "permission_prompt" and pane_id:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "1 · Allow once", "callback_data": f"ans:{pane_id}:1"},
                    {"text": "2 · Always", "callback_data": f"ans:{pane_id}:2"},
                    {"text": "3 · Deny", "callback_data": f"ans:{pane_id}:3"},
                ]
            ]
        }

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

    if pane_id and state.is_muted(pane_id):
        return

    header = _build_header(cwd, pane_id, "⏳")
    preview = prompt.strip()
    if len(preview) > 160:
        preview = preview[:160].rstrip() + "…"
    body = html.escape(preview, quote=False) if preview else "<i>Claude is working…</i>"
    text = f"{header}\n\n{body}"

    for chat_id in _chat_ids():
        msg_id = send_message(chat_id, text, parse_mode="HTML")
        if msg_id is not None:
            state.set_progress_msg_id(f"{session_id}:{chat_id}", msg_id)
            _spawn_typing_pumper(session_id, chat_id)


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
    _ = parser.add_argument("mode", choices=["notify", "reply", "progress"])
    args = parser.parse_args()
    handlers = {"notify": main_notify, "reply": main_reply, "progress": main_progress}
    try:
        handlers[args.mode]()
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write(f"tele-claude hook {args.mode} failed: {exc}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

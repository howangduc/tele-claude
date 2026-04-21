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
# _TYPING_PUMP_MAX_SECONDS is an absolute wall-clock ceiling (protects
# against truly-orphaned pumpers), but the pumper also exits early as
# soon as the progress file disappears. Raised from 10 min → 45 min
# because long Agent-Team / Task fan-out turns can legitimately run
# longer than 10 min and users were watching the typing indicator go
# dead on healthy long turns.
_TYPING_PUMP_INTERVAL = 4.0
_TYPING_PUMP_MAX_SECONDS = 2700.0  # 45 min


# ---------- HTTP ----------


def _token() -> str:
    token = os.environ.get("CLAUDE_TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(0)
    return token


def _chat_ids() -> list[str]:
    raw = os.environ.get("CLAUDE_TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


_DEBUG_LOG = Path.home() / ".cache" / "tele-claude" / "debug" / "api-errors.log"


def _log_api_error(method: str, body: dict[str, str], err: str) -> None:
    """Append a one-line diagnostic to the debug log.

    Silent catch-all in _call made chunk-send failures invisible — a 4-
    of-4 reply would go silent-3-of-4 on a parse error and we'd never
    know. The log captures just enough to localise issues without
    leaking message bodies (the text payload is truncated to 200 chars).
    """
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        preview = body.get("text", "")[:200].replace("\n", "\\n")
        with _DEBUG_LOG.open("a") as f:
            f.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {method} "
                f"err={err} text_preview={preview!r}\n"
            )
    except OSError:
        pass


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
    except Exception as exc:
        # Try to extract Telegram's actual error body from HTTPError so
        # the log says "Bad Request: can't parse entities" instead of
        # a generic catch-all. Falls back to str(exc) for network errors.
        err_detail = str(exc)
        # HTTPError objects carry `.read()` with the Telegram-returned
        # error body. Narrow via HTTPError explicitly so we don't need
        # a blanket getattr/cast dance.
        from urllib.error import HTTPError

        if isinstance(exc, HTTPError):
            try:
                err_detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
        # "message is not modified" is expected for heartbeat edits when
        # nothing changed since the last tick — it's a no-op, not a bug.
        # Skip logging to keep the error log signal-to-noise high.
        if "message is not modified" not in err_detail:
            _log_api_error(method, body, err_detail)
        return {"ok": False, "description": err_detail}


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

    Resilience: if the initial call fails with parse_mode=HTML, we
    retry once as plain text (parse_mode=None) with the HTML entities
    unescaped so the user at least sees the content. Silent HTML-parse
    400s were previously causing whole chunks to vanish from split
    replies — a 4-of-4 reply would arrive as 3-of-4 with no indication.
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
    err = str(resp.get("description") or "")
    # Inline-keyboard button URL rejected? Drop the markup and retry —
    # the text itself is fine, we just can't attach the bad button.
    # Examples: local host URLs (localhost, minio), non-FQDN hosts.
    if reply_markup is not None and (
        "inline keyboard button URL" in err or "Wrong HTTP URL" in err
    ):
        retry = _call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": None,
                "disable_notification": disable_notification or None,
            },
        )
        if retry.get("ok"):
            return int(retry["result"]["message_id"])
    # HTML-parse failure? Degrade to plain text so the user still sees
    # the content. Strip tags roughly — enough to rescue the chunk.
    # Also drop reply_markup in case a bad URL was hiding behind the
    # parse error (would fail the fallback the same way).
    if parse_mode == "HTML":
        fallback = _strip_html_tags(text)
        resp = _call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"⚠️ <i>(HTML parse failed, sending as plain)</i>\n\n{fallback}",
                "parse_mode": None,
                "reply_markup": None,
                "disable_notification": disable_notification or None,
            },
        )
        if resp.get("ok"):
            return int(resp["result"]["message_id"])
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}


def _strip_html_tags(text: str) -> str:
    """Best-effort tag strip + entity unescape for the plain-text fallback."""
    plain = _TAG_RE.sub("", text)
    for entity, char in _ENTITY_MAP.items():
        plain = plain.replace(entity, char)
    return plain


def edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Edit a message. Returns (ok, error_description).

    The error string lets callers react to specific failures like
    "message to edit not found" (the target was deleted by the user
    or a prior hook), in which case the heartbeat should re-send a
    fresh placeholder rather than silently drop the update.
    """
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
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("description") or "")


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


def _set_pane_title(pane_id: str, title: str) -> None:
    """Set a tmux pane's title so `/panes` (and tmux borders, if enabled)
    show what Claude is actually doing instead of just the working dir.

    Silent best-effort: tmux unavailable, pane gone, whitespace-only
    title, etc. all just fall through without raising. Newlines
    flattened to spaces (tmux titles are single-line) and capped at
    60 chars so they fit status lines without truncation.
    """
    if not pane_id or not title:
        return
    clean = title.replace("\n", " ").replace("\r", " ").strip()[:60]
    if not clean:
        return
    try:
        _ = subprocess.run(
            ["tmux", "select-pane", "-t", pane_id, "-T", clean],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


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


def _is_button_safe_url(url: str) -> bool:
    """Telegram rejects inline-keyboard URLs that aren't publicly routable.

    Example rejections observed live:
      http://localhost:4200/api   → "Wrong HTTP URL"
      http://minio:9000           → "Wrong HTTP URL"

    These local/container-internal hosts slip into Claude's output when
    it explains docker-compose or dev setups. Attaching them as URL
    buttons kills the whole sendMessage (the parse-mode fallback also
    inherits the bad markup, so chunks get silently dropped). Filter
    here BEFORE building buttons — the URL still appears inline in the
    text, it just doesn't become a tappable 🔗 button.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        if not host:
            return False
        # Must be a dotted FQDN or dotted IP. Internal container names
        # (minio, redis, db, …) and "localhost" have no dot and get
        # dropped here.
        if "." not in host:
            return False
        # Common non-public dot-names also get filtered.
        blocked = {"localhost", "localhost.localdomain"}
        if host in blocked:
            return False
        if host.startswith("127.") or host == "0.0.0.0":
            return False
        return True
    except Exception:
        return False


def _url_buttons(text: str, max_buttons: int = 4) -> list[list[dict[str, Any]]]:
    seen: list[str] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;:!?)]")
        if not url or url in seen:
            continue
        if not _is_button_safe_url(url):
            continue
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
        # Rename from `lines` to avoid shadowing the `list[str]` named
        # `lines` in the AskUserQuestion branch above (basedpyright trips
        # on the name reuse even though control flow makes it safe).
        line_count = content.count("\n") + (1 if content else 0)
        return f"📝 <b>Write</b> <code>{esc(path)}</code> ({line_count} lines)"
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


def _edit_or_resend_progress(
    chat_id: str, session_id: str, msg_id: int, text: str
) -> None:
    """Edit the existing ⏳ placeholder OR recover by sending a fresh one.

    If the target message was deleted (by the user, by a previous Stop
    hook, or whatever), Telegram returns "message to edit not found".
    That used to be silently swallowed — heartbeat edits became no-ops
    and the ⏳ never visibly updated. Now we detect that specific error
    and send a new placeholder, updating the progress file so future
    edits target the new message. For other errors (rate-limit, parse),
    we just skip this tick — the next heartbeat will try again.
    """
    ok, err = edit_message(chat_id, msg_id, text, parse_mode="HTML")
    if ok:
        return
    # "message to edit not found" → the ⏳ was deleted; resurrect it.
    # "message is not modified" → same content, expected no-op.
    if "message to edit not found" in err:
        new_id = send_message(
            chat_id, text, parse_mode="HTML", disable_notification=True
        )
        if new_id is not None:
            state.set_progress_msg_id(f"{session_id}:{chat_id}", new_id)


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

    # First line of the reply as pane title — shows what Claude
    # finished with so users can tell idle panes apart in /panes.
    first_line = raw_md.strip().splitlines()[0] if raw_md.strip() else ""
    if first_line:
        # Strip markdown heading/formatting chars for a cleaner title.
        cleaned = (
            first_line.lstrip("# *_-").strip().replace("*", "").replace("`", "")[:40]
        )
        _set_pane_title(pane_id, f"🤖 {cleaned}" if cleaned else "🤖 done")

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
        tool_name = (
            str(pending_tool.get("name")) if pending_tool else ""
        ) or "permission"
        _set_pane_title(pane_id, f"🔐 {tool_name}")
    elif notif_type == "idle_prompt":
        _set_pane_title(pane_id, "💤 idle")

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

    # Surface the prompt in the tmux pane title so the user can see at a
    # glance what each of their panes is working on (visible in /panes
    # listing and on pane borders if pane-border-status is enabled).
    preview_title = prompt.strip().replace("\n", " ")[:40]
    _set_pane_title(pane_id, f"⏳ {preview_title}" if preview_title else "⏳ working")

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


def _truncate(text: str, limit: int) -> str:
    """Shorten text with an ellipsis if it exceeds ``limit`` chars."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _summarise_in_progress(
    transcript_path: Path,
) -> tuple[int, str, str | None, list[str]]:
    """Return (tool_count, last_tool_name, latest_text, running_subagents).

    Counts assistant tool_use blocks since the last real user prompt and
    captures the most recent text block so the heartbeat can preview
    what Claude has been saying along the way.

    ``running_subagents`` lists the ``description`` of any ``Task`` tool
    call whose matching ``tool_result`` hasn't arrived yet — i.e. the
    subagents currently doing work. Without this, Task fan-outs look
    like dead air on the heartbeat (the main transcript is quiet while
    subagents write to their own JSONL under ``subagents/``).
    """
    tool_count = 0
    last_tool = ""
    latest_text: str | None = None
    task_descriptions: dict[str, str] = {}
    completed_ids: set[str] = set()
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
                        task_descriptions = {}
                        completed_ids = set()
                        continue
                    # tool_result entry — record completed tool_use IDs
                    # so matching Task calls drop off "still running".
                    for block in blocks:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            tuid = str(block.get("tool_use_id") or "")
                            if tuid:
                                completed_ids.add(tuid)
                    continue
                if role != "assistant":
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        tool_count += 1
                        name = str(block.get("name") or "")
                        last_tool = name
                        if name == "Task":
                            tuid = str(block.get("id") or "")
                            inp = block.get("input") or {}
                            desc = ""
                            if isinstance(inp, dict):
                                desc = str(
                                    inp.get("description")
                                    or inp.get("subagent_type")
                                    or ""
                                ).strip()
                            if tuid:
                                task_descriptions[tuid] = desc
                    elif btype == "text":
                        text = block.get("text") or ""
                        if text:
                            latest_text = text
    except OSError:
        pass
    running_subagents = [
        desc
        for tuid, desc in task_descriptions.items()
        if tuid not in completed_ids and desc
    ]
    return tool_count, last_tool, latest_text, running_subagents


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

    tool_count, last_tool, latest_text, running_subagents = _summarise_in_progress(
        transcript_path
    )

    # Surface live status in the tmux pane title so /panes (and tmux
    # borders if the user enables pane-border-status) shows "⏳ 7t · Bash +2a"
    # instead of a generic working-directory label.
    title_bits: list[str] = [f"⏳ {tool_count}t"]
    if last_tool:
        title_bits.append(f"· {last_tool}")
    if running_subagents:
        title_bits.append(f"+{len(running_subagents)}a")
    _set_pane_title(pane_id, " ".join(title_bits))

    header = _build_header(cwd, pane_id, "⏳")

    # Body: tool counter + in-flight subagent list + optional text preview.
    summary = (
        f"<i>Working… {tool_count} tool call{'' if tool_count == 1 else 's'}"
        + (f", last: <code>{html.escape(last_tool)}</code>" if last_tool else "")
        + "</i>"
    )
    lines = [summary]
    if running_subagents:
        # Show up to 5 active subagents so big fan-outs don't blow the
        # message budget. Descriptions are user-facing (from Task's
        # `description` field) so escape and truncate defensively.
        agents_preview = running_subagents[:5]
        bullets = [
            f"🧑‍💻 {html.escape(_truncate(desc, 80))}" for desc in agents_preview
        ]
        extra = (
            f"\n<i>…and {len(running_subagents) - 5} more</i>"
            if len(running_subagents) > 5
            else ""
        )
        lines.append("\n".join(bullets) + extra)
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


def main_subagent_stop() -> None:
    """Fires when a Task-spawned subagent finishes.

    Clears the heartbeat throttle so the next PostToolUse (or a fresh
    tick of this hook) re-renders ⏳ immediately — otherwise the user
    can wait up to 5 s to see the subagent drop off `running_subagents`.
    If a progress placeholder is tracked for this session, also edits
    it in place with a "✓ subagent done: <agent_type>" line so the
    completion is visible even without any subsequent activity.
    """
    data = _stdin_json()
    session_id = str(data.get("session_id") or "unknown")
    agent_type = str(data.get("agent_type") or "subagent")
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

    chat_ids = _chat_ids()
    any_pending = any(
        state.get_progress_msg_id(f"{session_id}:{c}") is not None for c in chat_ids
    )
    if not any_pending:
        return

    # Safety floor: Telegram rate-limits to ~1 msg/sec per chat. Teams of
    # N subagents finishing in a burst would otherwise fire N edits in
    # ~100 ms, blowing past the limit. A 1.5 s minimum between SubagentStop
    # edits keeps us well inside safety while still feeling responsive.
    # This shares the same heartbeat timestamp file as PostToolUse, so
    # the two never race against each other either.
    if not state.should_heartbeat(session_id, min_interval_seconds=1.5):
        return

    tool_count, last_tool, latest_text, running_subagents = _summarise_in_progress(
        transcript_path
    )

    # Surface live status in the tmux pane title so /panes (and tmux
    # borders if the user enables pane-border-status) shows "⏳ 7t · Bash +2a"
    # instead of a generic working-directory label.
    title_bits: list[str] = [f"⏳ {tool_count}t"]
    if last_tool:
        title_bits.append(f"· {last_tool}")
    if running_subagents:
        title_bits.append(f"+{len(running_subagents)}a")
    _set_pane_title(pane_id, " ".join(title_bits))

    header = _build_header(cwd, pane_id, "⏳")
    summary = (
        f"<i>Working… {tool_count} tool call{'' if tool_count == 1 else 's'}"
        + (f", last: <code>{html.escape(last_tool)}</code>" if last_tool else "")
        + "</i>"
    )
    lines = [summary]
    lines.append(
        f"✓ subagent done: <code>{html.escape(_truncate(agent_type, 40))}</code>"
    )
    if running_subagents:
        remaining = running_subagents[:5]
        bullets = [f"🧑‍💻 {html.escape(_truncate(desc, 80))}" for desc in remaining]
        lines.append("\n".join(bullets))
    if latest_text:
        preview = latest_text.strip()
        if len(preview) > 600:
            preview = preview[:600].rstrip() + "…"
        lines.append(f"<blockquote expandable>{html.escape(preview)}</blockquote>")
    text = f"{header}\n\n" + "\n\n".join(lines)

    for chat_id in chat_ids:
        msg_id = state.get_progress_msg_id(f"{session_id}:{chat_id}")
        if msg_id is not None:
            _edit_or_resend_progress(chat_id, session_id, msg_id, text)


def main_teammate_idle() -> None:
    """Fires when a teammate in an Agent Team is about to go idle.

    Sends a dedicated Telegram notification (NOT an edit of the main
    ⏳ placeholder) so the user knows a specific teammate is waiting
    for input. Pushes a real notification because this is a signal
    to act — tap to message the teammate directly.
    """
    data = _stdin_json()
    teammate_name = str(data.get("teammate_name") or "")
    agent_type = str(data.get("agent_type") or "")
    agent_id = str(data.get("agent_id") or "")
    cwd = str(data.get("cwd") or "")
    last_message = str(data.get("last_assistant_message") or "").strip()
    pane_id = os.environ.get("TMUX_PANE", "")

    if pane_id and state.is_muted(pane_id):
        return

    header_parts = ["🧑‍💻 <b>Teammate idle</b>"]
    label = teammate_name or agent_type or "teammate"
    header_parts.append(f"<code>{html.escape(_truncate(label, 40))}</code>")
    project = _project(cwd)
    if project:
        header_parts.append(f"<code>{html.escape(project)}</code>")
    header = "  ·  ".join(header_parts)

    lines = [header]
    if agent_type and teammate_name and agent_type != teammate_name:
        lines.append(f"<i>role: <code>{html.escape(agent_type)}</code></i>")
    if last_message:
        preview = _truncate(last_message, 600)
        lines.append(f"<blockquote expandable>{html.escape(preview)}</blockquote>")
    text = "\n\n".join(lines)

    # The teammate's `agent_id` disambiguates it in multi-team scenarios.
    # The Send-msg button uses a force-reply on tap so the user can type
    # a response that the bot (eventually) can route back — out of scope
    # for this hook; for now, we simply notify.
    for chat_id in _chat_ids():
        send_message(chat_id, text, parse_mode="HTML")

    # Debug-friendly: agent_id persists in the notification for audit but
    # doesn't need surfacing unless we add routing. Keep the send simple.
    _ = agent_id


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
        "mode",
        choices=[
            "notify",
            "reply",
            "progress",
            "post_tool_use",
            "subagent_stop",
            "teammate_idle",
        ],
    )
    args = parser.parse_args()
    handlers = {
        "notify": main_notify,
        "reply": main_reply,
        "progress": main_progress,
        "post_tool_use": main_post_tool_use,
        "subagent_stop": main_subagent_stop,
        "teammate_idle": main_teammate_idle,
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

"""Telegram bot that forwards messages to Claude Code tmux panes.

Commands (single source of truth is ``_COMMANDS`` near the bottom):
  /panes             — tappable keyboard of Claude Code panes; tap to activate.
  /use %N            — set active pane without the picker.
  /which             — show the active pane.
  /pwd [%N]          — show pane's live working directory.
  /new [dir]         — spawn a fresh detached tmux session running cc; bare invocation prompts via ForceReply.
  /get <path>        — upload a server-side file to Telegram as a document; bare invocation prompts via ForceReply.
  /cancel [%N]       — send Ctrl-C to a pane (active if omitted).
  /mute %N           — silence Notification + Stop hooks for that pane.
  /unmute %N         — re-enable hooks.
  /muted             — list muted panes.
  /subscribe %N      — opt a pane into hook forwarding.
  /unsubscribe %N    — remove a pane from forwarding.
  /subscribed        — list subscribed panes.
  /history %N [n]    — capture and send the last N lines of a pane.
  /shortcut add|rm|list — manage user-defined Claude slash-command shortcuts.

Callback handlers (from inline keyboards placed by hooks or by /panes):
  use:%N          — activate a pane.
  ans:%N:1|2|3    — send a digit to a pane (permission prompt answers, single-select AskUserQuestion).
  mtg:%N:idx:mask — toggle an option in a multi-select AskUserQuestion (digit keystroke flips TUI checkbox).
  msub:%N         — advance a multi-select AskUserQuestion to Claude's review screen (Enter), then swap keyboard for final confirm/cancel.
  mfin:%N:1|2     — final step of multi-select: 1=Submit answers, 2=Cancel (digit + Enter on the TUI's review prompt).
  qr:%N:<text>    — quick-reply text to a pane.
  cancel:%N       — send Ctrl-C to a pane.

Fallback for plain-text messages: resolve pane from a reply-to `%N`,
otherwise the active pane for that chat. Replies to "Args for /cmd?"
ForceReply prompts are dispatched back through the matching handler
(either a built-in like /new or a shortcut forward).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import tele_claude_state as state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["CLAUDE_TELEGRAM_BOT_TOKEN"]
CHAT_IDS: frozenset[int] = frozenset(
    int(chunk.strip())
    for chunk in os.environ["CLAUDE_TELEGRAM_CHAT_ID"].split(",")
    if chunk.strip()
)

# Where to stash inbound images so Claude Code can pick them up via file path.
IMAGE_DIR = Path(
    os.environ.get("TELE_CLAUDE_IMAGE_DIR")
    or str(Path.home() / ".cache" / "tele-claude" / "images")
)

# Where to stash inbound non-image documents (txt, pdf, md, code, logs, …).
# Claude Code's Read tool handles pdf + text; everything else still reads
# fine as raw bytes. Overridable via TELE_CLAUDE_FILE_DIR.
FILE_DIR = Path(
    os.environ.get("TELE_CLAUDE_FILE_DIR")
    or str(Path.home() / ".cache" / "tele-claude" / "files")
)

# Filenames coming from Telegram may contain path separators or shell
# metacharacters — neutralise before we write to disk. Keeps letters,
# digits, dot, dash, underscore; collapses everything else to underscore.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


# Forum mode: when set, the bot runs inside a supergroup that has Topics
# enabled, and each Claude pane gets its own topic (forum thread). Value
# is the supergroup id (negative int, e.g. -1001234567890). Unset → bot
# runs in legacy single-thread mode (private chat or plain group) and
# every topic-related code path short-circuits.
def _read_forum_chat_id() -> int | None:
    raw = os.environ.get("TELE_CLAUDE_SUPERGROUP_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


_FORUM_CHAT_ID: int | None = _read_forum_chat_id()

# Telegram caps forum-topic names at 128 chars; we stay well under it.
_TOPIC_NAME_MAX = 120

# Width budget for the `cwd` segment of the topic name before we start
# ellipsising it. Leaves room for pane id, title, and two separators.
_TOPIC_CWD_MAX = 60

_PANE_RE = re.compile(r"(?<!\w)%\d+(?!\w)")

# Built-in bot commands that should NEVER be forwarded to a pane
# (checked by the slash-passthrough handler to avoid double-processing).
_BUILTIN_COMMANDS = {
    "panes",
    "use",
    "which",
    "pwd",
    "new",
    "get",
    "cancel",
    "mute",
    "unmute",
    "muted",
    "subscribe",
    "unsubscribe",
    "subscribed",
    "history",
    "shortcut",
}

# Prefix used on our "waiting for args" prompt messages. The reply handler
# detects these by checking reply_to_message.text against this prefix, then
# extracts the canonical command name from the rest of the line.
_ARGS_PROMPT_PREFIX = "Args for /"

# Reply text values that mean "send the command without any args".
_SKIP_ARGS_TOKENS = frozenset({"", ".", "-", "/", "skip", "go", "bare"})


def _authorised(chat_id: int) -> bool:
    return chat_id in CHAT_IDS


def _short_home(path: str) -> str:
    return path.replace(os.path.expanduser("~"), "~")


def _normalise_pane(raw: str) -> str:
    return raw if raw.startswith("%") else f"%{raw.lstrip('%')}"


def _list_claude_panes() -> list[tuple[str, str, str]]:
    """Return (pane_id, cwd, title) for every Claude-running pane.

    Title comes from tmux's pane_title attribute — our hooks update
    it on every prompt / tool call / reply so /panes can show what
    each pane is actually doing, not just the working directory.
    """
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            # Tab-separated so paths/titles with spaces stay intact.
            "-F",
            "#{pane_id}\t#{pane_current_path}\t#{pane_title}",
            "-f",
            "#{m:*claude*,#{pane_current_command}}",
        ],
        capture_output=True,
        text=True,
    )
    panes: list[tuple[str, str, str]] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        pane_id = parts[0] if len(parts) > 0 else ""
        path = parts[1] if len(parts) > 1 else ""
        title = parts[2] if len(parts) > 2 else ""
        if pane_id:
            panes.append((pane_id, path, title))
    return panes


def _pane_exists(pane_id: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
    )
    return pane_id in result.stdout.split()


def _send_to_tmux(pane_id: str, text: str) -> None:
    # Multi-line text lands in Claude Code's TUI as a collapsed
    # `[Pasted text #N +M lines]` token. If we hit Enter too quickly
    # after the paste, the REPL hasn't yet registered the paste as a
    # finished token and the Enter gets swallowed — the prompt stays
    # on screen but never submits. A short pause lets the REPL settle.
    # Also route multi-line via tmux load-buffer / paste-buffer so the
    # REPL treats it as a genuine paste (triggers the collapse path)
    # rather than as rapid-fire keystrokes.
    if "\n" in text:
        _ = subprocess.run(
            ["tmux", "load-buffer", "-b", "tele-claude-tmp", "-"],
            input=text,
            text=True,
            check=True,
        )
        _ = subprocess.run(
            ["tmux", "paste-buffer", "-b", "tele-claude-tmp", "-t", pane_id, "-d"],
            check=True,
        )
        time.sleep(0.3)
    else:
        _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], check=True)
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
    # Any successful send-via-bot is an implicit subscribe — the user has
    # clearly opted this pane into the Telegram conversation loop.
    state.subscribe_pane(pane_id)


def _send_key(pane_id: str, key: str) -> None:
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, key], check=True)
    state.subscribe_pane(pane_id)


def _pane_from_thread(message: Message) -> str | None:
    """Return the pane that owns the topic this message was posted in.

    Only non-None in forum mode when the message landed inside a
    pane-mapped topic. Shared helper so commands and plain-text
    handlers route consistently.
    """
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None:
        return None
    return state.get_pane_by_thread(int(thread_id))


def _pane_context(message: Message) -> str | None:
    """Resolve the pane context of an *inbound* command with no explicit %N.

    Order: topic (forum mode) → per-chat active pane. Used by bare-arg
    commands like ``/pwd``, ``/cancel``, ``/which`` — the topic you're
    viewing IS the active pane in forum mode, even though ``chat_id``
    is the supergroup's (shared across every topic).
    """
    pane = _pane_from_thread(message)
    if pane:
        return pane
    return state.get_active_pane(message.chat_id)


def _resolve_pane(chat_id: int, message: Message) -> str | None:
    # Forum mode wins: if this message was posted inside a topic that maps
    # to a pane, that's the intended destination — no /use, no reply-to
    # dance. The topic the user is viewing IS the active pane.
    pane = _pane_from_thread(message)
    if pane:
        return pane
    reply_to = message.reply_to_message
    if reply_to and reply_to.text:
        match = _PANE_RE.search(reply_to.text)
        if match:
            return match.group(0)
    return state.get_active_pane(chat_id)


# ---------- Forum-mode helpers ----------


def _forum_enabled() -> bool:
    return _FORUM_CHAT_ID is not None


def _truncate_middle(text: str, max_len: int) -> str:
    """Shrink ``text`` to ``max_len`` by dropping characters from the middle."""
    if len(text) <= max_len:
        return text
    if max_len < 3:
        return text[:max_len]
    keep = max_len - 1  # room for the ellipsis
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[-tail:]}"


def _compose_topic_name(pane_id: str, pane_title: str, cwd: str) -> str:
    """Build ``%N · <title> · <full cwd>`` capped to Telegram's 128-char
    topic-name limit. Full cwd is preserved verbatim unless it would
    overflow, in which case the middle is ellipsised.
    """
    title_part = pane_title.strip()
    if len(title_part) > 40:
        title_part = title_part[:37] + "…"
    cwd_part = _truncate_middle(cwd, _TOPIC_CWD_MAX)
    segments = [pane_id]
    if title_part:
        segments.append(title_part)
    if cwd_part:
        segments.append(cwd_part)
    name = " · ".join(segments)
    if len(name) > _TOPIC_NAME_MAX:
        name = name[: _TOPIC_NAME_MAX - 1] + "…"
    return name


def _pane_info(pane_id: str) -> tuple[str, str]:
    """Return ``(pane_title, cwd)`` for a live pane — empty strings if dead."""
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_title}\t#{pane_current_path}",
        ],
        capture_output=True,
        text=True,
    )
    out = result.stdout.strip()
    if not out:
        return "", ""
    parts = out.split("\t", 1)
    title = parts[0] if parts else ""
    cwd = parts[1] if len(parts) > 1 else ""
    return title, cwd


async def _ensure_topic_for_pane(
    app: Application[Any, Any, Any, Any, Any, Any],
    pane_id: str,
    pane_title: str = "",
    cwd: str = "",
) -> int | None:
    """Return the thread_id for ``pane_id``'s topic, creating it if needed.

    Short-circuits to ``None`` when forum mode is disabled so callers can
    pass the result straight through to ``message_thread_id=…`` and get
    legacy single-thread behavior for free.

    On missing topic metadata (fresh pane, no hook has fired yet), we
    probe tmux for live ``pane_title`` + ``pane_current_path`` so the
    first-seen name is already informative instead of ``%15 ·  ·``.
    """
    if not _forum_enabled() or _FORUM_CHAT_ID is None:
        return None
    existing = state.get_topic(pane_id)
    if existing is not None:
        return existing
    if not pane_title or not cwd:
        probe_title, probe_cwd = _pane_info(pane_id)
        pane_title = pane_title or probe_title
        cwd = cwd or probe_cwd
    name = _compose_topic_name(pane_id, pane_title, cwd)
    try:
        topic = await app.bot.create_forum_topic(chat_id=_FORUM_CHAT_ID, name=name)
    except Exception:
        logger.exception("createForumTopic failed for %s", pane_id)
        return None
    thread_id = int(topic.message_thread_id)
    state.set_topic(pane_id, thread_id)
    state.set_cached_topic_name(pane_id, name)
    logger.info("Created topic for %s: thread_id=%d name=%r", pane_id, thread_id, name)
    return thread_id


async def _delete_topic_for_pane(
    app: Application[Any, Any, Any, Any, Any, Any], pane_id: str, thread_id: int
) -> None:
    if not _forum_enabled() or _FORUM_CHAT_ID is None:
        return
    try:
        _ = await app.bot.delete_forum_topic(
            chat_id=_FORUM_CHAT_ID, message_thread_id=thread_id
        )
        logger.info("Deleted topic for %s (thread_id=%d)", pane_id, thread_id)
    except Exception:
        # Ignore — user may have deleted it manually, or permissions
        # missing. The state side is already cleaned up by prune_panes.
        logger.exception(
            "deleteForumTopic failed for %s (thread_id=%d)", pane_id, thread_id
        )


def _pane_arg_or_active(message: Message, args: list[str]) -> str | None:
    """Resolve target pane for a command: explicit %N arg wins, else context.

    Context = forum topic if the message came from one, else the chat's
    active pane. Makes bare ``/pwd``, ``/cancel``, etc. work correctly
    in a supergroup — in single-thread mode the topic branch is a no-op
    and behavior matches what it always was.
    """
    if args:
        return _normalise_pane(args[0])
    return _pane_context(message)


# ---------- Commands ----------


async def cmd_panes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    panes = _list_claude_panes()
    alive_ids = {p for p, _, _ in panes}
    # Forum-mode reconciliation: BEFORE prune_panes wipes topic state,
    # snapshot (pane_id, thread_id) for every now-dead pane so we can
    # issue deleteForumTopic on the Telegram side too. Skipped when
    # forum mode is off — get_all_topics returns {} so the set stays
    # empty and nothing happens.
    dead_topic_targets: list[tuple[str, int]] = []
    if _forum_enabled():
        dead_topic_targets = [
            (pane, tid)
            for pane, tid in state.get_all_topics().items()
            if pane not in alive_ids
        ]
    # Purge any state pointing at panes that no longer exist so the UI
    # never shows stale %IDs (active/subscribed/muted/topics all get
    # cleaned in one atomic save).
    _ = state.prune_panes(alive_ids)
    for pane_id, thread_id in dead_topic_targets:
        await _delete_topic_for_pane(context.application, pane_id, thread_id)
    # Create topics for any live pane that doesn't have one yet — makes
    # /panes a full-reconcile command in both directions. No-op when
    # forum mode is off.
    if _forum_enabled():
        for pane_id, path, title in panes:
            if state.get_topic(pane_id) is None:
                _ = await _ensure_topic_for_pane(
                    context.application, pane_id, pane_title=title, cwd=path
                )
    if not panes:
        _ = await message.reply_text("No Claude Code panes found.")
        return
    active = state.get_active_pane(message.chat_id)
    subscribed = state.get_subscribed_panes()
    muted = state.get_muted_panes()
    rows: list[list[InlineKeyboardButton]] = []
    for pane_id, path, title in panes:
        if pane_id == active:
            badge = "● "
        elif pane_id in muted:
            badge = "🔕 "
        elif pane_id in subscribed:
            badge = "🔔 "
        else:
            badge = "· "  # unsubscribed — no forwarding yet
        # Title set by our hooks wins the button label — it tells the user
        # what the pane is doing. Falls back to working dir for panes
        # whose hooks haven't fired yet (fresh /new, or untouched panes).
        # Telegram inline-button labels have a practical limit ~64 chars.
        label_body = (
            title
            if title and not title.startswith(os.path.basename(path.rstrip("/")))
            else _short_home(path)
        )
        label = f"{badge}{pane_id}  {label_body}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"use:{pane_id}")])
    subs_summary = (
        f"{len(subscribed & alive_ids)}/{len(alive_ids)} subscribed"
        if alive_ids
        else "none"
    )
    header_lines = [
        f"Active: {active}" if active else "No active pane.",
        f"Subscriptions: {subs_summary} · Tap to select (auto-subscribes):",
    ]
    _ = await message.reply_text(
        "\n".join(header_lines), reply_markup=InlineKeyboardMarkup(rows)
    )


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    if not args:
        # Bare /use = "what's active?". In a topic, that's the topic's
        # pane; outside, the per-chat active pane.
        current = _pane_context(message)
        _ = await message.reply_text(
            f"Active: {current}" if current else "No active pane. Use /panes or /use %N"
        )
        return
    pane_id = _normalise_pane(args[0])
    state.set_active_pane(message.chat_id, pane_id)
    state.subscribe_pane(pane_id)  # explicit pick = explicit subscription
    _ = await message.reply_text(f"Active pane: {pane_id} 🔔 subscribed")


async def cmd_which(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    # In a forum topic, "which pane?" = the pane that owns this topic.
    # Outside a topic, fall back to the per-chat active pane.
    topic_pane = _pane_from_thread(message)
    if topic_pane:
        _ = await message.reply_text(f"Active: {topic_pane} (from this topic)")
        return
    current = state.get_active_pane(message.chat_id)
    _ = await message.reply_text(
        f"Active: {current}" if current else "No active pane. Use /panes or /use %N"
    )


def _pick_session_name(cwd: str) -> str:
    """Derive a unique tmux session name from the cwd basename.

    Collision handling: if ``claude-<basename>`` is taken, try ``-2``,
    ``-3``, … until we find a free one. tmux session names allow most
    characters but we scrub anything not in ``[\\w-]`` to keep names
    shell- and tmux-friendly (tmux uses ``:`` and ``.`` as separators).
    """
    base = os.path.basename(cwd.rstrip("/")) or "home"
    safe = re.sub(r"[^\w-]", "-", base).strip("-") or "home"
    existing = {
        line
        for line in subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if line
    }
    candidate = f"claude-{safe}"
    i = 2
    while candidate in existing:
        candidate = f"claude-{safe}-{i}"
        i += 1
    return candidate


async def _spawn_new_pane(message: Message, cwd_arg: str) -> None:
    """Spawn a fresh detached tmux session running ``cc`` and subscribe it.

    Each ``/new`` gets its own session (not just a window) so concurrent
    Claude tasks stay isolated — independent scrollback, single
    ``tmux kill-session`` cleanup, and no yanking the user's current
    client to a new window. The new session is detached so the user's
    attached terminal keeps doing whatever it was doing; they
    ``tmux attach -t <name>`` when they want to see it directly.
    """
    logger.info("cmd_new: spawning new session, cwd_arg=%r", cwd_arg)
    cwd = os.path.expanduser(cwd_arg) if cwd_arg else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        logger.info("cmd_new: directory not found: %s", cwd)
        _ = await message.reply_text(
            f"Directory not found: <code>{_html.escape(cwd)}</code>",
            parse_mode="HTML",
        )
        return

    session_name = _pick_session_name(cwd)
    logger.info("cmd_new: creating session=%r cwd=%s", session_name, cwd)
    try:
        created = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",  # detached — don't steal the user's current client
                "-s",
                session_name,
                "-c",
                cwd,
                "-P",
                "-F",
                "#{pane_id}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.exception("cmd_new: tmux new-session failed")
        _ = await message.reply_text(f"Failed to create session: {e.stderr or e}")
        return
    new_pane = created.stdout.strip()
    if not new_pane:
        logger.error("cmd_new: tmux returned empty pane id")
        _ = await message.reply_text("tmux didn't return a pane id.")
        return
    logger.info(
        "cmd_new: created pane %s in session %s, launching cc", new_pane, session_name
    )

    time.sleep(0.4)
    _ = subprocess.run(["tmux", "send-keys", "-t", new_pane, "cc", "Enter"], check=True)

    state.subscribe_pane(new_pane)
    state.set_active_pane(message.chat_id, new_pane)

    short_cwd = cwd.replace(os.path.expanduser("~"), "~")
    _ = await message.reply_text(
        f"✅ Spawned <code>{_html.escape(new_pane)}</code> in "
        f"<code>{_html.escape(short_cwd)}</code>\n"
        f"New session <code>{_html.escape(session_name)}</code> (detached) · "
        f"Launched <code>cc</code> · active + subscribed 🔔\n"
        f"Attach: <code>tmux attach -t {_html.escape(session_name)}</code>",
        parse_mode="HTML",
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Spawn a new Claude pane. With args: spawn immediately. Bare: ForceReply.

    Usage:
      /new             → ForceReply prompt (misclick-safe from ☰ menu)
      /new ~/foo       → spawn directly in that directory
      /new /abs        → absolute paths accepted

    The bare-invocation path prompts because tapping /new from Telegram's
    ☰ Menu fires it with no args — silent-spawning in $HOME on a misclick
    is the wrong default. Matches the Claude-shortcut ForceReply UX.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    logger.info("cmd_new invoked, args=%r", args)
    if args:
        await _spawn_new_pane(message, args[0])
        return
    logger.info("cmd_new: bare invocation, sending ForceReply prompt")
    _ = await message.reply_text(
        f"{_ARGS_PROMPT_PREFIX}new?\n\n"
        "Reply with a directory (e.g. <code>~/Source/foo</code>) or "
        "<code>.</code> to use <code>$HOME</code>.",
        parse_mode="HTML",
        reply_markup=ForceReply(
            input_field_placeholder="dir (or . for $HOME)",
            selective=True,
        ),
    )


async def cmd_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the live `pane_current_path` of a pane as a tap-to-copy code block."""
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text(
            "Usage: /pwd [%N] (or set an active pane via /use %N)"
        )
        return
    if not _pane_exists(pane_id):
        _ = await message.reply_text(f"Pane {pane_id} no longer exists.")
        return
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    path = result.stdout.strip() or "(empty)"
    _ = await message.reply_text(
        f"<b>{_html.escape(pane_id)}</b>\n<code>{_html.escape(path)}</code>",
        parse_mode="HTML",
    )


# Telegram's sendDocument caps uploads at 50 MB; leave a small margin.
_GET_MAX_BYTES = 49 * 1024 * 1024


async def _send_file_to_user(message: Message, path_arg: str) -> None:
    """Resolve ``path_arg``, validate, and upload as a Telegram document.

    Shared between the direct ``/get <path>`` call and the ForceReply
    dispatch for bare ``/get``. Relative paths are resolved against the
    context pane's ``pane_current_path`` so e.g. ``/get report.md`` from
    inside topic ``%21`` looks in pane ``%21``'s cwd.
    """
    path_arg = path_arg.strip().strip('"').strip("'")
    if not path_arg:
        _ = await message.reply_text("Missing path.")
        return
    path = os.path.expanduser(path_arg)
    if not os.path.isabs(path):
        pane = _pane_context(message)
        if not pane or not _pane_exists(pane):
            _ = await message.reply_text(
                "Relative path given but no active pane to resolve against. "
                "Use an absolute path or set an active pane via /use."
            )
            return
        probe = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_path}"],
            capture_output=True,
            text=True,
        )
        base = probe.stdout.strip()
        if base:
            path = os.path.normpath(os.path.join(base, path))
    if not os.path.exists(path):
        _ = await message.reply_text(
            f"Not found: <code>{_html.escape(path)}</code>", parse_mode="HTML"
        )
        return
    if os.path.isdir(path):
        _ = await message.reply_text(
            f"Is a directory: <code>{_html.escape(path)}</code>", parse_mode="HTML"
        )
        return
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        _ = await message.reply_text(
            f"Can't stat <code>{_html.escape(path)}</code>: {exc}", parse_mode="HTML"
        )
        return
    if size > _GET_MAX_BYTES:
        _ = await message.reply_text(
            f"Too large: {size / 1024 / 1024:.1f} MB "
            f"(Telegram sendDocument cap: 50 MB)."
        )
        return
    try:
        with open(path, "rb") as fh:
            # reply_document uploads via sendDocument — inherits the
            # inbound message's thread_id so in forum mode the file
            # lands in the same topic the user requested it from.
            _ = await message.reply_document(
                document=fh,
                filename=os.path.basename(path),
                caption=(
                    f"<code>{_html.escape(path)}</code> · {size:,} bytes"
                    if size
                    else f"<code>{_html.escape(path)}</code> · empty"
                ),
                parse_mode="HTML",
            )
        logger.info("cmd_get: sent %s (%d bytes)", path, size)
    except Exception as exc:  # pragma: no cover — Telegram / file I/O
        logger.exception("cmd_get: failed to upload %s", path)
        _ = await message.reply_text(f"Failed to upload: {exc}")


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload a server-side file to Telegram as a downloadable document.

    Usage:
      /get <path>     → bot uploads the file to the current chat/topic
      /get            → ForceReply prompts for a path

    Paths may be absolute, ``~``-prefixed, or relative to the context
    pane's cwd (the pane that owns the current topic, or the chat's
    active pane). Handy for pulling back markdown reports, log files,
    PDFs, or code that Claude wrote on the server — anything under the
    50 MB Telegram sendDocument limit.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    if args:
        await _send_file_to_user(message, " ".join(args))
        return
    _ = await message.reply_text(
        f"{_ARGS_PROMPT_PREFIX}get?\n\n"
        "Reply with an absolute path, <code>~</code>-path, or a relative "
        "path (resolved against the active pane's cwd).",
        parse_mode="HTML",
        reply_markup=ForceReply(
            input_field_placeholder="path (e.g. ~/report.md)",
            selective=True,
        ),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /cancel %N (or set an active pane first)")
        return
    if not _pane_exists(pane_id):
        _ = await message.reply_text(f"Pane {pane_id} no longer exists.")
        return
    try:
        _send_key(pane_id, "C-c")
        _ = await message.reply_text(f"🛑 Ctrl-C → {pane_id}")
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(f"Failed: {e}")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /mute %N")
        return
    state.mute_pane(pane_id)
    _ = await message.reply_text(f"🔕 Muted {pane_id}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /unmute %N")
        return
    state.unmute_pane(pane_id)
    _ = await message.reply_text(f"🔔 Unmuted {pane_id}")


async def cmd_muted(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    muted = sorted(state.get_muted_panes())
    _ = await message.reply_text(
        "Muted: " + ", ".join(muted) if muted else "No muted panes."
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicitly subscribe a pane to hook forwarding."""
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /subscribe %N")
        return
    state.subscribe_pane(pane_id)
    _ = await message.reply_text(f"🔔 Subscribed {pane_id}")


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a pane from hook forwarding. Hooks will exit silently for it."""
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /unsubscribe %N")
        return
    state.unsubscribe_pane(pane_id)
    _ = await message.reply_text(f"🔕 Unsubscribed {pane_id} (hooks will skip it)")


async def cmd_subscribed(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    subs = sorted(state.get_subscribed_panes())
    _ = await message.reply_text(
        "Subscribed: " + ", ".join(subs) if subs else "No subscribed panes."
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id: str | None = None
    lines = 20
    for arg in context.args or []:
        if arg.startswith("%"):
            pane_id = _normalise_pane(arg)
        elif arg.isdigit():
            lines = max(1, min(int(arg), 500))
    if not pane_id:
        pane_id = _pane_context(message)
    if not pane_id:
        _ = await message.reply_text("Usage: /history %N [lines]")
        return
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(f"Failed: {e}")
        return
    body = result.stdout.rstrip()
    if not body:
        _ = await message.reply_text(f"Pane {pane_id} is empty.")
        return
    if len(body) > 3500:
        body = "…\n" + body[-3500:]
    safe = _html.escape(body, quote=False)
    _ = await message.reply_text(
        f"<b>{pane_id}</b> · last {lines} lines\n<pre>{safe}</pre>",
        parse_mode="HTML",
    )


# ---------- Callback buttons ----------


async def on_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return
    message = query.message
    if not _authorised(message.chat_id):
        return
    data = query.data or ""

    if data.startswith("use:"):
        pane_id = data[4:]
        state.set_active_pane(message.chat_id, pane_id)
        _ = await query.answer(f"Active: {pane_id}")
        _ = await query.edit_message_text(
            f"Active pane: {pane_id}\n\nSend any message to forward it here."
        )
        return

    if data.startswith("ans:"):
        _, pane_id, answer = data.split(":", 2)
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_to_tmux(pane_id, answer)
            # Alert-style popup (needs a tap to dismiss) so the user gets
            # unambiguous confirmation even if they miss the brief toast.
            _ = await query.answer(f"✅ Sent {answer} → {pane_id}", show_alert=True)
            # Drop the buttons so the message visually "commits" to the
            # decision. We deliberately DON'T edit the body — the original
            # message's HTML (plan, question text, preamble) is kept intact
            # for scrollback. Editing the text risks re-parsing failures
            # when the message contains nested tags or HTML-special chars,
            # which manifests on the phone as "tap did nothing".
            try:
                _ = await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
        return

    if data.startswith("mtg:"):
        # Multi-select AskUserQuestion toggle. Each tap sends the digit
        # keystroke to the pane (Claude's TUI toggles that option's
        # checkbox) and flips the matching bit in the locally-tracked
        # bitmask so the keyboard can redraw ☐/☑ to mirror the TUI state.
        parts = data.split(":", 3)
        if len(parts) != 4:
            _ = await query.answer()
            return
        _, pane_id, idx_str, mask_str = parts
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            idx = int(idx_str)
            mask = int(mask_str)
        except ValueError:
            _ = await query.answer()
            return
        try:
            _send_key(pane_id, idx_str)  # digit keystroke = TUI toggle
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
        new_mask = mask ^ (1 << (idx - 1))
        # Pull the option count from the existing keyboard shape (total
        # rows minus the trailing Submit row) so we don't have to re-ship
        # the option list in every callback_data.
        current = message.reply_markup
        n_options = 0
        if current and current.inline_keyboard:
            n_options = max(0, len(current.inline_keyboard) - 1)
        if n_options == 0:
            n_options = idx  # defensive fallback
        new_rows: list[list[InlineKeyboardButton]] = []
        for i in range(1, n_options + 1):
            checked = "☑" if new_mask & (1 << (i - 1)) else "☐"
            new_rows.append(
                [
                    InlineKeyboardButton(
                        f"{checked} {i}",
                        callback_data=f"mtg:{pane_id}:{i}:{new_mask}",
                    )
                ]
            )
        new_rows.append(
            [InlineKeyboardButton("✅ Submit", callback_data=f"msub:{pane_id}")]
        )
        try:
            _ = await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(new_rows)
            )
        except Exception:
            pass
        _ = await query.answer(f"Toggled {idx}")
        return

    if data.startswith("msub:"):
        # Multi-select "Submit" tap — advances the TUI from toggle mode to
        # the "Review your answers / 1. Submit answers / 2. Cancel"
        # screen. We don't finalize yet because the TUI still needs one
        # more tap to confirm; swap the keyboard for that final pair so
        # the user never has to type.
        pane_id = data[len("msub:") :]
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_key(pane_id, "Enter")
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
        finalise_rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    "✅ Submit answers",
                    callback_data=f"mfin:{pane_id}:1",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"mfin:{pane_id}:2",
                ),
            ]
        ]
        try:
            _ = await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(finalise_rows)
            )
        except Exception:
            pass
        _ = await query.answer("Review — tap to finalise")
        return

    if data.startswith("mfin:"):
        # Final step of multi-select: send the digit (1=submit, 2=cancel)
        # on the TUI's review screen. Matches the shape of the single-
        # select `ans:` callback — digit + Enter so the TUI commits.
        parts = data.split(":", 2)
        if len(parts) != 3:
            _ = await query.answer()
            return
        _, pane_id, choice = parts
        if choice not in {"1", "2"}:
            _ = await query.answer()
            return
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_to_tmux(pane_id, choice)
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
        try:
            _ = await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        label = "Submitted" if choice == "1" else "Cancelled"
        _ = await query.answer(f"✅ {label} → {pane_id}", show_alert=True)
        return

    if data.startswith("qr:"):
        _, pane_id, text = data.split(":", 2)
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_to_tmux(pane_id, text)
            _ = await query.answer(f"→ {pane_id}: {text}")
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
        return

    if data.startswith("cancel:"):
        pane_id = data[len("cancel:") :]
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_key(pane_id, "C-c")
            _ = await query.answer(f"🛑 Ctrl-C → {pane_id}", show_alert=True)
            try:
                _ = await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
        return

    _ = await query.answer()


# ---------- Plain messages ----------


async def cmd_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage Claude slash-command shortcuts: /shortcut add|rm|list [name] [desc]."""
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    if not args:
        _ = await message.reply_text(
            "Usage:\n"
            "/shortcut add <name> [description]\n"
            "/shortcut rm <name>\n"
            "/shortcut list"
        )
        return
    sub = args[0].lower()
    if sub == "list":
        shortcuts = state.get_claude_shortcuts()
        if not shortcuts:
            _ = await message.reply_text(
                "No shortcuts yet. Add one with /shortcut add <name>"
            )
            return
        lines = ["<b>Claude shortcuts:</b>"]
        for name, desc in sorted(shortcuts.items()):
            lines.append(f"• /{_html.escape(name)} — {_html.escape(desc)}")
        _ = await message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    if sub == "add":
        if len(args) < 2:
            _ = await message.reply_text("Usage: /shortcut add <name> [description]")
            return
        name = args[1].lstrip("/")
        desc = " ".join(args[2:]) if len(args) > 2 else ""
        state.add_claude_shortcut(name, desc)
        await _publish_menu(context.application)
        _ = await message.reply_text(f"Added shortcut /{name}. Menu refreshed.")
        return
    if sub == "rm":
        if len(args) < 2:
            _ = await message.reply_text("Usage: /shortcut rm <name>")
            return
        name = args[1].lstrip("/")
        state.remove_claude_shortcut(name)
        await _publish_menu(context.application)
        _ = await message.reply_text(f"Removed shortcut /{name}. Menu refreshed.")
        return
    _ = await message.reply_text(f"Unknown subcommand: {sub}. Try /shortcut list")


def _canonicalise_shortcut(incoming: str) -> str:
    """Map Telegram's underscore-aliased shortcut back to its hyphenated form."""
    shortcuts = state.get_claude_shortcuts()
    if incoming in shortcuts:
        return incoming
    for stored in shortcuts:
        if stored.replace("-", "_") == incoming:
            return stored
    return incoming


async def _prompt_for_args(message: Message, canonical: str) -> None:
    """Reply with a ForceReply prompt so the user can add args one-handed.

    Inserted between ``on_slash_passthrough`` and the pane forward when a
    known shortcut is invoked bare (no args). Users tapping from
    Telegram's ☰ Menu button get a chance to dictate args; power users
    who already supplied args in the original message skip this path.
    """
    shortcuts = state.get_claude_shortcuts()
    description = shortcuts.get(canonical, "")
    lines = [f"{_ARGS_PROMPT_PREFIX}{canonical}?"]
    if description:
        lines.append(f"<i>{_html.escape(description)}</i>")
    lines.append("")
    lines.append("Reply with your args, or send <code>.</code> to forward bare.")
    _ = await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=ForceReply(
            input_field_placeholder=f"args for /{canonical}…",
            selective=True,
        ),
    )


async def _forward_shortcut_to_pane(
    message: Message, canonical: str, args: str
) -> None:
    """Shared send path used by both the direct and ForceReply-reply flows."""
    forward = f"/{canonical}" + (f" {args}" if args else "")
    pane_id = _resolve_pane(message.chat_id, message)
    if not pane_id:
        _ = await message.reply_text(
            "No active pane. Use /panes to pick one, or reply to a pane message."
        )
        return
    if not _pane_exists(pane_id):
        _ = await message.reply_text(
            f"Pane {pane_id} no longer exists. /panes to pick a live one."
        )
        return
    try:
        _send_to_tmux(pane_id, forward)
        _ = await message.reply_text(
            f"→ {pane_id}: <code>{_html.escape(forward[:80])}</code>",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(
            f"Failed to send to pane {pane_id}: {e}",
            reply_to_message_id=message.message_id,
        )


async def on_slash_passthrough(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any unknown /command to the active pane, preserving original name.

    If the user invoked a known shortcut BARE (no args), we intercept and
    reply with a ForceReply prompt so they can add args without typing
    the command name themselves (friendly to ☰ Menu tappers). Power users
    who supplied args in the original message skip the prompt entirely.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return

    first, _, rest = text.partition(" ")
    cmd_name = first[1:]  # strip leading slash
    if cmd_name in _BUILTIN_COMMANDS:
        return  # handled by CommandHandler (safety net only)

    canonical = _canonicalise_shortcut(cmd_name)
    shortcuts = state.get_claude_shortcuts()
    if canonical in shortcuts and not rest.strip():
        await _prompt_for_args(message, canonical)
        return

    await _forward_shortcut_to_pane(message, canonical, rest.strip())


def _safe_filename(name: str) -> str:
    """Sanitize a Telegram-supplied filename for disk use.

    Strips any path component (defence-in-depth for ``../evil.txt``),
    replaces anything outside ``[A-Za-z0-9._-]`` with ``_``, and
    clamps the total length so odd filenames don't blow past the
    filesystem's per-name limit.
    """
    base = os.path.basename(name) or "file"
    safe = _FILENAME_SAFE_RE.sub("_", base).strip("._") or "file"
    return safe[:120]


async def on_attachment(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward any attached file (image or document) to the active pane.

    Handles three cases:

    * **PHOTO** — compressed image from the gallery. Saved to
      ``IMAGE_DIR`` with a deterministic ``tg_<msg>_<uniq>.jpg`` name.
    * **Document with image/* mime** — "send as file" from the gallery,
      keeps original quality. Also lands in ``IMAGE_DIR`` with the
      original extension preserved.
    * **Any other Document** — txt, md, pdf, log, code files, zips,
      whatever. Saved to ``FILE_DIR`` with the original filename
      sanitised for disk safety. Claude Code's Read tool handles pdf
      + text transparently; binaries still read as raw bytes.

    Caption, if any, is sent first so Claude sees context before the
    file path: ``<caption>\\n<abs_path>``. Reply to the Telegram user
    names the file so they can tell multi-attachment sends apart.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return

    tg_file = None
    out_path: Path | None = None
    reply_emoji = "📎"
    reply_label: str = ""

    if message.photo:
        # Compressed photo — only msg_id+unique identifies it.
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = IMAGE_DIR / f"tg_{message.message_id}_{photo.file_unique_id}.jpg"
        reply_emoji = "🖼"
    elif message.document:
        doc = message.document
        tg_file = await doc.get_file()
        mime = (doc.mime_type or "").lower()
        original = doc.file_name or ""
        suffix = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if mime.startswith("image/"):
            # Original-quality photo — treat as image.
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            safe_suffix = suffix or "jpg"
            out_path = (
                IMAGE_DIR
                / f"tg_{message.message_id}_{doc.file_unique_id}.{safe_suffix}"
            )
            reply_emoji = "🖼"
        else:
            # Any other document — preserve the original filename so
            # Claude sees meaningful context ("this is `error.log`").
            # Prefix with msg_id for collision safety.
            FILE_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_filename(
                original or f"file.{suffix}" if suffix else "file"
            )
            out_path = FILE_DIR / f"tg_{message.message_id}_{safe_name}"
            reply_label = original or safe_name
            reply_emoji = "📄"
    else:
        return

    pane_id = _resolve_pane(message.chat_id, message)
    if not pane_id:
        _ = await message.reply_text(
            "No active pane. Use /panes to pick one, or reply to a pane message."
        )
        return
    if not _pane_exists(pane_id):
        _ = await message.reply_text(
            f"Pane {pane_id} no longer exists. /panes to pick a live one."
        )
        return

    _ = await tg_file.download_to_drive(str(out_path))

    caption = (message.caption or "").strip()
    text = f"{caption}\n{out_path}" if caption else str(out_path)

    logger.info(
        "%s → tmux pane %s: %s (caption=%r)", reply_emoji, pane_id, out_path, caption
    )
    try:
        _send_to_tmux(pane_id, text)
        suffix_label = (
            f" <code>{_html.escape(reply_label)}</code>" if reply_label else ""
        )
        _ = await message.reply_text(
            f"{reply_emoji}{suffix_label} → {pane_id}",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(
            f"Failed to send to pane {pane_id}: {e}",
            reply_to_message_id=message.message_id,
        )


# Back-compat alias — the MessageHandler filter was registered under
# ``on_photo`` name for a long time. Keep it around in case anything
# imports it.
on_photo = on_attachment


async def on_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return

    # If this is a reply to one of our "Args for /cmd?" prompts, dispatch.
    # Built-in commands (like /new) route to their handler; Claude shortcuts
    # forward /cmd <args> to the active pane. Skip tokens (".", "-", "skip",
    # "go", "bare", empty) mean "no args / accept default".
    reply_to = message.reply_to_message
    if reply_to and reply_to.text and reply_to.text.startswith(_ARGS_PROMPT_PREFIX):
        header = reply_to.text[len(_ARGS_PROMPT_PREFIX) :]
        canonical = header.split("?", 1)[0].strip()
        if canonical:
            raw_args = (message.text or "").strip()
            args = "" if raw_args.lower() in _SKIP_ARGS_TOKENS else raw_args
            if canonical == "new":
                await _spawn_new_pane(message, args)
                return
            if canonical == "get":
                await _send_file_to_user(message, args)
                return
            await _forward_shortcut_to_pane(message, canonical, args)
            return

    pane_id = _resolve_pane(message.chat_id, message)
    if not pane_id:
        _ = await message.reply_text(
            "No active pane. Use /panes to pick one, or reply to a pane message."
        )
        return
    if not _pane_exists(pane_id):
        _ = await message.reply_text(
            f"Pane {pane_id} no longer exists. /panes to pick a live one."
        )
        return
    text = message.text
    if not text:
        return
    logger.info("Sending to tmux pane %s: %s", pane_id, text)
    try:
        _send_to_tmux(pane_id, text)
        _ = await message.reply_text(
            f"→ {pane_id}", reply_to_message_id=message.message_id
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(f"Failed to send to pane {pane_id}: {e}")


# ---------- Entry ----------

_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Any]

# (command_name, description, handler) — single source of truth for both
# telegram.ext handler registration and the Telegram UI command menu.
_COMMANDS: list[tuple[str, str, _Handler]] = [
    ("panes", "List Claude Code panes (tap to activate+subscribe)", cmd_panes),
    ("use", "Set active pane: /use %N", cmd_use),
    ("which", "Show the active pane", cmd_which),
    ("pwd", "Show pane's working directory: /pwd [%N]", cmd_pwd),
    ("new", "Spawn a new Claude pane: /new [dir]", cmd_new),
    ("get", "Upload a server file to Telegram: /get <path>", cmd_get),
    ("cancel", "Send Ctrl-C: /cancel [%N]", cmd_cancel),
    ("mute", "Silence hooks: /mute %N", cmd_mute),
    ("unmute", "Re-enable hooks: /unmute %N", cmd_unmute),
    ("muted", "List muted panes", cmd_muted),
    ("subscribe", "Subscribe pane to hooks: /subscribe %N", cmd_subscribe),
    ("unsubscribe", "Stop hooks for pane: /unsubscribe %N", cmd_unsubscribe),
    ("subscribed", "List subscribed panes", cmd_subscribed),
    ("history", "Capture pane output: /history %N [lines]", cmd_history),
    ("shortcut", "Manage Claude shortcuts (add/rm/list)", cmd_shortcut),
]


async def _verify_forum_mode(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Sanity-check forum-mode setup and log any missing prerequisites.

    Non-fatal: if anything fails, the bot still runs — topic-related
    code paths will simply no-op (the send_message fallback path
    preserves legacy single-thread behavior). Better than refusing to
    start; worst case the user sees log warnings and fixes perms.
    """
    if not _forum_enabled() or _FORUM_CHAT_ID is None:
        return
    try:
        chat = await app.bot.get_chat(_FORUM_CHAT_ID)
    except Exception:
        logger.exception(
            "Forum mode: getChat(%s) failed — topic features disabled",
            _FORUM_CHAT_ID,
        )
        return
    if not getattr(chat, "is_forum", False):
        logger.warning(
            "Forum mode: chat %s is not a forum. Enable Topics in the "
            "supergroup settings or unset TELE_CLAUDE_SUPERGROUP_ID.",
            _FORUM_CHAT_ID,
        )
        return
    try:
        me = await app.bot.get_me()
        member = await app.bot.get_chat_member(_FORUM_CHAT_ID, me.id)
    except Exception:
        logger.exception(
            "Forum mode: getChatMember failed — can't verify bot permissions",
        )
        return
    can_manage_topics = getattr(member, "can_manage_topics", None)
    can_delete_messages = getattr(member, "can_delete_messages", None)
    missing: list[str] = []
    if can_manage_topics is False:
        missing.append("can_manage_topics")
    if can_delete_messages is False:
        missing.append("can_delete_messages")
    if missing:
        logger.warning(
            "Forum mode: bot is missing admin perms %s — topic create/delete "
            "will fail until granted.",
            ", ".join(missing),
        )
    else:
        logger.info("Forum mode: enabled, chat=%s is_forum=True", _FORUM_CHAT_ID)


async def _publish_menu(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Publish built-in commands + user-defined Claude shortcuts to Telegram.

    Telegram restricts command names to [a-z0-9_]{1,32}, so any shortcut
    containing hyphens (e.g. ``using-superpowers``) is registered with the
    hyphens replaced by underscores (``using_superpowers``). The
    passthrough handler translates back to the canonical name before
    forwarding to the pane.
    """
    menu: list[BotCommand] = [BotCommand(name, desc) for name, desc, _ in _COMMANDS]
    for name, desc in sorted(state.get_claude_shortcuts().items()):
        alias = name.replace("-", "_").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,32}", alias):
            continue  # silently skip entries that can't be a Telegram bot command
        label = f"→ Claude /{name}"
        menu.append(BotCommand(alias, desc if desc else label))
    await app.bot.set_my_commands(menu)
    await _verify_forum_mode(app)


_register_menu = _publish_menu  # back-compat alias kept for existing call sites


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_publish_menu).build()
    for name, _desc, handler in _COMMANDS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Accept photos + any document (txt, md, pdf, code, logs, zips, …).
    # Explicitly NOT filters.ATTACHMENT because that would also forward
    # videos + audio + voice notes, which Claude can't do much with.
    # Dispatched to ``on_attachment`` which branches on type internally.
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_attachment))
    app.add_handler(MessageHandler(filters.COMMAND, on_slash_passthrough))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

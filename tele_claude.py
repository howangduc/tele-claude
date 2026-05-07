"""Telegram bot that forwards messages to Claude Code tmux panes.

Commands (single source of truth is ``_COMMANDS`` near the bottom):
  /panes             — tappable keyboard of Claude Code panes; tap to activate.
  /use %N            — set active pane without the picker.
  /which             — show the active pane.
  /pwd [%N]          — show pane's live working directory.
  /new [dir]         — spawn a fresh detached tmux session running claude; bare invocation prompts via ForceReply.
  /resume [dir]      — pick a previous Claude session (~/.claude/projects/) and spawn a pane running claude --resume <id>.
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
  resume:<id>     — spawn a new pane running `claude --resume <id>` (from /resume picker).
  voice:send|cancel|retry|swap — speech-to-text confirm card actions
    (key derived from the bot reply's chat+message id; transcript +
    target pane + audio path are loaded from the pending-voice cache).

Fallback for plain-text messages: resolve pane from a reply-to `%N`,
otherwise the active pane for that chat. Replies to "Args for /cmd?"
ForceReply prompts are dispatched back through the matching handler
(either a built-in like /new or a shortcut forward).
"""

from __future__ import annotations

import asyncio
import html as _html
import json
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

import tele_claude_constants as constants
import tele_claude_questions
import tele_claude_speech as speech
import tele_claude_state as state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["CLAUDE_TELEGRAM_BOT_TOKEN"]
CHAT_IDS: frozenset[int] = frozenset(
    int(chunk.strip())
    for chunk in os.environ["CLAUDE_TELEGRAM_CHAT_ID"].split(",")
    if chunk.strip()
)

# Inbound media destinations live in ``constants`` (env-overridable
# via TELE_CLAUDE_IMAGE_DIR / TELE_CLAUDE_FILE_DIR / TELE_CLAUDE_VOICE_DIR).
IMAGE_DIR = constants.IMAGE_DIR
FILE_DIR = constants.FILE_DIR
VOICE_DIR = constants.VOICE_DIR

# Filenames coming from Telegram may contain path separators or shell
# metacharacters — neutralise before we write to disk. Keeps letters,
# digits, dot, dash, underscore; collapses everything else to underscore.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Forum mode: when set, the bot runs inside a supergroup that has
# Topics enabled and each Claude pane gets its own topic. Value is
# the supergroup id (negative int, e.g. -1001234567890). Resolved
# in ``constants`` from TELE_CLAUDE_SUPERGROUP_ID.
_FORUM_CHAT_ID: int | None = constants.FORUM_CHAT_ID

_PANE_RE = re.compile(r"(?<!\w)%\d+(?!\w)")

# Built-in bot commands that should NEVER be forwarded to a pane
# (checked by the slash-passthrough handler to avoid double-processing).
_BUILTIN_COMMANDS = {
    "panes",
    "use",
    "which",
    "pwd",
    "new",
    "resume",
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

# Alias to ``constants.ARGS_PROMPT_PREFIX`` — the reply handler
# detects "waiting for args" prompts by matching reply_to_message.text
# against this prefix, then extracts the canonical command name.
_ARGS_PROMPT_PREFIX = constants.ARGS_PROMPT_PREFIX

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

    Local patch: macOS Claude Code overrides its process name to its
    version (e.g. ``2.1.119``), so the original ``#{m:*claude*,#{pane_current_command}}``
    filter misses it. We list panes unfiltered, then walk each pane's
    pid subtree and match against the full argv via ``ps -o command``.
    """
    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_title}",
        ],
        capture_output=True,
        text=True,
    )
    ps_proc = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
    )
    children: dict[int, list[int]] = {}
    cmdline: dict[int, str] = {}
    for raw in ps_proc.stdout.splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        cmdline[pid] = parts[2]
        children.setdefault(ppid, []).append(pid)

    def _subtree_has_claude(root: int) -> bool:
        stack = [root]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if "claude" in cmdline.get(pid, "").lower():
                return True
            stack.extend(children.get(pid, []))
        return False

    panes: list[tuple[str, str, str]] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pane_id, pane_pid_s, path, title = parts[0], parts[1], parts[2], parts[3]
        try:
            pane_pid = int(pane_pid_s)
        except ValueError:
            continue
        if pane_id and _subtree_has_claude(pane_pid):
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
            ["tmux", "load-buffer", "-b", constants.TMUX_PASTE_BUFFER, "-"],
            input=text,
            text=True,
            check=True,
        )
        _ = subprocess.run(
            [
                "tmux",
                "paste-buffer",
                "-b",
                constants.TMUX_PASTE_BUFFER,
                "-t",
                pane_id,
                "-d",
            ],
            check=True,
        )
        time.sleep(constants.PASTE_SETTLE_DELAY)
    else:
        _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], check=True)
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)
    # Any successful send-via-bot is an implicit subscribe — the user has
    # clearly opted this pane into the Telegram conversation loop.
    state.subscribe_pane(pane_id)


def _send_key(pane_id: str, key: str) -> None:
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, key], check=True)
    state.subscribe_pane(pane_id)


# Claude Code shows one of these spinner verbs (or "esc to interrupt")
# while a turn is in flight. tmux capture-pane in that window contains
# the spinner; an idle pane shows the empty `❯ ` prompt and the bottom
# status bar without the spinner. We use this to gate `!cmd` forwards
# in on_message — keystrokes that arrive mid-turn don't go through
# Claude Code's `!`-REPL handler, so the bash output never lands in
# the transcript and there's nothing for the forwarder to surface.
_CLAUDE_BUSY_SIGNALS = (
    "esc to interrupt",
    "Beaming",
    "Brewing",
    "Cooking",
    "Composing",
    "Crafting",
    "Crunching",
    "Forging",
    "Generating",
    "Mulling",
    "Noodling",
    "Percolating",
    "Pondering",
    "Riffing",
    "Simmering",
    "Smelting",
    "Spinning",
    "Steaming",
    "Stirring",
    "Synthesizing",
    "Thinking",
    "Vibing",
    "Weaving",
    "Whipping",
    "Working",
    "Wrangling",
)


def _pane_is_busy(pane_id: str) -> bool:
    """Heuristic: is Claude Code mid-turn in this pane?

    Captures the last ~10 lines and looks for spinner verbs or the
    `esc to interrupt` instruction Claude prints during turns. False
    on capture failure — we'd rather forward and risk a no-op than
    silently drop the user's message.
    """
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-10"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False
    return any(signal in out for signal in _CLAUDE_BUSY_SIGNALS)


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
    cwd_part = _truncate_middle(cwd, constants.TOPIC_CWD_MAX)
    segments = [pane_id]
    if title_part:
        segments.append(title_part)
    if cwd_part:
        segments.append(cwd_part)
    name = " · ".join(segments)
    if len(name) > constants.TOPIC_NAME_MAX:
        name = name[: constants.TOPIC_NAME_MAX - 1] + "…"
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


async def _drop_dead_topic_mappings(
    app: Application[Any, Any, Any, Any, Any, Any], pane_ids: set[str]
) -> None:
    """Pop ``pane_topics`` entries whose topic the user deleted in Telegram.

    Validates each cached topic_id by issuing a no-op ``editForumTopic``
    with the already-cached name. A "thread not found" / "TOPIC_DELETED"
    response means the user deleted the topic from the Telegram side
    while the bot wasn't watching — drop the mapping so the create-loop
    in ``cmd_panes`` rebuilds a fresh topic for the same (still-alive)
    pane (issue #16).

    Subscription / mute / active-pane state is deliberately left alone —
    losing the topic does NOT mean the user wanted to stop receiving
    output from this pane.
    """
    if not _forum_enabled() or _FORUM_CHAT_ID is None:
        return
    for pane_id in pane_ids:
        thread_id = state.get_topic(pane_id)
        if thread_id is None:
            continue
        cached_name = state.get_cached_topic_name(pane_id) or pane_id
        try:
            _ = await app.bot.edit_forum_topic(
                chat_id=_FORUM_CHAT_ID,
                message_thread_id=thread_id,
                name=cached_name,
            )
            continue  # topic still alive
        except Exception as exc:
            err = str(exc)
            if "thread" in err.lower() or "TOPIC" in err:
                _ = state.pop_topic(pane_id)
                logger.info(
                    "Dropped stale topic mapping for %s (thread_id=%d): %s",
                    pane_id,
                    thread_id,
                    err,
                )
            else:
                # Some other error (rate limit, permissions, …) — leave
                # the mapping and let the next /panes try again.
                logger.warning(
                    "Topic-validation editForumTopic failed for %s "
                    "(thread_id=%d), keeping mapping: %s",
                    pane_id,
                    thread_id,
                    err,
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
    # Drop any cached topic mapping whose topic the user deleted on the
    # Telegram side. Without this, ``_ensure_topic_for_pane`` below
    # short-circuits on the stale thread_id and the pane never gets a
    # fresh topic — leaving it functionally orphaned (issue #16). The
    # pane's subscription is intentionally untouched here.
    if _forum_enabled():
        await _drop_dead_topic_mappings(context.application, alive_ids)
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


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick the permission mode for newly-spawned panes (issue #27).

    Bare ``/mode`` shows a 4-button picker with the active mode marked.
    ``/mode <name>`` sets directly without the picker. Subsequent
    ``/new`` and ``/resume`` panes spawn with the chosen mode.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    active = state.get_permission_mode()
    logger.info("cmd_mode invoked: args=%r active=%r", args, active)

    if args:
        chosen = args[0].strip()
        if chosen not in constants.PERMISSION_MODES:
            valid = ", ".join(constants.PERMISSION_MODES.keys())
            _ = await message.reply_text(
                f"Unknown mode <code>{_html.escape(chosen)}</code>. "
                f"Valid: <code>{_html.escape(valid)}</code>",
                parse_mode="HTML",
            )
            return
        state.set_permission_mode(chosen)
        logger.info("cmd_mode: %r → %r", active, chosen)
        _ = await message.reply_text(
            f"🛡 Permission mode → <code>{_html.escape(chosen)}</code>. "
            f"Applies to subsequent /new and /resume panes.",
            parse_mode="HTML",
        )
        return

    # Picker mode: one row per option, mark the active one with ✓.
    rows: list[list[InlineKeyboardButton]] = []
    for name in constants.PERMISSION_MODES:
        marker = "✓ " if name == active else "  "
        label = f"{marker}{name}"
        if name == "bypass":
            label += " ⚠️"
        rows.append([InlineKeyboardButton(label, callback_data=f"mode:{name}")])

    _ = await message.reply_text(
        f"🛡 <b>Permission mode</b> · current: <code>{_html.escape(active)}</code>\n\n"
        "Tap to change. Applies to subsequent /new and /resume panes.\n\n"
        "<i>default</i> — normal permission prompts (1/2/3 keyboard).\n"
        "<i>acceptEdits</i> — auto-accept edits, prompt other tools.\n"
        "<i>plan</i> — planning-only; no Edit/Write/Bash without approval.\n"
        "<i>bypass ⚠️</i> — <code>--dangerously-skip-permissions</code> (legacy default).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_pinned(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the TodoWrite pinned-card feature (issue #28).

    Bare ``/pinned`` shows current state with on/off buttons.
    ``/pinned on`` and ``/pinned off`` set directly. Affects all
    panes — when off, the bot stops pinning AND stops editing any
    existing pinned cards. When flipped back on, the next TodoWrite
    fire creates a fresh pinned card per pane.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    enabled = state.get_todowrite_pinned_enabled()
    logger.info("cmd_pinned invoked: args=%r enabled=%r", args, enabled)

    if args:
        chosen = args[0].strip().lower()
        if chosen not in ("on", "off"):
            _ = await message.reply_text(
                "Usage: <code>/pinned on</code> or <code>/pinned off</code>",
                parse_mode="HTML",
            )
            return
        new_value = chosen == "on"
        state.set_todowrite_pinned_enabled(new_value)
        logger.info("cmd_pinned: %r → %r", enabled, new_value)
        _ = await message.reply_text(
            f"📌 Pinned TodoWrite cards: <b>{'on' if new_value else 'off'}</b>",
            parse_mode="HTML",
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                ("✓ on" if enabled else "  on"), callback_data="pinned:on"
            ),
            InlineKeyboardButton(
                ("✓ off" if not enabled else "  off"), callback_data="pinned:off"
            ),
        ]
    ]
    _ = await message.reply_text(
        f"📌 <b>TodoWrite pinned cards</b> · current: "
        f"<b>{'on' if enabled else 'off'}</b>\n\n"
        "When <i>on</i>, the bot pins Claude's TodoWrite list per pane "
        "and edits it in place as the agent updates. When <i>off</i>, "
        "no pins created and no existing pins touched.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_which(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    # In a forum topic, "which pane?" = the pane that owns this topic.
    # Outside a topic, fall back to the per-chat active pane.
    mode = state.get_permission_mode()
    mode_suffix = f" · mode <code>{_html.escape(mode)}</code>"
    topic_pane = _pane_from_thread(message)
    if topic_pane:
        _ = await message.reply_text(
            f"Active: <code>{_html.escape(topic_pane)}</code> (from this topic){mode_suffix}",
            parse_mode="HTML",
        )
        return
    current = state.get_active_pane(message.chat_id)
    if current:
        _ = await message.reply_text(
            f"Active: <code>{_html.escape(current)}</code>{mode_suffix}",
            parse_mode="HTML",
        )
    else:
        _ = await message.reply_text(
            f"No active pane. Use /panes or /use %N.{mode_suffix}",
            parse_mode="HTML",
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
    candidate = f"{constants.TMUX_SESSION_PREFIX}{safe}"
    i = 2
    while candidate in existing:
        candidate = f"{constants.TMUX_SESSION_PREFIX}{safe}-{i}"
        i += 1
    return candidate


# Claude Code prints this on first-time entry into an unfamiliar
# directory — a startup security gate distinct from any tool-permission
# notification, so no Notification hook fires and the bot has no
# inline-keyboard event to surface. _wait_for_claude_ready detects it
# in the pane capture and (when TELE_CLAUDE_AUTO_TRUST is truthy) sends
# a "1\n" keystroke to accept it, then keeps polling for the real ❯
# prompt. Without auto-trust the spawn ack times out at ⏳ Still booting
# and the user must type "1" via Telegram to unstick.
_TRUST_FOLDER_MARKER = "Yes, I trust this folder"


def _auto_trust_enabled() -> bool:
    return constants.env_truthy("TELE_CLAUDE_AUTO_TRUST")


def _wait_for_claude_ready(
    pane_id: str, max_wait_seconds: float = 6.0, poll_seconds: float = 0.3
) -> bool:
    """Poll the pane until Claude Code's idle prompt (``❯``) appears.

    Claude's TUI eats keystrokes that arrive during banner draw / TTY
    init — so a fresh ``/new`` whose user types immediately loses the
    first message (issue #11). This blocks the spawn handler briefly
    until the prompt char is visible, then returns True. Timeout
    returns False — caller decides whether to warn the user.

    When ``TELE_CLAUDE_AUTO_TRUST`` is truthy and the capture shows the
    "Yes, I trust this folder" gate, sends "1\\n" once and keeps
    polling — saves the user from typing it via Telegram on every fresh
    directory.
    """
    auto_trusted = False
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", "-30"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError:
            return False
        if "❯" in out:
            return True
        if (
            not auto_trusted
            and _TRUST_FOLDER_MARKER in out
            and _auto_trust_enabled()
        ):
            logger.info(
                "auto-trust: detected trust-folder gate on %s, sending '1'",
                pane_id,
            )
            try:
                _send_to_tmux(pane_id, "1")
            except subprocess.CalledProcessError:
                logger.exception("auto-trust send failed for %s", pane_id)
            # Set the flag regardless — one attempt; if it didn't take,
            # let the user finish manually. Avoids a key-press loop.
            auto_trusted = True
        time.sleep(poll_seconds)
    return False


def _resolve_launch_cmd(mode: str | None = None) -> str:
    """Compute the effective LAUNCH_CMD for a new pane, honouring the
    selected permission mode (issue #27).

    When ``TELE_CLAUDE_NEW_LAUNCH_CMD`` is set, that env var wins —
    we don't try to rewrite a user-provided wrapper. Otherwise we
    swap the ``--dangerously-skip-permissions`` flag (or its absence)
    for the picked mode's ``--permission-mode <value>``. ``mode=None``
    falls back to ``state.get_permission_mode()``.

    Bypass mode keeps the legacy ``--dangerously-skip-permissions``
    flag verbatim — that's literally the 0.1.x default and we don't
    want to silently change semantics for users who pick it.
    """
    # Power-user env override always wins. Don't second-guess it.
    if "TELE_CLAUDE_NEW_LAUNCH_CMD" in os.environ:
        cmd = constants.LAUNCH_CMD
        logger.info("resolve_launch_cmd: env-override → %r", cmd)
        return cmd
    name = mode or state.get_permission_mode()
    flag_value = constants.PERMISSION_MODES.get(name)
    if name == "bypass" or flag_value is None:
        # Legacy default, unchanged. Also catches an unknown mode
        # smuggled in via a hand-edited state file — falls back to
        # bypass behaviour rather than emitting an invalid flag.
        cmd = f"{constants._BASE_LAUNCH_CMD} --dangerously-skip-permissions"
        logger.info("resolve_launch_cmd: mode=%r → %r", name, cmd)
        return cmd
    cmd = f"{constants._BASE_LAUNCH_CMD} --permission-mode {flag_value}"
    logger.info("resolve_launch_cmd: mode=%r → %r", name, cmd)
    return cmd


async def _spawn_new_pane(
    message: Message,
    cwd_arg: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    launch_cmd: str | None = None,
    initial_topic_title: str = "🚀 starting",
) -> None:
    """Spawn a fresh detached tmux session running a Claude launch command and subscribe it.

    Each ``/new`` gets its own session (not just a window) so concurrent
    Claude tasks stay isolated — independent scrollback, single
    ``tmux kill-session`` cleanup, and no yanking the user's current
    client to a new window. The new session is detached so the user's
    attached terminal keeps doing whatever it was doing; they
    ``tmux attach -t <name>`` when they want to see it directly.

    When ``context`` is provided AND forum mode is active, also creates
    the pane's topic and posts the spawn confirmation INTO that topic
    so the user lands directly in the chat thread for the new pane
    (issue #11). Without ``context`` the legacy single-thread reply
    path is used.

    ``launch_cmd`` defaults to ``constants.LAUNCH_CMD``. ``/resume``
    overrides it with ``"<base> --resume <session-id>"`` so the same
    spawn machinery powers both fresh and resumed sessions.
    """
    # If the caller didn't pass a launch_cmd, derive it from the
    # active /mode setting (issue #27). /resume passes its own
    # ``--resume <id>`` form, so this only fires for /new spawns.
    effective_launch_cmd = launch_cmd or _resolve_launch_cmd()
    logger.info(
        "spawn_new_pane: cwd_arg=%r launch_cmd=%r", cwd_arg, effective_launch_cmd
    )
    cwd = os.path.expanduser(cwd_arg) if cwd_arg else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        logger.info("spawn_new_pane: directory not found: %s", cwd)
        _ = await message.reply_text(
            f"Directory not found: <code>{_html.escape(cwd)}</code>",
            parse_mode="HTML",
        )
        return

    session_name = _pick_session_name(cwd)
    logger.info("spawn_new_pane: creating session=%r cwd=%s", session_name, cwd)
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
        logger.exception("spawn_new_pane: tmux new-session failed")
        _ = await message.reply_text(f"Failed to create session: {e.stderr or e}")
        return
    new_pane = created.stdout.strip()
    if not new_pane:
        logger.error("spawn_new_pane: tmux returned empty pane id")
        _ = await message.reply_text("tmux didn't return a pane id.")
        return
    logger.info(
        "spawn_new_pane: created pane %s in session %s, launching %r",
        new_pane,
        session_name,
        effective_launch_cmd,
    )

    time.sleep(constants.SPAWN_SETTLE_DELAY)
    # ``-l`` (literal) so ``=`` and spaces in LAUNCH_CMD are typed
    # verbatim instead of being parsed as tmux key names. Enter is
    # sent as a separate key sequence (no ``-l``) to actually submit.
    _ = subprocess.run(
        ["tmux", "send-keys", "-t", new_pane, "-l", effective_launch_cmd],
        check=True,
    )
    _ = subprocess.run(
        ["tmux", "send-keys", "-t", new_pane, "Enter"],
        check=True,
    )

    state.subscribe_pane(new_pane)
    state.set_active_pane(message.chat_id, new_pane)

    # Wait for Claude TUI to finish init before declaring success.
    # Without this, the user's first message in the new topic races
    # against Claude's banner draw and gets eaten (issue #11).
    ready = _wait_for_claude_ready(new_pane)

    # Create the forum topic up-front so the user has somewhere to
    # land. Without this, /new only writes a chat-level confirmation
    # and the user must run /panes manually to materialise the topic.
    thread_id: int | None = None
    if context is not None:
        thread_id = await _ensure_topic_for_pane(
            context.application, new_pane, pane_title=initial_topic_title, cwd=cwd
        )

    short_cwd = cwd.replace(os.path.expanduser("~"), "~")
    ready_line = (
        "🟢 <i>Claude is ready — chat away.</i>"
        if ready
        else "⏳ <i>Still booting — give it a few seconds before chatting.</i>"
    )
    body = (
        f"✅ Spawned <code>{_html.escape(new_pane)}</code> in "
        f"<code>{_html.escape(short_cwd)}</code>\n"
        f"New session <code>{_html.escape(session_name)}</code> (detached) · "
        f"Launched <code>{_html.escape(effective_launch_cmd)}</code> · active + subscribed 🔔\n"
        f"Attach: <code>tmux attach -t {_html.escape(session_name)}</code>\n"
        f"{ready_line}"
    )
    # Send INTO the new topic when we created one. Telegram opens this
    # topic on tap, so the user lands directly in the chat thread for
    # the pane they just spawned. Falls back to legacy single-thread
    # reply when forum mode is off / context not threaded through.
    _ = await message.reply_text(body, parse_mode="HTML", message_thread_id=thread_id)


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
        await _spawn_new_pane(message, args[0], context)
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


# ---------- /resume ----------

# Cap on sessions shown by /resume. Telegram inline keyboards allow
# more buttons but rendering 12 single-column rows already eats most of
# a phone screen — past that, the user wants /resume <dir> to narrow.
RESUME_LIST_MAX = 12

# Width budget for the first-prompt preview on a /resume button. Each
# button label is ``<basename> · <age> · <preview>``; total label cap
# is ~60 chars per Telegram practical limit.
RESUME_PREVIEW_MAX = 25

# Strips XML-ish tags so Claude's <command-name>/<command-message>
# wrappers (used for slash-command transcripts) collapse to readable
# text — otherwise every /caveman session shows ``<command-message>...``
# on its button.
_RESUME_TAG_RE = re.compile(r"<[^>]+>")
_RESUME_WS_RE = re.compile(r"\s+")


def _decode_project_dir(encoded: str) -> str:
    """Map ``-home-hainm-foo`` back to ``/home/hainm/foo``.

    Best-effort decode for cases where the JSONL ``cwd`` field is
    unavailable. Real cwds containing ``-`` are ambiguous (the encoding
    is lossy) — for accurate paths we read ``cwd`` from the JSONL itself
    in ``_list_recent_sessions``.
    """
    return "/" + encoded.lstrip("-").replace("-", "/")


def _read_first_prompt(path: Path) -> tuple[str, str]:
    """Return (cwd, first-user-prompt-preview) for a session JSONL.

    Iterates the file once: grabs the first ``cwd`` field encountered
    (any line type carries it) and the first ``user``-role message
    whose content is plain text — i.e. an actual user-typed prompt,
    not a tool-result echo. Falls back to empty preview if the file
    is empty / corrupt / contains only tool_result entries.
    """
    cwd = ""
    preview = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not cwd:
                    candidate = entry.get("cwd")
                    if isinstance(candidate, str) and candidate:
                        cwd = candidate
                if preview:
                    if cwd:
                        break
                    continue
                if entry.get("type") != "user":
                    continue
                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                text: str | None = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for chunk in content:
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("type") == "text":
                            t = chunk.get("text")
                            if isinstance(t, str):
                                text = t
                                break
                if not text:
                    continue
                # Collapse XML tags + whitespace so /command-name and
                # multi-line prompts render compactly on a button.
                stripped = _RESUME_TAG_RE.sub(" ", text)
                stripped = _RESUME_WS_RE.sub(" ", stripped).strip()
                if stripped:
                    preview = stripped
    except OSError:
        return cwd, preview
    return cwd, preview


def _format_age(seconds: float) -> str:
    """Compact age string (``5m``, ``2h``, ``3d``, ``2w``)."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 86400 * 14:
        return f"{int(seconds // 86400)}d"
    return f"{int(seconds // (86400 * 7))}w"


def _list_recent_sessions(
    filter_dir: str | None = None, limit: int = RESUME_LIST_MAX
) -> list[tuple[str, str, float, str]]:
    """Walk ``~/.claude/projects`` and return recent session metadata.

    Returns ``(session_id, cwd, age_seconds, first_prompt_preview)``
    tuples sorted newest-first. Sessions whose ``cwd`` cannot be
    determined fall back to the decoded project-dir name.

    ``filter_dir`` (when set) restricts results to sessions whose
    resolved ``cwd`` equals or sits inside the given directory —
    matched against the absolute, ``~``-expanded form so users can
    type ``~/foo``, ``./foo`` or ``/abs/path`` interchangeably.
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return []

    target_abs: str | None = None
    if filter_dir:
        target_abs = os.path.realpath(os.path.expanduser(filter_dir))

    candidates: list[tuple[float, Path, str]] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        for session_path in project_dir.glob("*.jsonl"):
            try:
                mtime = session_path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, session_path, project_dir.name))

    # Sort newest-first; cap the work to the recent slice plus a small
    # buffer so a heavy filter still finds enough hits without parsing
    # every session on disk.
    candidates.sort(key=lambda t: t[0], reverse=True)
    scan_cap = limit * 6 if filter_dir else limit * 2
    candidates = candidates[:scan_cap]

    now = time.time()
    out: list[tuple[str, str, float, str]] = []
    for mtime, session_path, project_name in candidates:
        cwd, preview = _read_first_prompt(session_path)
        if not cwd:
            cwd = _decode_project_dir(project_name)
        if target_abs is not None:
            cwd_abs = os.path.realpath(cwd)
            if cwd_abs != target_abs and not cwd_abs.startswith(target_abs + os.sep):
                continue
        out.append((session_path.stem, cwd, max(0.0, now - mtime), preview))
        if len(out) >= limit:
            break
    return out


def _truncate_preview(text: str, width: int = RESUME_PREVIEW_MAX) -> str:
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)].rstrip() + "…"


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a previous Claude session and resume it in a fresh pane.

    Usage:
      /resume          → list up to 12 most-recent sessions across all
                          ``~/.claude/projects/*/``.
      /resume <dir>    → narrow to sessions whose recorded cwd sits
                          under <dir> (handy when several repos share
                          basenames).

    Tap a session button → bot spawns a new tmux pane in that session's
    cwd running ``<launch-cmd> --resume <session-id>`` and auto-subscribes
    it (same flow as /new).
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    args = list(context.args or [])
    filter_dir = args[0] if args else None
    sessions = _list_recent_sessions(filter_dir=filter_dir)
    if not sessions:
        if filter_dir:
            _ = await message.reply_text(
                f"No Claude sessions found under <code>{_html.escape(filter_dir)}</code>.",
                parse_mode="HTML",
            )
        else:
            _ = await message.reply_text(
                "No Claude sessions found in <code>~/.claude/projects/</code>.",
                parse_mode="HTML",
            )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for session_id, cwd, age, preview in sessions:
        basename = os.path.basename(cwd.rstrip("/")) or "~"
        preview_display = _truncate_preview(preview) if preview else "<no prompt>"
        label = f"{basename} · {_format_age(age)} · {preview_display}"
        # Telegram caps button labels around 64 chars; trim defensively.
        rows.append(
            [InlineKeyboardButton(label[:60], callback_data=f"resume:{session_id}")]
        )

    header_lines = ["Pick a session to resume (newest first):"]
    if filter_dir:
        header_lines.append(f"Filter: <code>{_html.escape(filter_dir)}</code>")
    header_lines.append(f"Showing {len(sessions)} (cap {RESUME_LIST_MAX}).")
    _ = await message.reply_text(
        "\n".join(header_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _find_session_path(session_id: str) -> Path | None:
    """Locate the JSONL for a session_id (no enforced project layout).

    Glob across ``~/.claude/projects/*/<id>.jsonl`` rather than trusting
    a cached cwd — the session may have been opened from multiple
    aliased paths and we want whichever one is on disk now.
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return None
    matches = list(projects_root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _pane_session_cwd(pane_id: str) -> str | None:
    """Return the Claude session's recorded cwd for ``pane_id``, or None.

    Reads the pane's tracked transcript JSONL (set by the hooks) and
    returns the first ``cwd`` field. Used by ``/get`` to resolve
    relative paths against the project Claude is actually working in,
    not the TUI's launch directory which is what
    ``#{pane_current_path}`` would report. (#36)
    """
    transcript_path = state.get_pane_transcript(pane_id)
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _find_live_pane_for_session(session_id: str) -> str | None:
    """Return a live Claude pane currently bound to ``session_id``, else None.

    Two detection paths in priority order:

    1. **Hook-recorded mapping.** Whenever a hook fires from a pane,
       ``state.set_pane_transcript(pane_id, transcript_path)`` records
       the JSONL Claude is writing. The session id is the file's stem,
       so a reverse lookup is exact + cheap. Catches the common case
       (any pane that's done at least one turn since the bot started).

    2. **/proc/<pane_pid>/cmdline scan.** For panes that have never
       fired a hook (fresh pane, no turn yet, or hooks-disabled), read
       the foreground process's argv and look for ``--resume <id>``.
       Linux-only; quietly skipped on macOS or if ``pane_pid`` is
       unobtainable.

    Without this guard, a user picking a session from ``/resume`` that
    is already live in pane %N spawns a SECOND pane running the same
    ``claude --resume <id>``. Two processes append to the same JSONL,
    hooks race, and the user sees interleaved output (issue #23).
    """
    panes = _list_claude_panes()
    for pane_id, _cmd, _title in panes:
        recorded = state.get_pane_transcript(pane_id)
        if recorded:
            try:
                if Path(recorded).stem == session_id:
                    return pane_id
            except (OSError, ValueError):
                pass
    # Fallback: process-args scan for panes with no hook history yet.
    needle = f"--resume {session_id}"
    for pane_id, _cmd, _title in panes:
        try:
            pid = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            continue
        if not pid:
            continue
        try:
            cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, FileNotFoundError):
            continue
        # /proc/<pid>/cmdline is null-separated argv; flatten and substring-match.
        cmdline = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if needle in cmdline:
            return pane_id
    return None


async def _spawn_resume_pane(
    message: Message, session_id: str, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Resolve session cwd and spawn a pane running ``--resume <id>``.

    Returns True on success so the caller can answer the callback with
    the appropriate toast. Errors surface as user-visible reply text.
    """
    path = _find_session_path(session_id)
    if path is None:
        _ = await message.reply_text(
            f"Session <code>{_html.escape(session_id)}</code> no longer on disk.",
            parse_mode="HTML",
        )
        return False

    # If this session is already live in another pane, re-bind the
    # topic to that pane instead of spawning a duplicate. Two panes
    # writing to the same JSONL produces interleaved output and dual
    # hook fires — see issue #23.
    existing_pane = _find_live_pane_for_session(session_id)
    if existing_pane is not None:
        state.set_active_pane(message.chat_id, existing_pane)
        state.subscribe_pane(existing_pane)
        thread_id = await _ensure_topic_for_pane(
            context.application,
            existing_pane,
            pane_title=f"↩ resume {session_id[:8]}",
        )
        _ = await message.reply_text(
            f"↩ Already live in <code>{_html.escape(existing_pane)}</code> — "
            f"re-bound topic + active. Send messages here.",
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
        return True

    cwd, _preview = _read_first_prompt(path)
    if not cwd:
        cwd = _decode_project_dir(path.parent.name)
    # ``--resume <id>`` is appended literally — keeping the rest of
    # LAUNCH_CMD (the ``TELE_CLAUDE=1`` opt-in + flags) intact so hooks
    # still fire on the resumed pane.
    # Resume honours the active /mode too — append --resume after the
    # mode-aware base (issue #27). Env-var override path returns the
    # raw LAUNCH_CMD as before.
    launch_cmd = f"{_resolve_launch_cmd()} --resume {session_id}"
    short_id = session_id[:8]
    await _spawn_new_pane(
        message,
        cwd,
        context=context,
        launch_cmd=launch_cmd,
        initial_topic_title=f"↩ resume {short_id}",
    )
    return True


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
        # Prefer the Claude session's recorded cwd over the pane's
        # foreground-process cwd. When the pane is running the Claude
        # TUI, ``#{pane_current_path}`` returns the TUI's launch dir,
        # not the project dir Claude is working in — so relative
        # paths like ``tele-claude/report.md`` resolve against the
        # parent of the project and 404. The session transcript
        # JSONL records the true cwd Claude sees. (#36)
        base = _pane_session_cwd(pane) or ""
        if not base:
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
    lines = constants.HISTORY_DEFAULT_LINES
    for arg in context.args or []:
        if arg.startswith("%"):
            pane_id = _normalise_pane(arg)
        elif arg.isdigit():
            lines = max(1, min(int(arg), constants.HISTORY_MAX_LINES))
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
    if len(body) > constants.HISTORY_BODY_TRIM:
        body = "…\n" + body[-constants.HISTORY_BODY_TRIM :]
    safe = _html.escape(body, quote=False)
    _ = await message.reply_text(
        f"<b>{pane_id}</b> · last {lines} lines\n<pre>{safe}</pre>",
        parse_mode="HTML",
    )


# ---------- Callback buttons ----------


def _coerce_int(value: object, default: int = 0) -> int:
    """Best-effort ``int(value)`` for state-dict reads that come back as
    ``object`` from JSON. Returns ``default`` on anything unparseable.
    Centralises the cast so basedpyright doesn't complain at every site
    that pulls ``current_idx`` / ``total`` out of a pending payload.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _rows_dict_to_markup(
    rows: list[list[dict[str, Any]]],
) -> InlineKeyboardMarkup:
    """Convert ``tele_claude_questions``-style raw row dicts to a PTB
    ``InlineKeyboardMarkup``. The helpers in that module return dicts so
    they can be reused by the hook side (which serialises straight to
    Telegram's HTTP API); the bot side wraps them into the typed PTB
    objects here.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                for b in row
            ]
            for row in rows
        ]
    )


async def _send_next_question(
    context: ContextTypes.DEFAULT_TYPE, message: Message, pane_id: str
) -> bool:
    """Advance the pending-questions cursor for ``pane_id`` and send Q[next]
    as a fresh Telegram message in the same topic.

    Returns ``True`` when a follow-up question was sent. Returns
    ``False`` when no pending state exists, or the user just answered
    the LAST question (caller treats False as "TUI is on the review
    screen — emit the final Submit/Cancel keyboard").
    """
    payload = state.advance_pending_questions(pane_id)
    if payload is None:
        return False
    questions = payload.get("questions") or []
    try:
        idx = _coerce_int(payload.get("current_idx"))
        total = _coerce_int(payload.get("total"))
    except (TypeError, ValueError):
        state.clear_pending_questions(pane_id)
        return False
    if not isinstance(questions, list) or idx >= len(questions) or idx >= total:
        state.clear_pending_questions(pane_id)
        return False
    next_q = questions[idx]
    if not isinstance(next_q, dict):
        # Was a leak: malformed payload left dialog state alive
        # for 15 min TTL and zombie-cursor'd the next AUQ. (#43)
        state.clear_pending_questions(pane_id)
        return False
    body = tele_claude_questions.render_question_html(next_q, idx, total)
    rows_dict = tele_claude_questions.question_keyboard_rows(pane_id, next_q)
    if not rows_dict:
        # Same leak shape as above — empty rows_dict (e.g. options=[])
        # used to leave state alive. (#43)
        state.clear_pending_questions(pane_id)
        return False
    try:
        _ = await context.bot.send_message(
            chat_id=message.chat_id,
            text=body,
            parse_mode="HTML",
            reply_markup=_rows_dict_to_markup(rows_dict),
            message_thread_id=getattr(message, "message_thread_id", None),
        )
    except Exception:
        logger.exception("send_next_question: failed to send Q[%d]", idx)
        # Network/Telegram failure mid-dialog should also tear down
        # state so the user isn't stuck behind a half-rendered dialog
        # the next AUQ inherits. (#43)
        state.clear_pending_questions(pane_id)
        return False
    return True


async def _send_final_review_keyboard(
    context: ContextTypes.DEFAULT_TYPE, message: Message, pane_id: str
) -> None:
    """Emit the post-questions ``✅ Submit answers / ❌ Cancel`` keyboard.

    Used after the user has answered the LAST question of a multi-
    question AskUserQuestion chain. Claude's TUI is then on its review
    screen waiting for one more keystroke (digit ``1`` or ``2``) — this
    surfaces those choices as a fresh Telegram message in the same
    topic so the user never has to attach to tmux to finalise.
    """
    rows = [
        [
            InlineKeyboardButton(
                "✅ Submit answers", callback_data=f"mfin:{pane_id}:1"
            ),
            InlineKeyboardButton("❌ Cancel", callback_data=f"mfin:{pane_id}:2"),
        ]
    ]
    try:
        _ = await context.bot.send_message(
            chat_id=message.chat_id,
            text=(
                f"📝 <b>Review your answers</b> for "
                f"<code>{_html.escape(pane_id)}</code> · tap to finalise."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
            message_thread_id=getattr(message, "message_thread_id", None),
        )
    except Exception:
        logger.exception("send_final_review_keyboard failed")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    if data.startswith("mode:"):
        chosen = data[len("mode:"):]
        if chosen not in constants.PERMISSION_MODES:
            _ = await query.answer("Unknown mode", show_alert=True)
            return
        state.set_permission_mode(chosen)
        logger.info("mode: callback set permission_mode=%r", chosen)
        _ = await query.answer(f"🛡 → {chosen}")
        try:
            _ = await query.edit_message_text(
                f"🛡 Permission mode → <code>{_html.escape(chosen)}</code>\n\n"
                f"Applies to subsequent /new and /resume panes. "
                f"Existing panes keep whatever mode they spawned with.",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            # Telegram occasionally rejects edits on messages whose HTML
            # has become malformed by prior edits, or when the message
            # was deleted between dispatch and edit. State persisted +
            # toast already fired, so swallow rather than alarm the
            # user with a noisy retry. Matches the ans:/voice: pattern.
            pass
        return

    if data.startswith("pinned:"):
        chosen = data[len("pinned:"):]
        if chosen not in ("on", "off"):
            _ = await query.answer("Bad arg", show_alert=True)
            return
        new_value = chosen == "on"
        state.set_todowrite_pinned_enabled(new_value)
        logger.info("pinned: callback set enabled=%r", new_value)
        _ = await query.answer(f"📌 → {chosen}")
        try:
            _ = await query.edit_message_text(
                f"📌 Pinned TodoWrite cards: <b>{'on' if new_value else 'off'}</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            # Telegram occasionally rejects edits on messages whose
            # HTML has become malformed by prior edits, or when the
            # message was deleted between dispatch and edit. State
            # persisted + toast already fired, so swallow.
            pass
        return

    if data.startswith("ans:"):
        # Defensive: stale / forged callbacks like ``ans:%5`` (no
        # answer digit) used to raise ``ValueError`` on tuple unpack
        # and leave the Telegram spinner stuck. Mirrors the guard
        # pattern already used by ``mtg:`` and ``mfin:``. (#41)
        parts = data.split(":", 2)
        if len(parts) != 3:
            _ = await query.answer()
            return
        _, pane_id, answer = parts
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_to_tmux(pane_id, answer)
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
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
        # Multi-question AskUserQuestion advance. No-op for permission
        # prompts (Allow/Always/Deny), ExitPlanMode (Approve/Keep
        # planning), and single-question dialogs — pending_questions
        # state is only written by the hook for chains of length ≥ 2.
        pending = state.get_pending_questions(pane_id)
        if pending:
            try:
                idx = _coerce_int(pending.get("current_idx"))
                total = _coerce_int(pending.get("total"))
            except (TypeError, ValueError):
                state.clear_pending_questions(pane_id)
                return
            if idx < total - 1:
                _ = await _send_next_question(context, message, pane_id)
            else:
                # Last single-select answered → TUI advanced to review.
                state.clear_pending_questions(pane_id)
                await _send_final_review_keyboard(context, message, pane_id)
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
        # Defensive: forged ``mtg:%P:99:0`` callbacks used to draw 99
        # toggle buttons because n_options below fell back to ``idx``
        # without an upper bound. Validate idx ∈ [1, _MAX_OPTIONS]
        # before any tmux write. (#44)
        if idx < 1 or idx > tele_claude_questions._MAX_OPTIONS:
            _ = await query.answer()
            return
        # If the tapped option is a free-text "Type something" slot,
        # refuse the tap — sending the digit would jump the TUI to
        # text-input mode but Telegram has no inline-text input route
        # on inline keyboards.
        #
        # KNOWN LIMITATION: ``pending_questions`` state is only written
        # by the hook for multi-question chains (len(questions) > 1, see
        # tele_claude_hooks.py). A single multi-select question whose
        # last option is "Type something" still wedges the pane —
        # ``pending`` is None below and we fall through to send the
        # digit. Long-term: extend state-write to chain length 1 +
        # multi-select, OR move heuristic into the hook itself. (#46)
        pending = state.get_pending_questions(pane_id)
        if pending:
            try:
                cur = _coerce_int(pending.get("current_idx"))
                qs = pending.get("questions") or []
                if isinstance(qs, list) and 0 <= cur < len(qs):
                    cur_q = qs[cur]
                    opts = cur_q.get("options") if isinstance(cur_q, dict) else None
                    if isinstance(opts, list) and 0 < idx <= len(opts):
                        if tele_claude_questions.is_free_text_option(opts[idx - 1]):
                            _ = await query.answer(
                                "Free-text option — attach to pane to type your answer.",
                                show_alert=True,
                            )
                            return
            except (TypeError, ValueError):
                pass
        # Claude's multi-select TUI says "Enter to select · Tab/Arrow keys
        # to navigate" at the bottom. The digit alone JUMPS THE CURSOR to
        # option N but doesn't toggle it — Enter does the toggle of the
        # currently-focused option. So we need both keystrokes (in that
        # order) for "tap option N in Telegram" to actually flip its
        # checkbox in the TUI. Without the trailing Enter the user sees
        # their tap silently lost while Claude's pre-selected default
        # stays checked. Use ``_send_to_tmux`` here so the digit is sent
        # in literal mode (avoiding any tmux key-name interpretation)
        # and Enter is sent as a key event right after.
        try:
            _send_to_tmux(pane_id, idx_str)
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
        new_mask = mask ^ (1 << (idx - 1))
        # Pull the option count from the existing keyboard shape (total
        # rows minus the trailing Submit row) so we don't have to re-ship
        # the option list in every callback_data. Cap at _MAX_OPTIONS
        # regardless of source so a forged callback can't expand the
        # keyboard. (#44)
        current = message.reply_markup
        n_options = 0
        if current and current.inline_keyboard:
            n_options = max(0, len(current.inline_keyboard) - 1)
        if n_options == 0:
            # Keyboard absent (race or strip). Use the tapped idx as a
            # safe lower bound — it's already validated above, so worst
            # case we draw fewer rows than the original keyboard, never
            # more.
            n_options = idx
        n_options = min(n_options, tele_claude_questions._MAX_OPTIONS)
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
        # Multi-select "Submit" tap. In a single-question dialog this
        # advances the TUI from toggle mode to the "Review your answers
        # / 1. Submit answers / 2. Cancel" screen. In a multi-question
        # chain, the TUI instead jumps to the next question's tab — the
        # bot then renders that next question and drops this message's
        # keyboard so the user can't tap submit twice.
        pane_id = data[len("msub:") :]
        if not _pane_exists(pane_id):
            _ = await query.answer(f"{pane_id} gone", show_alert=True)
            return
        try:
            _send_key(pane_id, "Enter")
        except subprocess.CalledProcessError as e:
            _ = await query.answer(f"Failed: {e}", show_alert=True)
            return
        pending = state.get_pending_questions(pane_id)
        idx = total = 0
        if pending:
            idx = _coerce_int(pending.get("current_idx"))
            total = _coerce_int(pending.get("total"))
        if pending and idx < total - 1:
            # Not the last question — drop this kbd, send next.
            try:
                _ = await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            sent = await _send_next_question(context, message, pane_id)
            if not sent:
                # _send_next_question's error paths self-clear pending
                # state (#43). Surface that to the user so they aren't
                # left looking at a blank topic wondering what happened
                # — and so we don't fall through to the final review
                # card emission below, which would mis-imply success. (#47)
                _ = await query.answer(
                    "⚠️ Next question failed — dialog reset.",
                    show_alert=True,
                )
                return
            _ = await query.answer(f"Q {idx + 1}/{total} submitted")
            return
        # Last question (or no pending) — TUI is on review screen.
        # Swap THIS message's keyboard in place to the Submit/Cancel
        # pair (preserves scroll position better than a fresh message
        # for the common single-question case).
        if pending:
            state.clear_pending_questions(pane_id)
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
        # Defensive: if pending state somehow survived (e.g. user
        # manually navigated past the review screen in tmux), drop it
        # so the next AskUserQuestion call starts fresh.
        state.clear_pending_questions(pane_id)
        label = "Submitted" if choice == "1" else "Cancelled"
        _ = await query.answer(f"✅ {label} → {pane_id}", show_alert=True)
        return

    if data.startswith("qr:"):
        # Same guard shape as ``ans:`` — see #41.
        parts = data.split(":", 2)
        if len(parts) != 3:
            _ = await query.answer()
            return
        _, pane_id, text = parts
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

    if data.startswith("resume:"):
        # /resume picker → spawn a new pane running ``claude --resume <id>``.
        # Spawn work is slow (tmux + ready-poll), so answer the toast EARLY
        # before kicking it off — Telegram greys the spinner around 15s.
        # Buttons are dropped in place so the user can't double-tap into
        # two parallel resume panes.
        session_id = data[len("resume:") :]
        if not session_id:
            _ = await query.answer()
            return
        _ = await query.answer(f"↩ Resuming {session_id[:8]}…")
        try:
            _ = await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            _ = await _spawn_resume_pane(message, session_id, context)
        except Exception:
            logger.exception("resume: spawn failed for %s", session_id)
            try:
                _ = await message.reply_text(
                    f"Failed to resume <code>{_html.escape(session_id)}</code> — see logs.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    if data.startswith("voice:"):
        # Voice STT confirm/cancel/retry/swap. Pending state is keyed by
        # (chat_id, message_id) of the bot's reply (the card the buttons
        # are attached to) — derived from query.message, so callback_data
        # stays short.
        #
        # IMPORTANT: ``query.answer()`` MUST be called within ~15 s of the
        # tap or Telegram returns "Query is too old" and the user's
        # spinner never clears (looks like the button did nothing). So
        # in every branch we answer EARLY, before the slow work
        # (tmux subprocess, network edits, STT API calls).
        action = data[len("voice:") :]
        chat_id = message.chat_id
        msg_id = message.message_id
        pending = state.get_pending_voice(chat_id, msg_id)
        if pending is None:
            _ = await query.answer("Expired")
            try:
                _ = await query.edit_message_text("⌛ expired", reply_markup=None)
            except Exception:
                pass
            return

        pane_id = str(pending.get("target_pane") or "")
        audio_path_str = str(pending.get("audio_path") or "")
        transcript_obj = pending.get("transcript")
        provider = str(pending.get("provider") or constants.STT_PROVIDER_DEFAULT)

        if action == "send":
            if not isinstance(transcript_obj, str) or not transcript_obj:
                _ = await query.answer("No transcript yet", show_alert=True)
                return
            if not _pane_exists(pane_id):
                _ = await query.answer(f"{pane_id} gone", show_alert=True)
                return
            try:
                _send_to_tmux(pane_id, transcript_obj)
            except subprocess.CalledProcessError as e:
                _ = await query.answer(f"Failed: {e}", show_alert=True)
                return
            # Tmux send already succeeded — answer the spinner NOW so
            # the user gets immediate feedback even if the message edit
            # is slow. Cleanup work follows.
            _ = await query.answer(f"✅ Sent → {pane_id}")
            try:
                _ = await query.edit_message_text(
                    f"✅ Sent to {_html.escape(pane_id)}",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
            state.clear_pending_voice(chat_id, msg_id)
            try:
                Path(audio_path_str).unlink()
            except OSError:
                pass
            return

        if action == "cancel":
            _ = await query.answer("Cancelled")
            try:
                _ = await query.edit_message_text("❌ Cancelled", reply_markup=None)
            except Exception:
                pass
            state.clear_pending_voice(chat_id, msg_id)
            try:
                Path(audio_path_str).unlink()
            except OSError:
                pass
            return

        if action in {"retry", "swap"}:
            next_provider = (
                speech.other_provider(provider) if action == "swap" else provider
            )
            try:
                port = speech.get_stt_port(next_provider)
            except ValueError as e:
                _ = await query.answer("Misconfigured", show_alert=True)
                try:
                    _ = await query.edit_message_text(
                        f"❌ STT misconfigured: {e}", reply_markup=None
                    )
                except Exception:
                    pass
                return

            # STT call may take several seconds — answer the spinner
            # now (with a toast) so the keyboard goes back to interactive
            # while we wait for the provider's response.
            _ = await query.answer("Transcribing…")
            logger.info(
                "STT %s (%s) → pane %s: %s",
                action,
                next_provider,
                pane_id,
                audio_path_str,
            )
            try:
                new_transcript = await port.transcribe(Path(audio_path_str))
            except (speech.TranscriptionError, NotImplementedError) as e:
                pending["provider"] = next_provider
                state.set_pending_voice(chat_id, msg_id, pending)
                try:
                    _ = await query.edit_message_text(
                        f"❌ STT failed: {e}",
                        reply_markup=_voice_error_keyboard(),
                    )
                except Exception:
                    pass
                return

            pending["provider"] = next_provider
            pending["transcript"] = new_transcript
            state.set_pending_voice(chat_id, msg_id, pending)
            try:
                _ = await query.edit_message_text(
                    _voice_confirm_body(pane_id, new_transcript),
                    parse_mode="HTML",
                    reply_markup=_voice_confirm_keyboard(),
                )
            except Exception:
                pass
            return

        # Unknown voice:* action — just dismiss the spinner.
        _ = await query.answer()
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
    return safe[: constants.FILENAME_MAX_LEN]


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


# ---------- Voice notes (speech-to-text) ----------


def _voice_confirm_keyboard() -> InlineKeyboardMarkup:
    """Send / Cancel buttons shown on a successful transcription."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send", callback_data="voice:send"),
                InlineKeyboardButton("❌ Cancel", callback_data="voice:cancel"),
            ]
        ]
    )


def _voice_error_keyboard() -> InlineKeyboardMarkup:
    """Retry / Try-other-provider buttons shown on STT failure."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Retry", callback_data="voice:retry"),
                InlineKeyboardButton(
                    "🔁 Try other provider", callback_data="voice:swap"
                ),
            ]
        ]
    )


def _voice_confirm_body(pane_id: str, transcript: str) -> str:
    """HTML body for the success confirm card."""
    return (
        f"🎤 <b>Transcript ({_html.escape(pane_id)})</b>:\n"
        f"<code>{_html.escape(transcript)}</code>"
    )


async def on_voice(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice note and ask the user to confirm before forwarding.

    Flow:
      1. Auth + pane resolution (forum topic → pane, else active pane).
      2. Download .ogg into ``VOICE_DIR`` so the retry/swap buttons can
         re-run STT against the same file later.
      3. Run the active STT adapter; render success → confirm card or
         failure → error card with retry/swap buttons. Either way,
         persist a pending-voice file keyed by the bot's reply id so
         the callback handlers find context on tap.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    voice = message.voice
    if not voice:
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

    # Download the .ogg. Mirrors the IMAGE_DIR / FILE_DIR pattern so the
    # filename includes both message_id (collision safety) and Telegram's
    # file_unique_id (idempotency on re-delivery).
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = VOICE_DIR / f"tg_{message.message_id}_{voice.file_unique_id}.ogg"
    tg_file = await voice.get_file()
    _ = await tg_file.download_to_drive(str(audio_path))

    # Show an interim placeholder right away. STT can take several seconds;
    # a silent gap reads as "bot broken" on phones. We edit this same
    # message into the confirm card or the error card below — one chat
    # bubble per voice note instead of two.
    placeholder = await message.reply_text("🎤 Transcribing…")

    provider = (
        os.environ.get("TELE_CLAUDE_STT_PROVIDER", constants.STT_PROVIDER_DEFAULT)
    ).lower()

    # Misconfiguration is a startup-class error: no retry buttons would
    # help (retry hits the same broken config). Bail by morphing the
    # placeholder into a plain error message.
    try:
        port = speech.get_stt_port(provider)
    except ValueError as e:
        _ = await placeholder.edit_text(f"❌ STT misconfigured: {e}")
        try:
            audio_path.unlink()
        except OSError:
            pass
        return

    logger.info("STT (%s) → pane %s: %s", provider, pane_id, audio_path)
    try:
        transcript = await port.transcribe(audio_path)
    except (speech.TranscriptionError, NotImplementedError) as e:
        _ = await placeholder.edit_text(
            f"❌ STT failed: {e}",
            reply_markup=_voice_error_keyboard(),
        )
        state.set_pending_voice(
            placeholder.chat_id,
            placeholder.message_id,
            {
                "audio_path": str(audio_path),
                "target_pane": pane_id,
                "provider": provider,
                "transcript": None,
            },
        )
        return

    _ = await placeholder.edit_text(
        _voice_confirm_body(pane_id, transcript),
        parse_mode="HTML",
        reply_markup=_voice_confirm_keyboard(),
    )
    state.set_pending_voice(
        placeholder.chat_id,
        placeholder.message_id,
        {
            "audio_path": str(audio_path),
            "target_pane": pane_id,
            "provider": provider,
            "transcript": transcript,
        },
    )


# ---------- `!cmd` bash-output forwarder ----------
#
# Background: Claude Code's `!`-prefix runs a shell command locally
# (no LLM turn → no Stop, progress, notify, or post-tool-use hooks).
# The bot's reply path is hook-driven, so `!cmd` output normally
# never reaches Telegram. We compensate by intercepting the `!` prefix
# in on_message: forward to the pane as usual (so Claude sees it),
# then schedule a background task that polls the pane's transcript for
# the matching <bash-input>/<bash-stdout> entries Claude Code writes
# and replies to the user's message with the captured 🐚 block.

_BANG_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)
_BANG_BASH_STDOUT_RE = re.compile(r"<bash-stdout>(.*?)</bash-stdout>", re.DOTALL)
_BANG_BASH_STDERR_RE = re.compile(r"<bash-stderr>(.*?)</bash-stderr>", re.DOTALL)
_BANG_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BANG_FORWARD_TIMEOUT_SECONDS = 5.0
_BANG_FORWARD_POLL_INTERVAL = 0.4
_BANG_FORWARD_MAX_BLOCK_CHARS = 3500


def _read_bang_bash_block(transcript: Path, cmd: str) -> str | None:
    """Find the most recent <bash-input>cmd</bash-input> in ``transcript``
    and return a markdown-formatted ``$ cmd / stdout / [stderr]`` block.

    Returns None if either entry hasn't been flushed yet (caller polls)
    or if the entry has no useful content (e.g. ``!cd /tmp`` produces
    empty stdout + empty stderr — skip the noise)."""
    if not transcript.exists():
        return None
    try:
        with transcript.open() as f:
            lines = f.readlines()
    except OSError:
        return None
    cmd_norm = cmd.strip()
    input_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if "<bash-input>" not in lines[i]:
            continue
        try:
            entry = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        c = entry.get("message", {}).get("content")
        if not isinstance(c, str):
            continue
        m = _BANG_BASH_INPUT_RE.search(c)
        if m and m.group(1).strip() == cmd_norm:
            input_idx = i
            break
    if input_idx is None:
        return None
    # Output entry usually lands within 1-2 entries after input. Bound
    # the search so we don't tail the whole transcript on every miss.
    for j in range(input_idx + 1, min(input_idx + 6, len(lines))):
        if "<bash-stdout>" not in lines[j] and "<bash-stderr>" not in lines[j]:
            continue
        try:
            entry = json.loads(lines[j])
        except json.JSONDecodeError:
            continue
        c = entry.get("message", {}).get("content")
        if not isinstance(c, str):
            continue
        out_m = _BANG_BASH_STDOUT_RE.search(c)
        err_m = _BANG_BASH_STDERR_RE.search(c)
        sections: list[str] = [f"$ {cmd_norm}"]
        if out_m:
            body = _BANG_ANSI_RE.sub("", out_m.group(1)).rstrip()
            if body:
                sections.append(body)
        if err_m:
            err_body = _BANG_ANSI_RE.sub("", err_m.group(1)).rstrip()
            if err_body:
                sections.append(f"[stderr]\n{err_body}")
        if len(sections) == 1:
            # Only the prompt — no actual output. `!cd /tmp` style.
            return None
        block = "\n".join(sections)
        if len(block) > _BANG_FORWARD_MAX_BLOCK_CHARS:
            block = block[: _BANG_FORWARD_MAX_BLOCK_CHARS - 3].rstrip() + "…"
        return block
    return None


async def _forward_bang_output(message: Message, pane_id: str, raw_text: str) -> None:
    """Tail the pane's transcript for the matching bash entries Claude
    Code wrote, then reply with a 🐚 fenced code block.

    Bails silently if (a) the pane never had a hook fire (no
    transcript mapping), (b) the transcript doesn't show the entry
    within ``_BANG_FORWARD_TIMEOUT_SECONDS``, or (c) the cmd produced
    no useful output.
    """
    cmd = raw_text[1:].lstrip()
    if not cmd:
        return
    transcript_str = state.get_pane_transcript(pane_id)
    if not transcript_str:
        return
    transcript = Path(transcript_str)

    deadline = time.monotonic() + _BANG_FORWARD_TIMEOUT_SECONDS
    block: str | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(_BANG_FORWARD_POLL_INTERVAL)
        block = _read_bang_bash_block(transcript, cmd)
        if block is not None:
            break
    if not block:
        return

    body = f"🐚 <code>{_html.escape(pane_id)}</code>\n<pre>{_html.escape(block)}</pre>"
    try:
        _ = await message.reply_text(
            body,
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
    except Exception:
        logger.exception("Failed to forward bash output for %s", pane_id)


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
                await _spawn_new_pane(message, args, _context)
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

    # `!cmd` only takes Claude Code's `!`-REPL local-command path when
    # typed at an idle prompt. Mid-turn keystrokes get queued/absorbed
    # as plain text in the input buffer — no <bash-input> envelope ever
    # gets written to the transcript, so the forwarder below has nothing
    # to surface and the user sees a silent `→ %N` ack. Refuse + warn
    # so the user knows to wait + resend (issue #9).
    if text.startswith("!") and _pane_is_busy(pane_id):
        _ = await message.reply_text(
            f"⏸ {pane_id} is busy — `!cmd` only works at an idle prompt. "
            "Wait for Claude to finish, then resend.",
            reply_to_message_id=message.message_id,
        )
        return

    logger.info("Sending to tmux pane %s: %s", pane_id, text)
    try:
        _send_to_tmux(pane_id, text)
        _ = await message.reply_text(
            f"→ {pane_id}", reply_to_message_id=message.message_id
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(f"Failed to send to pane {pane_id}: {e}")
        return

    # `!cmd` is Claude Code's bash escape — local execution, no LLM
    # turn, no hooks fire. The hook-driven reply path therefore never
    # surfaces the output to Telegram. Schedule a background poll of
    # the pane's transcript to capture and forward whatever Claude
    # Code wrote (matching <bash-input>/<bash-stdout> entries).
    if text.startswith("!"):
        _ = asyncio.create_task(_forward_bang_output(message, pane_id, text))


# ---------- Entry ----------

_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Any]

# (command_name, description, handler) — single source of truth for both
# telegram.ext handler registration and the Telegram UI command menu.
_COMMANDS: list[tuple[str, str, _Handler]] = [
    ("panes", "List Claude Code panes (tap to activate+subscribe)", cmd_panes),
    ("use", "Set active pane: /use %N", cmd_use),
    ("mode", "Set permission mode for new panes: /mode [name]", cmd_mode),
    ("pinned", "Toggle TodoWrite pinned card: /pinned [on|off]", cmd_pinned),
    ("which", "Show the active pane", cmd_which),
    ("pwd", "Show pane's working directory: /pwd [%N]", cmd_pwd),
    ("new", "Spawn a new Claude pane: /new [dir]", cmd_new),
    ("resume", "Resume a previous Claude session: /resume [dir]", cmd_resume),
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
    # Sweep stale pending-voice entries (and their cached audio) so a
    # crash mid-confirm doesn't leak forever. Cheap directory scan; runs
    # once at startup before polling begins.
    try:
        swept = state.sweep_expired_pending_voice()
        if swept:
            logger.info("Swept %d expired pending-voice entries on startup", swept)
    except Exception:
        logger.exception("Startup sweep of pending-voice failed (non-fatal)")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_publish_menu).build()
    for name, _desc, handler in _COMMANDS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Accept photos + any document (txt, md, pdf, code, logs, zips, …).
    # Explicitly NOT filters.ATTACHMENT because that would also forward
    # videos + audio (other than voice notes), which Claude can't do
    # much with. Voice notes get their own handler (STT → confirm → forward).
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_attachment))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.COMMAND, on_slash_passthrough))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

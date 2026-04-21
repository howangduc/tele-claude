"""Telegram bot that forwards messages to Claude Code tmux panes.

Features
  /panes          — tappable keyboard of Claude Code panes; tap to activate.
  /use %N         — set active pane without the picker.
  /which          — show the active pane.
  /cancel [%N]    — send Ctrl-C to a pane (active if omitted).
  /mute %N        — silence Notification + Stop hooks for that pane.
  /unmute %N      — re-enable hooks.
  /muted          — list muted panes.
  /history %N [n] — capture and send the last N lines of a pane.

Callback handlers (from inline keyboards placed by hooks or by /panes):
  use:%N          — activate a pane.
  ans:%N:1|2|3    — send a digit to a pane (permission prompt answers).
  qr:%N:<text>    — quick-reply text to a pane.
  cancel:%N       — send Ctrl-C to a pane.

Fallback for plain-text messages: resolve pane from a reply-to `%N`,
otherwise the active pane for that chat.
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

_PANE_RE = re.compile(r"(?<!\w)%\d+(?!\w)")

# Built-in bot commands that should NEVER be forwarded to a pane
# (checked by the slash-passthrough handler to avoid double-processing).
_BUILTIN_COMMANDS = {
    "panes",
    "use",
    "which",
    "pwd",
    "new",
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


def _resolve_pane(chat_id: int, message: Message) -> str | None:
    reply_to = message.reply_to_message
    if reply_to and reply_to.text:
        match = _PANE_RE.search(reply_to.text)
        if match:
            return match.group(0)
    return state.get_active_pane(chat_id)


def _pane_arg_or_active(chat_id: int, args: list[str]) -> str | None:
    if args:
        return _normalise_pane(args[0])
    return state.get_active_pane(chat_id)


# ---------- Commands ----------


async def cmd_panes(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    panes = _list_claude_panes()
    alive_ids = {p for p, _, _ in panes}
    # Purge any state pointing at panes that no longer exist so the UI
    # never shows stale %IDs (active/subscribed/muted all get cleaned).
    _ = state.prune_panes(alive_ids)
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
        current = state.get_active_pane(message.chat_id)
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
    current = state.get_active_pane(message.chat_id)
    _ = await message.reply_text(
        f"Active: {current}" if current else "No active pane. Use /panes or /use %N"
    )


async def _spawn_new_pane(message: Message, cwd_arg: str) -> None:
    """Shared spawn logic used by both ``cmd_new`` and the ForceReply path."""
    cwd = os.path.expanduser(cwd_arg) if cwd_arg else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        _ = await message.reply_text(
            f"Directory not found: <code>{_html.escape(cwd)}</code>",
            parse_mode="HTML",
        )
        return

    sessions = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    session_list = [s for s in sessions.stdout.strip().splitlines() if s]
    if not session_list:
        _ = await message.reply_text(
            "No tmux session found. Start tmux on the host first."
        )
        return
    attached = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{?session_attached,#{session_name},}"],
        capture_output=True,
        text=True,
    )
    attached_name = next(
        (line for line in attached.stdout.strip().splitlines() if line), ""
    )
    session = attached_name or session_list[0]

    try:
        created = subprocess.run(
            [
                "tmux",
                "new-window",
                "-t",
                f"{session}:",
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
        _ = await message.reply_text(f"Failed to create pane: {e.stderr or e}")
        return
    new_pane = created.stdout.strip()
    if not new_pane:
        _ = await message.reply_text("tmux didn't return a pane id.")
        return

    time.sleep(0.4)
    _ = subprocess.run(["tmux", "send-keys", "-t", new_pane, "cc", "Enter"], check=True)

    state.subscribe_pane(new_pane)
    state.set_active_pane(message.chat_id, new_pane)

    short_cwd = cwd.replace(os.path.expanduser("~"), "~")
    _ = await message.reply_text(
        f"✅ Spawned <code>{_html.escape(new_pane)}</code> in "
        f"<code>{_html.escape(short_cwd)}</code> · session "
        f"<code>{_html.escape(session)}</code>\n"
        f"Launched <code>cc</code> · active + subscribed 🔔",
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
    if args:
        await _spawn_new_pane(message, args[0])
        return
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
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
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
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
    if not pane_id:
        _ = await message.reply_text("Usage: /mute %N")
        return
    state.mute_pane(pane_id)
    _ = await message.reply_text(f"🔕 Muted {pane_id}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not _authorised(message.chat_id):
        return
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
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
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
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
    pane_id = _pane_arg_or_active(message.chat_id, list(context.args or []))
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
        pane_id = state.get_active_pane(message.chat_id)
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


async def on_photo(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an incoming image and hand its absolute path to the active pane.

    Accepts both PHOTO (compressed) and Document.IMAGE (original quality).
    Caption, if any, is forwarded before the path so Claude has context.
    """
    message = update.message
    if not message or not _authorised(message.chat_id):
        return

    tg_file = None
    suffix = "jpg"
    if message.photo:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        unique = photo.file_unique_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        tg_file = await message.document.get_file()
        unique = message.document.file_unique_id
        original = message.document.file_name or ""
        if "." in original:
            suffix = original.rsplit(".", 1)[-1].lower()
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

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = IMAGE_DIR / f"tg_{message.message_id}_{unique}.{suffix}"
    _ = await tg_file.download_to_drive(str(out_path))

    caption = (message.caption or "").strip()
    text = f"{caption}\n{out_path}" if caption else str(out_path)

    logger.info("Image → tmux pane %s: %s (caption=%r)", pane_id, out_path, caption)
    try:
        _send_to_tmux(pane_id, text)
        _ = await message.reply_text(
            f"🖼 → {pane_id}", reply_to_message_id=message.message_id
        )
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(
            f"Failed to send to pane {pane_id}: {e}",
            reply_to_message_id=message.message_id,
        )


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


_register_menu = _publish_menu  # back-compat alias kept for existing call sites


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_publish_menu).build()
    for name, _desc, handler in _COMMANDS:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.COMMAND, on_slash_passthrough))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

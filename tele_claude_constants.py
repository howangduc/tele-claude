"""Centralized configuration constants for tele-claude.

Every hardcoded value that drives behavior lives here so it can be
tuned in one place. Env-var-overridable values are evaluated at
import time — restart the bot / hooks after changing them.

Skipped on purpose (kept inline at their sole call-site):
  * compiled regexes (``_FILENAME_SAFE_RE``, ``_PANE_RE``, ``_ROW_RE``,
    ``_SEP_RE``) — single-use, no benefit to centralizing.
  * format.py placeholder sentinels — internal stashing markers.
  * the literal ``"tmux"`` binary name in subprocess calls.
  * domain-data sets (``_BUILTIN_COMMANDS``, ``_SKIP_ARGS_TOKENS``).
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------- Env helpers ----------


def env_truthy(name: str) -> bool:
    """Read ``name`` from env and return True if it looks affirmative.

    Recognises ``1`` / ``true`` / ``yes`` / ``on`` (case- and
    whitespace-insensitive). Anything else (including unset) reads as
    False. Used by feature gates like ``TELE_CLAUDE_AUTO_TRUST`` and
    ``TELE_CLAUDE_STT_TAG_EVENTS`` so they share one set of accepted
    values — drift between gates was a real review finding (PR #34).
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ---------- Launch command ----------

# Shell command typed into a freshly-spawned pane by ``/new``. The
# ``TELE_CLAUDE=1`` prefix is the opt-in gate every hook wrapper checks
# (see ``~/.claude/hooks/telegram-*.sh``); without it the new pane stays
# silent. Default uses the explicit ``claude --dangerously-skip-permissions``
# form so it works without depending on the user's bashrc aliases.
# Override per-machine via ``TELE_CLAUDE_NEW_LAUNCH_CMD`` (e.g. set it
# to ``TELE_CLAUDE=1 ccm`` if you have a Claude Code wrapper alias).
# Common prefix shared by the default LAUNCH_CMD and _resolve_launch_cmd's
# generated commands — keeps the env-var name + bin name in one place so
# they don't drift (e.g. if we rename TELE_CLAUDE to something else).
_BASE_LAUNCH_CMD = "TELE_CLAUDE=1 claude"

LAUNCH_CMD = os.environ.get(
    "TELE_CLAUDE_NEW_LAUNCH_CMD",
    f"{_BASE_LAUNCH_CMD} --dangerously-skip-permissions",
)

# Permission modes selectable from the bot's /mode command (issue #27).
# Maps the user-facing mode name to Claude Code's --permission-mode flag
# value. ``None`` means "no flag at all" — bypass uses the legacy
# --dangerously-skip-permissions instead, preserving the 0.1.x default
# without forcing a breaking behaviour change. Power users who set
# TELE_CLAUDE_NEW_LAUNCH_CMD bypass this whole machinery (their
# override wins; we never inject --permission-mode into a custom cmd).
PERMISSION_MODES: dict[str, str | None] = {
    "default": "default",        # normal permission prompts (1/2/3 keyboard via ans:)
    "acceptEdits": "acceptEdits",  # auto-accept file edits, prompt for other tools
    "plan": "plan",              # planning-only — no Edit/Write/Bash without approval
    "bypass": None,              # --dangerously-skip-permissions (current default)
}

# ---------- tmux ----------

# Paste-buffer name used when sending multi-line text to a pane
# (load-buffer + paste-buffer instead of literal send-keys, so the
# REPL treats the input as a paste rather than rapid keystrokes).
TMUX_PASTE_BUFFER = "tele-claude-tmp"

# Prefix for tmux sessions created by ``/new``. The pane's basename
# is appended (sanitized), with ``-2`` / ``-3`` / ... collision suffixes.
TMUX_SESSION_PREFIX = "claude-"

# Settling delays (seconds) after destructive tmux ops — the REPL
# needs a beat to register the input event before the next keystroke.
PASTE_SETTLE_DELAY = 0.3
SPAWN_SETTLE_DELAY = 0.4

# ---------- Telegram API ----------

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram's hard message-body limit is 4096 chars; this leaves room
# for header text and HTML tag inflation.
MAX_MESSAGE_LEN = 4000

# Markdown → HTML can inflate text by 20-50% (adding <b>, <code>,
# <pre> tags). Start the split at this raw budget and shrink
# iteratively if conversion overshoots; never go below the floor.
RAW_SPLIT_BUDGET = 2500
MIN_RAW_SPLIT = 800

# Telegram caps forum-topic names at 128 chars; we stay well under.
TOPIC_NAME_MAX = 120

# CWD segment width budget inside a topic name (rest goes to pane id
# + title + separators).
TOPIC_CWD_MAX = 60

# Prefix on "waiting for args" prompt messages — used both to emit
# the prompt and to detect replies to it.
ARGS_PROMPT_PREFIX = "Args for /"

# HTTP request timeout (seconds) for direct Telegram API calls from
# the hooks module (the bot uses python-telegram-bot's own timeouts).
HTTP_TIMEOUT = 10

# ---------- Markdown formatting ----------

# Max total monospace width that fits without wrapping on a typical
# mobile Telegram client at default font size. Above this we switch
# from aligned <pre> tables to vertical bullet blocks.
PRE_MAX_WIDTH = 34

# ---------- Hook timing & throttles ----------

# Idle notifications get suppressed unless this many seconds have
# passed since the session's last real activity (UserPromptSubmit
# or Stop). Claude Code's own idle_prompt fires at 60s (hardcoded
# upstream — see anthropics/claude-code#13922).
IDLE_SUPPRESS_SECONDS = float(os.environ.get("TELE_CLAUDE_IDLE_MIN_SECONDS", "900"))

# Forum-mode topic rename throttle: fire ``editForumTopic`` once
# every Nth Stop hook (per-pane counter in state). 1 = every turn.
TOPIC_RENAME_EVERY_N = max(
    1, int(os.environ.get("TELE_CLAUDE_TOPIC_RENAME_EVERY", "15"))
)

# Typing-indicator pumper: ``sendChatAction`` lasts 5s per call, so
# the pumper re-sends every TYPING_PUMP_INTERVAL seconds while a
# turn is active. TYPING_PUMP_MAX_SECONDS is the wall-clock ceiling
# (protects against truly-orphaned pumpers).
TYPING_PUMP_INTERVAL = 4.0
TYPING_PUMP_MAX_SECONDS = 2700.0  # 45 min

# Default poll interval (seconds) when the Stop hook is waiting for
# Claude Code's buffered JSONL transcript writer to flush.
TRANSCRIPT_POLL_INTERVAL = 0.3

# ---------- /history command ----------

HISTORY_DEFAULT_LINES = 20
HISTORY_MAX_LINES = 500
HISTORY_BODY_TRIM = 3500  # max chars before head-truncation

# ---------- Multi-question AskUserQuestion ----------

# How long pending-question state for a pane is considered fresh. If
# the user dismisses a multi-question dialog inside tmux (Esc) or
# answers it directly without using Telegram, we don't want the stale
# cache to corrupt the next AskUserQuestion fired by the same pane.
# 15 min is plenty for any normal answer flow.
PENDING_QUESTIONS_TTL_SECONDS = 900

# ---------- Speech-to-text ----------

# Provider name resolved from env (``elevenlabs`` or ``selfhost``).
# The factory in ``tele_claude_speech.py`` reads this with the same
# default; this constant lets unrelated code (e.g. logs, status
# command) see the active provider without re-reading the env.
STT_PROVIDER_DEFAULT = "elevenlabs"

# How long an unconfirmed transcript stays in the pending cache before
# the next interaction sees it as expired (and edits the card to
# ``⌛ expired`` with no buttons). Mirrors the pending-questions TTL.
PENDING_VOICE_TTL_SECONDS = int(
    os.environ.get("TELE_CLAUDE_STT_PENDING_TTL_SECONDS", "900")
)

# ---------- Filenames ----------

# Hard cap on the disk-side filename component (most filesystems
# allow 255 bytes; we stay well under to leave room for the
# ``tg_<msg>_`` prefix).
FILENAME_MAX_LEN = 120


# ---------- Filesystem paths (env-overridable) ----------


def _default_state_dir() -> Path:
    return Path.home() / ".cache" / "tele-claude"


STATE_DIR: Path = Path(
    os.environ.get("TELE_CLAUDE_STATE_DIR") or str(_default_state_dir())
)
IMAGE_DIR: Path = Path(
    os.environ.get("TELE_CLAUDE_IMAGE_DIR") or str(_default_state_dir() / "images")
)
FILE_DIR: Path = Path(
    os.environ.get("TELE_CLAUDE_FILE_DIR") or str(_default_state_dir() / "files")
)
# Voice-note audio files (.ogg) cached on download so the "Retry" /
# "Try other provider" buttons can re-run STT against the same audio
# without asking Telegram for the file again. Override via
# TELE_CLAUDE_VOICE_DIR (rare — kept for symmetry with IMAGE_DIR).
VOICE_DIR: Path = Path(
    os.environ.get("TELE_CLAUDE_VOICE_DIR") or str(_default_state_dir() / "voice")
)


# ---------- Forum mode (supergroup with Topics) ----------


def _read_forum_chat_id() -> int | None:
    raw = os.environ.get("TELE_CLAUDE_SUPERGROUP_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


FORUM_CHAT_ID: int | None = _read_forum_chat_id()

# When forum mode is on, only forward hook output to the supergroup
# (not to other authorised chats). Set =0 in env to re-enable fan-out.
FORUM_EXCLUSIVE: bool = (
    os.environ.get("TELE_CLAUDE_FORUM_EXCLUSIVE", "1").strip() != "0"
)

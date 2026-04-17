"""Persistent runtime state for tele-claude.

A small JSON file at ~/.cache/tele-claude/state.json plus a couple of
sibling subdirectories for ephemeral per-session data. Writes are
atomic (temp + rename) so concurrent readers never see a partial file.

Shared between the bot (reads + writes) and the Claude Code hooks
(mostly reads, plus per-session progress/fingerprint files owned by
the hooks themselves). No locking is needed because the bot owns
state.json and hooks only touch their own per-session files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


def _cache_root() -> Path:
    root = Path(
        os.environ.get("TELE_CLAUDE_STATE_DIR", Path.home() / ".cache" / "tele-claude")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path() -> Path:
    return _cache_root() / "state.json"


def _load() -> dict[str, object]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(state: dict[str, object]) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_str_list(state: dict[str, object], key: str) -> list[str]:
    """Narrow a state value to ``list[str]``.

    ``_load()`` returns ``dict[str, object]`` for type safety, which means
    every accessor has to narrow the ``object`` back down before using it.
    This helper centralises that pattern so the rest of the file doesn't
    repeat ``isinstance(x, list)`` checks inline (and keeps basedpyright
    happy — ``set(state.get(...) or [])`` trips on the ``object`` type).
    Non-str entries in the list are dropped defensively.
    """
    value = state.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


# ---------- Active pane (per Telegram chat) ----------


def get_active_pane(chat_id: int) -> str | None:
    panes = _load().get("active_pane", {})
    return panes.get(str(chat_id)) if isinstance(panes, dict) else None


def set_active_pane(chat_id: int, pane_id: str) -> None:
    state = _load()
    panes = state.get("active_pane")
    if not isinstance(panes, dict):
        panes = {}
    panes[str(chat_id)] = pane_id
    state["active_pane"] = panes
    _save(state)


# ---------- Subscribed panes (hooks only fire for these) ----------


def get_subscribed_panes() -> set[str]:
    return set(_load_str_list(_load(), "subscribed_panes"))


def subscribe_pane(pane_id: str) -> None:
    """Add a pane to the subscription set so hooks forward from it."""
    state = _load()
    subs = set(_load_str_list(state, "subscribed_panes"))
    if pane_id in subs:
        return
    subs.add(pane_id)
    state["subscribed_panes"] = sorted(subs)
    _save(state)


def unsubscribe_pane(pane_id: str) -> None:
    state = _load()
    subs = set(_load_str_list(state, "subscribed_panes"))
    if pane_id not in subs:
        return
    subs.discard(pane_id)
    state["subscribed_panes"] = sorted(subs)
    _save(state)


def is_subscribed(pane_id: str) -> bool:
    return pane_id in get_subscribed_panes()


def prune_panes(alive_pane_ids: set[str]) -> tuple[set[str], set[str]]:
    """Remove dead panes from every pane-keyed state section.

    Returns (removed_subscribed, removed_muted) for user-facing reporting.
    `active_pane` entries pointing at dead panes are also cleared.
    """
    state = _load()
    changed = False

    subs = set(_load_str_list(state, "subscribed_panes"))
    dead_subs = subs - alive_pane_ids
    if dead_subs:
        state["subscribed_panes"] = sorted(subs - dead_subs)
        changed = True

    muted = set(_load_str_list(state, "muted_panes"))
    dead_muted = muted - alive_pane_ids
    if dead_muted:
        state["muted_panes"] = sorted(muted - dead_muted)
        changed = True

    active = state.get("active_pane")
    if isinstance(active, dict):
        stale_chats = [c for c, p in active.items() if p not in alive_pane_ids]
        for chat in stale_chats:
            del active[chat]
        if stale_chats:
            state["active_pane"] = active
            changed = True

    if changed:
        _save(state)
    return dead_subs, dead_muted


# ---------- Muted panes (global across chats) ----------


def get_muted_panes() -> set[str]:
    return set(_load_str_list(_load(), "muted_panes"))


def mute_pane(pane_id: str) -> None:
    state = _load()
    muted = set(_load_str_list(state, "muted_panes"))
    muted.add(pane_id)
    state["muted_panes"] = sorted(muted)
    _save(state)


def unmute_pane(pane_id: str) -> None:
    state = _load()
    muted = set(_load_str_list(state, "muted_panes"))
    muted.discard(pane_id)
    state["muted_panes"] = sorted(muted)
    _save(state)


def is_muted(pane_id: str) -> bool:
    return pane_id in get_muted_panes()


# ---------- Progress message tracking (per session × chat) ----------


def _progress_dir() -> Path:
    path = _cache_root() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _progress_file(key: str) -> Path:
    safe = key.replace("/", "_").replace(":", "_")
    return _progress_dir() / f"{safe}.json"


def set_progress_msg_id(key: str, message_id: int) -> None:
    _progress_file(key).write_text(json.dumps({"message_id": message_id}))


def get_progress_msg_id(key: str) -> int | None:
    path = _progress_file(key)
    if not path.exists():
        return None
    try:
        return int(json.loads(path.read_text()).get("message_id"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def clear_progress(key: str) -> None:
    try:
        _progress_file(key).unlink()
    except FileNotFoundError:
        pass


# ---------- Claude command shortcuts ----------


def get_claude_shortcuts() -> dict[str, str]:
    """Return {name: description} of user-saved Claude slash-command shortcuts."""
    data = _load().get("claude_shortcuts", {})
    return data if isinstance(data, dict) else {}


def add_claude_shortcut(name: str, description: str) -> None:
    """Register a Claude slash command so it shows up in Telegram autocomplete."""
    state = _load()
    shortcuts = state.get("claude_shortcuts")
    if not isinstance(shortcuts, dict):
        shortcuts = {}
    shortcuts[name] = description or "Forward to the active Claude pane"
    state["claude_shortcuts"] = shortcuts
    _save(state)


def remove_claude_shortcut(name: str) -> None:
    """Drop a shortcut. Silently no-ops if absent."""
    state = _load()
    shortcuts = state.get("claude_shortcuts")
    if not isinstance(shortcuts, dict) or name not in shortcuts:
        return
    del shortcuts[name]
    state["claude_shortcuts"] = shortcuts
    _save(state)


# ---------- Progress heartbeat throttle (PostToolUse hook) ----------


def _heartbeat_file(session_id: str) -> Path:
    path = _cache_root() / "heartbeat"
    path.mkdir(parents=True, exist_ok=True)
    safe = session_id.replace("/", "_")
    return path / f"{safe}.ts"


def should_heartbeat(session_id: str, min_interval_seconds: float = 5.0) -> bool:
    """Return True if enough time has passed since the last heartbeat.

    Used by PostToolUse to throttle ⏳ updates — Telegram limits edits
    to ~30/min/chat, and updating after every single tool call would
    spam both the wire and the user's notification tray.
    """
    path = _heartbeat_file(session_id)
    now = time.time()
    if path.exists():
        try:
            last = float(path.read_text().strip())
            if now - last < min_interval_seconds:
                return False
        except (OSError, ValueError):
            pass
    try:
        path.write_text(f"{now:.3f}")
    except OSError:
        pass
    return True


def clear_heartbeat(session_id: str) -> None:
    try:
        _heartbeat_file(session_id).unlink()
    except FileNotFoundError:
        pass


# ---------- Activity tracking (for idle-notification suppression) ----------


def _activity_file(session_id: str) -> Path:
    path = _cache_root() / "activity"
    path.mkdir(parents=True, exist_ok=True)
    safe = session_id.replace("/", "_")
    return path / f"{safe}.ts"


def touch_activity(session_id: str) -> None:
    """Record the current time as the last activity for this session."""
    try:
        _activity_file(session_id).write_text(f"{time.time():.3f}")
    except OSError:
        pass


def seconds_since_activity(session_id: str) -> float | None:
    """Seconds since the last activity, or None if never recorded."""
    path = _activity_file(session_id)
    if not path.exists():
        return None
    try:
        return max(0.0, time.time() - float(path.read_text().strip()))
    except (OSError, ValueError):
        return None


# ---------- Dedup fingerprints (per session) ----------


def _fingerprint_file(session_id: str) -> Path:
    path = _cache_root() / "fingerprints"
    path.mkdir(parents=True, exist_ok=True)
    safe = session_id.replace("/", "_")
    return path / f"{safe}.json"


def check_and_set_fingerprint(
    session_id: str, content: str, ttl_seconds: float = 5.0
) -> bool:
    """Return True if this content was seen within TTL (skip sending).

    Otherwise record the new fingerprint and return False.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    now = time.time()
    path = _fingerprint_file(session_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if (
                data.get("hash") == digest
                and now - float(data.get("ts", 0)) < ttl_seconds
            ):
                return True
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    try:
        path.write_text(json.dumps({"hash": digest, "ts": now}))
    except OSError:
        pass
    return False

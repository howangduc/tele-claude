# Boot-Prune Stale Pane References — Design

**Issue:** [#54](https://github.com/SCP120/tele-claude/issues/54) — `[bug] panes persist across machine restart — should reset on boot`

**Date:** 2026-05-08

**Author:** Claude Opus 4.7 (1M context) + reviewer SCP120

---

## Goal

After a machine reboot, sleep+wake, or `tmux kill-server`, the bot's persistent state file (`~/.cache/tele-claude/state.json`) holds pane references (subscribed_panes, muted_panes, active_pane, pane_topics, pane_topic_names) that point at tmux pane IDs which no longer exist. `/panes` shows ghost entries; hook callbacks from new panes that happen to reuse an old `%N` get wrong subscription state.

**Fix:** at bot startup, query the current tmux server for the live pane-id set, and call the existing `tele_claude_state.prune_panes(alive_pane_ids)` to wipe every reference to a pane that's no longer alive.

---

## Background — what already exists

- `tele_claude_state.prune_panes(alive: set[str]) -> tuple[set[str], set[str]]` is already a working API. It removes dead pane IDs from `subscribed_panes`, `muted_panes`, `active_pane`, `pane_topics`, `pane_topic_names`, and per-pane files in `pending_questions/`. Returns `(removed_subscribed, removed_muted)` for reporting.
- It's already called from `cmd_panes` (the `/panes` command), so the user can manually trigger a prune by running `/panes`. But that's reactive — the user has to know they need it.
- The bot's `main()` at `tele_claude.py:3109` builds the `Application`, registers handlers, and calls `application.run_polling()`. There's currently no startup hook that touches state.

The fix is one new call-site at bot startup.

---

## Decision

**Add a startup-time prune call in `main()`** before `application.run_polling()`. Sources the live pane set from a new helper `_alive_tmux_panes()` which wraps `tmux list-panes -a -F '#{pane_id}'`. When tmux isn't running, the helper returns the empty set, the prune wipes every pane reference, and the user starts fresh — exactly what we want after a reboot before the tmux server is back up.

Log the removal counts so the user can see what was cleaned.

---

## Scope

Touches **two files**:

### `tele_claude.py`

**1. New helper** near the existing `_pane_exists(pane_id)` (around line 219):

```python
def _alive_tmux_panes() -> set[str]:
    """Return the set of pane IDs the tmux server currently knows about.

    Empty set when no tmux server is running — which is the right answer
    for the boot-prune path: no server means every previously-subscribed
    pane is dead. (#54)
    """
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())
```

**2. Boot-prune call** in `main()` immediately before `application.run_polling()`:

```python
    # Boot-prune (#54): validate every pane reference in state against
    # the current tmux server. After reboot, sleep+wake, or
    # `tmux kill-server`, old pane IDs are dead. The state file
    # survives in ~/.cache/tele-claude/, so we'd otherwise show ghost
    # subscriptions in /panes and route hook output to dead panes.
    alive = _alive_tmux_panes()
    removed_subs, removed_muted = state.prune_panes(alive)
    if removed_subs or removed_muted:
        logger.info(
            "boot-prune: dropped %d subscribed and %d muted dead pane(s) "
            "(alive=%d)",
            len(removed_subs),
            len(removed_muted),
            len(alive),
        )
```

### `tests/test_state_prune.py` (new file)

Pure unit test on `tele_claude_state.prune_panes` since it's a pure function over a JSON file. Use the existing `STATE_DIR` env-var override to point state.json at a temp directory.

```python
"""Tests for tele_claude_state.prune_panes (#54)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tele_claude_state at a fresh temp dir for each test.

    constants.STATE_DIR is read at import time, so we have to monkeypatch
    the resolved Path object on the constants module — not just the env.
    """
    monkeypatch.setenv("TELE_CLAUDE_STATE_DIR", str(tmp_path))
    import importlib

    import tele_claude_constants as constants
    import tele_claude_state as state

    importlib.reload(constants)
    importlib.reload(state)
    return tmp_path


def _write_state(state_dir: Path, payload: dict[str, object]) -> None:
    (state_dir / "state.json").write_text(json.dumps(payload))


def test_prune_panes_drops_dead_subscribed(state_dir: Path) -> None:
    import tele_claude_state as state

    _write_state(
        state_dir, {"subscribed_panes": ["%1", "%2", "%3"], "muted_panes": []}
    )
    removed_subs, removed_muted = state.prune_panes({"%1", "%3"})
    assert removed_subs == {"%2"}
    assert removed_muted == set()
    after = state.get_subscribed_panes()
    assert after == {"%1", "%3"}


def test_prune_panes_clears_active_pane_for_dead(state_dir: Path) -> None:
    import tele_claude_state as state

    _write_state(
        state_dir,
        {"active_pane": {"123": "%5", "456": "%2"}, "subscribed_panes": []},
    )
    state.prune_panes({"%2"})
    assert state.get_active_pane(123) is None
    assert state.get_active_pane(456) == "%2"


def test_prune_panes_empty_alive_wipes_all(state_dir: Path) -> None:
    """Common boot-prune case: tmux server gone, no panes alive."""
    import tele_claude_state as state

    _write_state(
        state_dir,
        {
            "subscribed_panes": ["%1", "%2"],
            "muted_panes": ["%3"],
            "active_pane": {"7": "%1"},
            "pane_topics": {"%1": 100, "%2": 200},
            "pane_topic_names": {"%1": "x", "%2": "y"},
        },
    )
    removed_subs, removed_muted = state.prune_panes(set())
    assert removed_subs == {"%1", "%2"}
    assert removed_muted == {"%3"}
    assert state.get_subscribed_panes() == set()
    assert state.get_active_pane(7) is None
```

---

## Non-goals (deliberately out of scope)

- **Boot-id fingerprint** (compare `/proc/sys/kernel/random/boot_id` against stored one). Doesn't help when tmux is killed without a reboot. Option A's "ask tmux directly" approach handles both. YAGNI.
- **Move state under `/run/user/$UID`** so it's volatile. Wipes non-pane state (permission_mode, pinned_todos, claude_shortcuts) too — all of which the user wants to survive reboot.
- **Periodic prune** (cron-style every N minutes). Reactive `/panes` already exists for that need. The startup prune is the only place the user can't manually trigger.
- **Persistence opt-in** (only prune subscribed panes; keep muted as a permanent ignore-list). The user's expected mental model is "panes are tmux-session-scoped" — both subscribed AND muted should reset.
- **Notify the user via Telegram** when prune fires. Log line is enough; the user can see it via `journalctl --user -u tele-claude.service`. Sending a message would spam the supergroup on every restart.

---

## Risk assessment

**Low risk.**

- **`prune_panes` is already battle-tested** via `cmd_panes` invocations.
- **The boot-prune call is purely additive**; no existing call-site changes.
- **`_alive_tmux_panes()` cannot raise** under normal conditions — `subprocess.run` with `capture_output=True` swallows the non-zero exit + missing-server messages, leaving stdout empty. Empty stdout → empty set → wipe everything. That's the correct behaviour after a fresh boot.
- **Edge case: pane ID reuse.** After tmux restart, `%N` numbering restarts. If a *new* `%5` is created BEFORE the bot prunes, the new pane inherits the old subscription. Mitigation: the bot's startup runs immediately, so the window is sub-second. If the user spawns Claude panes via `/new` BEFORE the bot is up, they wouldn't have subscriptions in state yet (the bot writes them). So this race is theoretical.
- **Concurrency:** the bot is the only writer of `state.json`. Hook scripts read but don't write `subscribed_panes`. The boot-prune fires before `run_polling()`, so no other code path can race it.

---

## Verification

**Unit tests** (3 new tests in `tests/test_state_prune.py`) pin the prune behaviour against a temp state file:
- Drops dead from `subscribed_panes`.
- Clears stale `active_pane` chat→pane entries.
- Empty alive set wipes everything (the common boot case).

**Live test** (post-deploy):
1. Subscribe a pane via `/new`. Verify in `/panes` it appears.
2. Kill the tmux server: `tmux kill-server`.
3. Restart the bot: `systemctl --user restart tele-claude.service`.
4. Tail logs: `journalctl --user -u tele-claude.service -f`.
   - **Expected:** log line `boot-prune: dropped N subscribed and M muted dead pane(s) (alive=0)`.
5. Run `/panes` — empty list.
6. Spawn fresh tmux + new Claude pane via `/new` — verify it appears in `/panes` and gets a fresh topic in forum mode.

PASS = log line present + ghost panes gone from `/panes`.

---

## Implementation outline

- One commit on `fix/boot-prune-panes` branch.
- Two files: `tele_claude.py` (helper + main() call) + `tests/test_state_prune.py` (new).
- ~25 LOC source + ~70 LOC tests.
- Single PR. Squash-merge → tag `v0.3.6` → restart bot → live verify per the recipe.
- CHANGELOG `### Fixed` entry referencing #54.
- Close #54 via `Closes #54` in the PR body.

---

## Self-review notes

- **Placeholder scan:** none. Specific function signatures + exact log line text + exact test scaffolding.
- **Internal consistency:** the boot-prune call uses the existing `state.prune_panes` API exactly as `cmd_panes` does. No divergent path.
- **Scope check:** single subsystem (state module + bot startup). Single PR.
- **Ambiguity:** `_alive_tmux_panes()` deliberately returns the empty set on missing tmux server (vs. raising). The docstring calls this out so a future reader doesn't "fix" it by adding a raise.

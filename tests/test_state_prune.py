"""Tests for tele_claude_state.prune_panes (#54)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tele_claude_state at a fresh temp dir for each test.

    constants.STATE_DIR is read at import time, so we have to reload
    both modules after monkeypatching the env var so the new path
    takes effect.
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

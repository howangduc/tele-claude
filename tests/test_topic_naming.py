"""Unit tests for the topic-name composer.

The bot and the hook subprocess each keep their own copy of
``_compose_topic_name`` (see CLAUDE.md note about the hooks module
running as a separate process). Both copies must agree, so these
tests pin the contract by exercising the hooks copy — it's the only
one safely importable without spinning up the Telegram client.
"""

from __future__ import annotations

import tele_claude_constants as constants
from tele_claude_hooks import _compose_topic_name


def test_basename_only_for_typical_cwd() -> None:
    assert _compose_topic_name("%9", "💤 idle", "/home/you/tele-claude") == "tele-claude"


def test_strips_trailing_slash() -> None:
    assert _compose_topic_name("%9", "⏳ working", "/home/you/tele-claude/") == "tele-claude"


def test_pane_title_is_ignored() -> None:
    # The whole point of the fix: live activity icons no longer pollute the topic name.
    a = _compose_topic_name("%9", "⏳ 7t · Bash +2a", "/srv/foo")
    b = _compose_topic_name("%9", "🤖 Found 3 issues — shipping fix", "/srv/foo")
    assert a == b == "foo"


def test_empty_cwd_falls_back_to_pane_id() -> None:
    assert _compose_topic_name("%9", "any title", "") == "%9"


def test_root_cwd_falls_back_to_pane_id() -> None:
    assert _compose_topic_name("%9", "any title", "/") == "%9"


def test_excessively_long_basename_is_truncated() -> None:
    long_basename = "x" * (constants.TOPIC_NAME_MAX + 50)
    cwd = f"/home/{long_basename}"
    out = _compose_topic_name("%9", "", cwd)
    assert len(out) == constants.TOPIC_NAME_MAX
    assert out.endswith("…")

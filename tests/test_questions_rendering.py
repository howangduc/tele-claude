"""Smoke tests for tele_claude_questions pure functions."""

from __future__ import annotations

import tele_claude_questions as q


def test_render_simple_question() -> None:
    body = q.render_question_html(
        {"question": "Pick one", "options": [{"label": "A"}, {"label": "B"}]},
        idx=0,
        total=1,
    )
    assert "Pick one" in body
    assert "1. A" in body
    assert "2. B" in body


def test_question_keyboard_rows_single_select() -> None:
    rows = q.question_keyboard_rows(
        "%5", {"options": [{"label": "Yes"}, {"label": "No"}]}
    )
    assert len(rows) == 2
    assert rows[0][0]["callback_data"] == "ans:%5:1"
    assert rows[1][0]["callback_data"] == "ans:%5:2"


def test_question_keyboard_rows_multi_select() -> None:
    rows = q.question_keyboard_rows(
        "%5", {"multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]}
    )
    # 2 toggle rows + 1 submit row
    assert len(rows) == 3
    assert rows[0][0]["callback_data"] == "mtg:%5:1:0"
    assert rows[2][0]["callback_data"] == "msub:%5"


def test_ans_callback_data_shape() -> None:
    """Document the wire shape ``ans:`` callbacks must satisfy.

    The ``on_callback`` handler in ``tele_claude.py`` length-guards
    ``data.split(":", 2)`` to exactly 3 parts. This test asserts the
    keyboard builder produces compliant data so the guard passes.
    """
    rows = q.question_keyboard_rows("%5", {"options": [{"label": "A"}]})
    cb = rows[0][0]["callback_data"]
    parts = cb.split(":", 2)
    assert len(parts) == 3
    assert parts == ["ans", "%5", "1"]


def test_keyboard_rows_empty_options_returns_empty() -> None:
    """Empty options[] must return [] so hook code can detect and
    avoid falling through to Allow/Always/Deny. (#42)
    """
    assert q.question_keyboard_rows("%5", {"options": []}) == []
    assert q.question_keyboard_rows("%5", {}) == []
    assert q.question_keyboard_rows("%5", {"options": "not a list"}) == []


def test_max_options_cap_for_multi_select() -> None:
    """``question_keyboard_rows`` must never return more than
    _MAX_OPTIONS toggle rows even if more options are declared. (#44)
    """
    many_options = [{"label": f"Opt{i}"} for i in range(20)]
    rows = q.question_keyboard_rows(
        "%5", {"multiSelect": True, "options": many_options}
    )
    # _MAX_OPTIONS toggle rows + 1 submit row
    assert len(rows) == q._MAX_OPTIONS + 1


def test_multi_select_renders_default_warning() -> None:
    """Multi-select bodies carry a one-line warning that the
    Telegram mask does not mirror TUI pre-checked defaults. (#45)
    """
    body = q.render_question_html(
        {"multiSelect": True, "question": "Pick", "options": [{"label": "A"}]},
        idx=0,
        total=1,
    )
    assert "verify selection in pane" in body


def test_single_select_no_default_warning() -> None:
    body = q.render_question_html(
        {"question": "Pick", "options": [{"label": "A"}]},
        idx=0,
        total=1,
    )
    assert "verify selection in pane" not in body


def test_is_free_text_option_label_match() -> None:
    """Free-text label heuristic is case-insensitive and trimmed. (#46)"""
    assert q.is_free_text_option({"label": "Type something"}) is True
    assert q.is_free_text_option({"label": "  type SOMETHING  "}) is True
    assert q.is_free_text_option({"label": "Other"}) is True
    assert q.is_free_text_option("custom") is True
    assert q.is_free_text_option({"label": "Yes"}) is False
    assert q.is_free_text_option({}) is False
    assert q.is_free_text_option(None) is False


def test_keyboard_rows_marks_free_text_with_pencil() -> None:
    """Free-text options render with ✏️ marker on initial paint so
    user sees they can't toggle this slot before tapping. (#46 UX)
    """
    rows = q.question_keyboard_rows(
        "%5",
        {
            "multiSelect": True,
            "options": [
                {"label": "Yes"},
                {"label": "No"},
                {"label": "Type something"},
            ],
        },
    )
    # 3 toggle rows + 1 submit row
    assert len(rows) == 4
    assert rows[0][0]["text"].startswith("☐")  # Yes
    assert rows[1][0]["text"].startswith("☐")  # No
    assert rows[2][0]["text"].startswith("✏️")  # Type something

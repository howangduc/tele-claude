# Drop Success-Path Toasts on Callback Handlers — Design

**Issue:** [#53](https://github.com/SCP120/tele-claude/issues/53) — `[fix] drop blocking "Sent N → pane" alert after permission button tap`

**Date:** 2026-05-08

**Author:** Claude Opus 4.7 (1M context) + reviewer SCP120

---

## Goal

After tapping any success-path inline-keyboard button in tele-claude (permission Allow / AUQ answer / multi-select toggle / submit / cancel / quick-reply / switch-pane), the user should see **no Telegram modal alert and no transient toast**. The user-visible feedback that the tap registered is the inline-keyboard disappearing (already implemented via `edit_message_reply_markup(reply_markup=None)` on every success path).

Error / refusal paths keep their alert-style modals because they need explicit acknowledgement.

---

## Background — why the alert exists today

Telegram Bot API requires every callback query to be acknowledged via `answerCallbackQuery` within 30s, otherwise the inline-spinner on the tapped button stays spinning indefinitely. The bot's current code calls `await query.answer(text, show_alert=True)` on every success-tap — which produces a full-screen modal on mobile that the user must dismiss with **OK** before continuing. The text inside the modal is purely informational ("✅ Sent 1 → %5") and adds no decision input — the action has already happened.

User reported this as friction on mobile (#53). The information is redundant with two other signals already in place:
1. The button row disappears (`edit_message_reply_markup(reply_markup=None)`).
2. The bot's downstream acknowledgement (e.g. Claude TUI processing the keystroke, or a follow-up message in the topic) lands within ≤2s.

So the modal is pure noise on the success path.

---

## Decision

**Drop the toast entirely on success paths.** Replace `await query.answer(text, show_alert=True)` and `await query.answer(text)` with the bare `await query.answer()` (no arguments). The Telegram API still gets its required acknowledgement — the user just sees nothing happen client-side except the buttons going away.

**Keep the alert-style modal on error and refusal paths** because those scenarios genuinely need user acknowledgement (the user has to know the action did NOT happen as expected).

---

## Scope — exact call-sites in `tele_claude.py`

### Drop toast (change to `query.answer()`)

| Path | Approx. line | Current text | Why drop |
|---|---|---|---|
| `use:` (switch active pane) | ~1819 | `f"Active: {pane_id}"` | Edit-in-place message body already updates with the new active pane. |
| `ans:` (single-select / permission) | ~1885 | `f"✅ Sent {answer} → {pane_id}"`, `show_alert=True` | The keystroke is already on its way to tmux; the message buttons are dropped right after. |
| `mtg:` (multi-select toggle) | ~1980 | `f"Toggled {idx}"` | The keyboard immediately redraws with the updated `☑/☐` mark — that IS the feedback. |
| `msub:` (multi-select submit, mid-chain) | ~2011 | `f"Q {idx + 1}/{total} submitted"` | A fresh question card lands ≤1s later. |
| `msub:` (multi-select submit, review screen) | ~2037 | `"Review — tap to finalise"` | The keyboard is edited in-place to Submit/Cancel. |
| `mfin:` (final submit / cancel) | ~2069 | `f"✅ {label} → {pane_id}"`, `show_alert=True` | Buttons removed; Claude TUI has the keystroke. |
| `qr:` (quick reply URL-button row) | ~2079 | `f"→ {pane_id}: {text}"` | Buttons removed. |

### Keep alert (no change)

| Path | Approx. line | Text | Why keep |
|---|---|---|---|
| pane-gone (multiple sites) | ~1876 / 1927 / 1992 / 2052 / 2074 / 2085 | `f"{pane_id} gone"` | User tapped a stale keyboard from a dead pane — needs to know nothing happened. |
| subprocess error (multiple sites) | various | `f"Failed: {e}"` | Action did NOT execute — must surface. |
| Phase 6 free-text refusal | ~1971 (post-fix-up) | `"Free-text option — attach to pane to type your answer."` | User's tap was REFUSED — alert is the right severity. |
| Phase 7 mid-chain advance failure | ~2078 (post-Phase-7) | `"⚠️ Next question failed — dialog reset."` | Dialog state was torn down; user must know. |
| `cancel:` (Ctrl-C) | ~2091 | `f"🛑 Ctrl-C → {pane_id}"` | Destructive — the user just sent SIGINT to a busy process. |

---

## Design principle codified

After this change, the bot follows one rule for callback acknowledgement:

> `query.answer()` with **no arguments** for success paths.
> `query.answer(text, show_alert=True)` for **error and refusal** paths only.

If a future reader is tempted to add a new success-path `show_alert=True`, the rule says: don't. If they're tempted to add a transient toast for a success ack, the rule says: prefer silent.

This rule will be added as a code comment near the top of the `on_callback` function in `tele_claude.py` so the convention is visible.

---

## Non-goals (deliberately out of scope)

- **Marking the tapped button visually before removal** (e.g. `✅ {label}` on the button text in `edit_message_reply_markup`). Adds keyboard-rebuild step on every tap. Defer until a user explicitly asks.
- **Editing the message body to record the choice** (e.g. appending `Answered: 1`). Adds HTML-edit complexity. Defer.
- **Refactoring `query.answer` calls into a helper.** Spec says find-and-replace at 7 sites. YAGNI on extraction.

---

## Risk assessment

**Low risk.** The change is per-call-site, not structural. Each edit is a 1-2 line diff. Reverting is a one-line revert per site if a regression surfaces.

**Telegram-side risk:** none. Bare `query.answer()` is a documented and supported call shape — used across most production bots for silent acks.

**Test surface:** The handler functions are async + depend on python-telegram-bot's `Update` / `Message` / `Query` objects. The Phase 0 pytest harness covers pure rendering helpers, not these handlers. We will NOT add tests for the toast-drop in this PR — verification is via live test on the running bot. Same trade-off accepted for Phases 2, 6, 7, 8.

---

## Verification

Live test recipe (post-merge of the resulting PR):

1. Restart bot service (`systemctl --user restart tele-claude.service`).
2. From the supergroup topic for any subscribed pane, trigger a permission prompt (any Claude tool that needs approval).
3. Tap **Allow once** (button 1).
   - **Pre-fix:** modal `ControlBot · ✅ Sent 1 → %P · OK` blocks UI until tapped.
   - **Post-fix:** buttons disappear, no modal, no toast.
4. Trigger an `AskUserQuestion` (single-select, ≥2 options). Tap any option.
   - **Pre-fix:** modal `Sent N → %P`.
   - **Post-fix:** buttons disappear silently.
5. Trigger a multi-select AUQ. Tap a non-free-text option. Toggle redraws to `☑`.
   - **Pre-fix:** transient toast `Toggled N`.
   - **Post-fix:** silent.
6. Tap the free-text (`✏️`) option in the same multi-select.
   - **Post-fix:** alert modal `Free-text option — attach to pane to type your answer.` STILL fires (refusal path, not success).
7. Tap a stale keyboard from a killed pane.
   - **Post-fix:** alert modal `%P gone` STILL fires (error path).

PASS = silent on the 5 success cases; alert preserved on the 2 control cases.

---

## Implementation outline (informs the writing-plans phase)

- One commit on a `fix/drop-success-toasts` branch.
- Edits in `tele_claude.py` only.
- ~14 LOC delta (7 call-sites × ~2 lines each).
- Add the convention comment block at the top of `on_callback`.
- Update `CHANGELOG.md` `[Unreleased]` with a `### Changed` entry referencing #53.
- Squash-merge → tag `v0.3.5` → restart bot.
- Live-test per the recipe above.
- Close #53 in the merge commit body via `Closes #53`.

---

## Self-review notes

- **Placeholder scan:** none. Every "approximate line" is approximate because Phase-by-Phase commits in 0.3.4 may have shifted line numbers; the implementation plan's `Edit` operations will use exact-string matching, not line numbers.
- **Internal consistency:** the "drop toast" table and the "keep alert" table cover all 11 `query.answer` call-sites in `tele_claude.py` (verified by grep). No call-site falls through both lists.
- **Scope check:** single subsystem (callback dispatch in `tele_claude.py`). Single PR. No decomposition needed.
- **Ambiguity:** the `mtg:` toggle was chosen for silent because the keyboard redraw IS the feedback. If a future reviewer wants a brief toast on toggle, they can add `query.answer(f"☑ {idx}")` without `show_alert` — the convention comment in `on_callback` discusses this trade-off.

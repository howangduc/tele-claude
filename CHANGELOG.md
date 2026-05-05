# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] — 2026-05-05

### Added

- **`/resume` command.** Pick a previous Claude Code session from `~/.claude/projects/` and spawn a new pane running `claude --resume <session-id>` in the project's cwd. Picker shows up to 12 newest-first sessions with project basename · age · first-prompt preview. `/resume <dir>` narrows by starts-with path match. Reuses the new ready-wait + auto-create-topic plumbing from `/new`. (#17)
- **`tele-claude doctor` subcommand.** Verifies install correctness with 7 checks (claude on PATH, alias resolves, `TELE_CLAUDE` propagates to a child shell, hook scripts present, env file present with required vars, tmux installed, python-telegram-bot importable). Each check prints `✓`/`✗` with an actionable fix hint on failure. New `tele_claude_cli.py` thin dispatcher so `doctor` runs without the bot's import-time env-var crash. (#15)

### Fixed

- **`!cmd` shell command output forwarding actually works now.** PR #5 patched `_last_assistant_text` for `<local-command-stdout>` envelopes — wrong layer, since Claude Code's `!`-REPL fires no Stop/progress/notify hooks at all (no LLM turn). Real fix lives in `tele_claude.on_message`: intercepts the `!` prefix, schedules an async transcript-tail task that polls for the matching `<bash-input>`/`<bash-stdout>` entries Claude wrote, replies with a 🐚 fenced block. (#1, real fix)
- **Multi-question `AskUserQuestion` now renders correctly.** Notification hook was firing before Claude flushed the matching `tool_use` JSONL entry, so `_find_pending_context` returned `None` and the body fell back to the generic `Allow / Always / Deny` keyboard. Wrapped `_find_pending_context` in a wait-for-stable poll (1.5s max, returns immediately if entry already on disk). Adds a breadcrumb to `~/.cache/tele-claude/debug/api-errors.log` if the wait still times out. (#8)
- **`!cmd` mid-turn forwards now refuse cleanly.** When Claude is mid-turn ("Beaming…"), `tmux send-keys "!ls"` keystrokes get absorbed as plain text instead of triggering the `!`-REPL handler, so no `<bash-input>` envelope ever lands and the forwarder finds nothing. Bot now captures the target pane's last lines, refuses with `⏸ %N is busy` if a spinner verb or `esc to interrupt` is visible. (#9)
- **`/new` auto-creates the forum topic AND waits for Claude TUI to be ready.** Two coupled gaps: no topic creation forced manual `/panes` step; fire-and-forget spawn replied "✅ Spawned" before Claude's TUI was listening, so the user's first message hit a TUI mid-banner-draw and got eaten. Now polls the pane for the `❯` prompt sentinel (max 6s) and posts the spawn ack INSIDE the new topic with either `🟢 Claude ready — chat away.` or `⏳ Still booting`. (#11)
- **Pane stays in `/panes` after topic deletion.** Bot never detected forum-topic deletion, so the cached `%pane → thread_id` mapping persisted forever; hook fallbacks landed in the main thread instead of recreating the topic. Now drops the stale mapping at three sites (`send_message` fallback, `_maybe_rename_topic` fallback, `_drop_dead_topic_mappings` invoked by `cmd_panes` reconciliation), so the next `/panes` rebuilds a fresh topic for the live pane. (#16)
- **Stranded ⏳ progress placeholder no longer dangles below the 🤖 reply.** Race between `main_reply` (Stop hook) and `main_subagent_stop` / `main_post_tool_use`: when a subagent finished during the Stop window, the concurrent hook used `_edit_or_resend_progress`'s "message not found" branch to send a fresh placeholder while `main_reply` was still in its delete-then-send loop. Fix: `main_reply` now snapshots + atomically clears progress state for all chats BEFORE entering the slow delete + send path, so concurrent hooks see `any_pending = False` and bail.
- **`/resume` no longer spawns a duplicate pane** when the picked session is already live. New `_find_live_pane_for_session(session_id)` checks (a) the pane → transcript mapping the hooks record, then (b) `/proc/<pane_pid>/cmdline` for `--resume <id>`. When found, re-binds the topic + active pane instead of spawning. (#23)

## [0.1.1] — 2026-05-04

### Added

- **Voice-note speech-to-text dictation.** Hold the microphone in Telegram, dictate, and the bot transcribes the audio and shows a `🎤 Transcript (%PANE)` card with **Send / Cancel** buttons before forwarding to the active pane. STT failures show a `❌ STT failed` card with **🔁 Retry** and **🔁 Try other provider** buttons; expired confirmations morph to `⌛ expired`. Implemented as a port-and-adapter shape: `SpeechToTextPort` ABC + `ElevenLabsAdapter` (Scribe `scribe_v1`) + `SelfHostedAdapter` (stub raising `NotImplementedError` until API docs are wired in). New env: `TELE_CLAUDE_STT_PROVIDER`, `TELE_CLAUDE_STT_API_KEY`, `TELE_CLAUDE_STT_BASE_URL`, `TELE_CLAUDE_STT_PENDING_TTL_SECONDS`, `TELE_CLAUDE_VOICE_DIR`. New explicit dep: `aiohttp>=3.9`. (#4)
- **`!`-shell-command output forwarding.** Shell commands invoked from a Telegram message now have their stdout/stderr forwarded back to the chat, so you can run quick lookups from your phone without attaching to the pane. (#5)

### Changed

- Install URL updated from `chiendo97/tele-claude` to `SCP120/tele-claude` — the new canonical standalone repo.

### Fixed

- Hatchling wheel packaging now includes sibling `tele_claude_*.py` modules (`tele_claude_constants.py`, `tele_claude_format.py`, `tele_claude_hooks.py`, `tele_claude_questions.py`, `tele_claude_speech.py`, `tele_claude_state.py`) so `uvx --from git+...` and other wheel-based installs ship a functional bot. Earlier rename of `constants.py` → `tele_claude_constants.py` was a partial fix; #6 closes the gap by adding explicit `[tool.hatch.build.targets.wheel]` includes. (#6)

## [0.1.0] — Initial release

- Telegram bot forwards messages to Claude Code tmux panes.
- Pane subscription model, mute/unmute, `/panes` picker, active-pane routing.
- Bidirectional integration via Claude Code hooks (`UserPromptSubmit`, `Stop`, `Notification`, `PostToolUse`, `SubagentStop`, `TeammateIdle`).
- Forum-mode (supergroup with Topics) for one-thread-per-pane.
- AskUserQuestion multi-question support, permission-prompt buttons.
- Photo/document attachment forwarding.
- `/new`, `/get`, `/cancel`, `/history`, `/shortcut` commands.

[Unreleased]: https://github.com/SCP120/tele-claude/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/SCP120/tele-claude/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/SCP120/tele-claude/releases/tag/v0.1.0

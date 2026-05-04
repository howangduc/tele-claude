# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

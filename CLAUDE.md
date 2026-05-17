# tele-claude — development guide for Claude Code

User-facing docs: `@README.md` · Changelog: `@CHANGELOG.md`

This file is the contributor onboarding guide. It captures **what Claude Code (or a new human contributor) cannot infer from the code**: versioning policy, packaging gotchas, and a few "always do this / never do this" rules.

## Project shape

Flat module layout — `tele_claude*.py` siblings at repo root, no package directory. Hatchling's `[tool.hatch.build.targets.wheel]` `include = ["tele_claude*.py"]` is what makes all sibling modules ship in the wheel.

| File | Role |
|---|---|
| `tele_claude.py` | Bot entrypoint (`tele-claude` script) |
| `tele_claude_cli.py` | CLI dispatcher (delegates to bot or `doctor`) |
| `tele_claude_doctor.py` | `tele-claude doctor` install diagnostics |
| `tele_claude_hooks.py` | Claude Code hook integration |
| `tele_claude_speech.py` | STT port + adapters (ElevenLabs / SelfHosted) |
| `tele_claude_state.py` | Pane subscriptions, mute state, pending confirmations |
| `tele_claude_questions.py` | Inline-button confirmation flows |
| `tele_claude_format.py` | Markdown → Telegram HTML formatter |
| `tele_claude_constants.py` | Shared constants |

## Versioning

Follows [Semantic Versioning](https://semver.org/). Version lives in `pyproject.toml`; changelog in `CHANGELOG.md` (Keep a Changelog format).

**Bump from commit prefix since last tag:**

| Commits since last release | Bump | Example |
|---|---|---|
| `[fix]` only | **patch** (`Z+1`) | `0.1.1` → `0.1.2` |
| `[feat]` (with or without `[fix]`) | **minor** (`Y+1.0`) | `0.1.1` → `0.2.0` |
| Big refactor / structural change / breaking API | **major** (`X+1.0.0`) | `0.1.1` → `1.0.0` |
| `[chore]` / `[docs]` only | none | — |

**Counts as major:** breaking change to bot CLI, env vars, or hook contract; massive refactor that renames public modules or rewrites architecture; removing/replacing a public adapter port.

**Pre-1.0 caveat:** while on `0.x`, a clearly-scoped breaking change MAY ride a minor bump — call it out under `### Changed` with a `**Breaking:**` prefix.

**Release checklist** (single commit / PR):

1. List commits since last tag: `git log $(git describe --tags --abbrev=0)..HEAD --oneline`.
2. Update `version` in `pyproject.toml`.
3. In `CHANGELOG.md`: rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`; add a fresh empty `## [Unreleased]` section above it.
4. If `pyproject.toml` deps changed: `uv lock` and commit `uv.lock` alongside.
5. Tag the release commit: `git tag vX.Y.Z`.

## Commit prefixes

`[feat]` (minor) · `[fix]` (patch) · `[refactor]` (patch — or major if structural/breaking) · `[chore]` (no bump) · `[docs]` (no bump).

## Build & test

```bash
uv sync                         # install dev deps
uv run ruff check .             # lint
uv run basedpyright             # type check
uv build                        # produce wheel + sdist in dist/
unzip -l dist/*.whl | grep tele_claude   # verify all sibling modules ship
```

There is no test suite yet; the doctor command (`uv run tele-claude doctor`) is the smoke test for installs.

## Always / never

- **Never** rename or nest `tele_claude_*.py` without updating `[tool.hatch.build.targets.wheel]` and verifying the built wheel — the auto-discovery picks one of `tele_claude.py` or `tele_claude/`, sibling modules silently drop otherwise.
- **Never** make `tele-claude doctor` import the bot eagerly. It must run with no env vars set — that's the broken-install state it exists to diagnose. Keep bot imports lazy in `tele_claude_cli.py`.
- **Always** add new STT providers behind `SpeechToTextPort` in `tele_claude_speech.py`; selection is via `TELE_CLAUDE_STT_PROVIDER`.
- **Always** preserve the subscription-gate behaviour: hooks must exit silently for unsubscribed panes (see `tele_claude_state.py`).

## Issue authoring (caveman mode)

GitHub issues filed against this repo (`howangduc/tele-claude` — both `gh issue create` and any `gh issue edit` of issue bodies) MUST be written in **caveman mode** — the compressed style provided by the `caveman` plugin (project-scope, see `.claude/settings.json`).

**Rules:**

- **Always** invoke `/caveman` (or its rules) before drafting an issue title or body for this repo. If `/caveman` is unavailable, refuse to create the issue and tell the user the plugin is missing — do **not** fall back to verbose prose.
- **Drop:** articles (a/the/an), filler phrases ("in order to", "it should be noted"), greetings, sign-offs, marketing voice.
- **Keep:** technical terms exact, file paths, line numbers, code blocks, command examples — these MUST stay precise.
- **Shape:** title stays in the existing `[bug] / [feat] / [fix]` prefix convention; body uses fragments, bullets over prose, `[thing] [action] [reason]. [next step].` pattern.
- **Scope:** rule applies only to issues filed against the tele-claude repos. PR descriptions, commit messages, and CHANGELOG entries follow their own conventions (verbose prose is fine there).

This is a deliberate forcing function — the maintainers prefer terse, scannable issues.

<!-- Maintainer notes (stripped from Claude's context):
     - Keep this file under 200 lines. Path-scoped rules → .claude/rules/ if it grows.
     - Personal preferences belong in CLAUDE.local.md (gitignored).
-->

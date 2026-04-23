# tele-claude

A Telegram bot that lets you send messages to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI sessions running in tmux panes — from your phone.

## How it works

```
You (Telegram)                    Server (tmux)
     |                                |
     |  /panes                        |
     |-----> Bot lists Claude Code    |
     |       panes running in tmux    |
     |                                |
     |  "%21 ~/Source/genbook-api"    |
     |  "%72 ~/Source/pipelines"      |
     |<----- one message per pane     |
     |                                |
     |  (reply to %21 message)        |
     |  "fix the failing test"        |
     |-----> Bot extracts %21,        |
     |       runs tmux send-keys  --->| Claude Code in pane %21
     |                                | receives "fix the failing test"
     |  "Sent to pane %21"            |
     |<----- confirmation             |
```

The bot detects Claude Code panes by matching `*claude*` against `pane_current_command` in tmux.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- tmux (with Claude Code sessions running in panes)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Save the bot token

### 2. Get your chat ID

1. Message your bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789}` in the response

### 3. Create the credentials file

Store the token and chat ID in a single config file so both the bot and the hooks (see below) can source it:

```bash
mkdir -p ~/.config/tele-claude
cat > ~/.config/tele-claude/env << 'EOF'
export CLAUDE_TELEGRAM_BOT_TOKEN="your-bot-token"
export CLAUDE_TELEGRAM_CHAT_ID="your-chat-id"
EOF
chmod 600 ~/.config/tele-claude/env
```

> **Multi-user ACL.** `CLAUDE_TELEGRAM_CHAT_ID` accepts a comma-separated list: `"123,456,789"`. Every listed chat can issue commands and receives forwarded notifications/replies. Anyone not on the list is ignored silently.

### 4. Run the bot

Run directly with `uvx` (no clone needed):

```bash
. ~/.config/tele-claude/env
uvx --from git+https://github.com/chiendo97/tele-claude tele-claude
```

Or clone and run locally:

```bash
git clone https://github.com/chiendo97/tele-claude.git
cd tele-claude
. ~/.config/tele-claude/env
uv run tele-claude
```

The package also exposes a standalone `tele-claude-format` script (defined in `[project.scripts]`). It reads markdown from stdin and writes Telegram HTML to stdout — handy for ad-hoc previews: `echo '# hi\n\n**bold**' | uv run tele-claude-format`.

Run it in a detached tmux session so it survives your terminal closing:

```bash
tmux new -d -s tele-claude '. ~/.config/tele-claude/env && uv run tele-claude'
tmux attach -t tele-claude   # view logs
```

#### Running as a systemd user service (recommended)

For a bot that survives reboots and auto-restarts on crashes, install it as a user service instead of tmux:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/tele-claude.service <<'EOF'
[Unit]
Description=tele-claude Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/tele-claude
ExecStart=/bin/bash -c '. %h/.config/tele-claude/env && exec uv run tele-claude'
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now tele-claude.service
loginctl enable-linger "$USER"   # keep the service running after logout
```

Operate it:

```bash
systemctl --user status tele-claude          # health
systemctl --user restart tele-claude         # after code changes
journalctl --user -u tele-claude -f          # tail logs
```

If `uv` isn't on the systemd PATH, replace `exec uv run tele-claude` with its absolute path (e.g. `exec /home/you/miniconda3/bin/uv run tele-claude`).

## Usage

### Commands

| Command | What it does |
|---------|--------------|
| `/panes` | Lists Claude Code panes as tappable buttons. Tapping a button sets the **active pane**. Each button's label shows the pane's current **tmux pane-title** — set automatically by our hooks to reflect live activity (`⏳ 7t · Bash +2a`, `🔐 Edit`, `🤖 Found 3 issues`, `💤 idle`) — so you can tell at a glance which pane is doing what without opening each. Muted panes show a 🔕 badge; the active pane shows ●. |
| `/use %N` | Sets `%N` as the active pane without going through the picker. Accepts `/use 2` too. |
| `/which` | Shows the current active pane. |
| `/pwd [%N]` | Shows the pane's live working directory (`pane_current_path`) as a tap-to-copy code block. Falls back to active pane if omitted. |
| `/new [dir]` | Spawn a fresh Claude pane in a **new detached tmux session** named `claude-<basename>` (collision-suffixed: `-2`, `-3`, …). With a directory arg, spawns immediately (accepts `~/foo` or absolute paths). **Bare `/new` prompts via ForceReply** (misclick-safe from the ☰ Menu) — reply with a dir, or `.` to use `$HOME`. The new session shows up in `tmux ls`; attach anytime with `tmux attach -t <name>` (or `switch-client` from inside tmux). Launches `cc` in the pane (user's alias = `claude --dangerously-skip-permissions`), auto-subscribes it, and makes it the active pane — the next Telegram message goes there without `/use`, no attach needed. Detached means your current terminal never gets yanked. |
| `/cancel [%N]` | Sends **Ctrl-C** to a pane (active pane if `%N` omitted). Stops a runaway turn from your phone. |
| `/mute %N` | Stops forwarding `Notification` + `Stop` hook messages for that pane (still subscribed). |
| `/unmute %N` | Resumes forwarding. |
| `/muted` | Lists currently muted panes. |
| `/subscribe %N` | Opts a pane into hook forwarding. Auto-triggered by any bot→pane send. |
| `/unsubscribe %N` | Removes a pane from forwarding. Hooks exit silently for it. |
| `/subscribed` | Lists subscribed panes. |
| `/history %N [lines]` | Captures the last N lines (default 20, max 500) of a pane and sends them as a preformatted block. |
| `/shortcut add <name> [desc]` | Registers a Claude slash-command so it shows up in Telegram's `/` autocomplete. Example: `/shortcut add sdlc Run the SDLC orchestrator`. |
| `/shortcut rm <name>` | Removes a shortcut from the menu. |
| `/shortcut list` | Shows all registered shortcuts. |

State (active pane, subscribed panes, mute list, shortcuts) persists in `~/.cache/tele-claude/state.json`, so bot restarts don't lose your selections. The same directory holds per-session ephemerals under `progress/`, `activity/`, `fingerprints/`, and cached inbound images under `images/`. Override the root with `export TELE_CLAUDE_STATE_DIR=/path` in `~/.config/tele-claude/env`.

### Subscription model — hooks only forward from panes you've interacted with

Even with `TELE_CLAUDE=1` set globally via the `claude` alias, **new tmux panes stay silent until you opt them in**. This prevents a newly-started Claude session from firing notifications at Telegram before you're ready for it.

A pane becomes **subscribed** the moment you:
- send any text, photo, or slash command to it via Telegram, OR
- tap its button in `/panes`, OR
- run `/use %N` or `/subscribe %N`

Hooks (`Notification`, `Stop`, `UserPromptSubmit`, `PostToolUse`, `SubagentStop`, `TeammateIdle`) check the subscription set first; unsubscribed panes exit the hook silently. Pair with `/mute` for temporary silencing that keeps the subscription, or `/unsubscribe` for a harder opt-out. Every `/panes` run calls `state.prune_panes()`, which sweeps dead pane IDs out of **all three** pane-keyed sections — `subscribed_panes`, `muted_panes`, and `active_pane` (per-chat) — so a closed pane never lingers in state.

Badges in `/panes`:
- `●` active + subscribed
- `🔔` subscribed, not active
- `🔕` subscribed, muted
- `·` alive but not subscribed (tap to opt in)

### Pane titles — live activity labels

Each hook fires a `tmux select-pane -T <title>` with a concise status summary so every pane gets a live, human-readable label:

| Event | Title format | Example |
|-------|--------------|---------|
| `UserPromptSubmit` | `⏳ <prompt preview 40c>` | `⏳ fix the auth race condition` |
| `PostToolUse` | `⏳ Nt · <last_tool>[ +Ka]` | `⏳ 7t · Bash +2a` (7 tools, last=Bash, 2 subagents running) |
| `SubagentStop` | refreshed same as PostToolUse | `⏳ 7t · Task +1a` |
| `Notification: permission_prompt` | `🔐 <ToolName>` | `🔐 Edit` |
| `Notification: idle_prompt` | `💤 idle` | `💤 idle` |
| `Stop` | `🤖 <first line of reply 40c>` | `🤖 Found 3 issues — shipping fix` |

These titles show up automatically in `/panes` button labels on Telegram. For visibility **inside tmux itself** (on pane borders), optionally add to `~/.tmux.conf`:

```tmux
set -g pane-border-status top
set -g pane-border-format " #{pane_id}  #{pane_title} "
```

Reload with `tmux source ~/.tmux.conf` or restart tmux. Not required — Telegram `/panes` shows the titles regardless.

> 💡 **Command menu.** The bot publishes this list to Telegram via `setMyCommands` on startup. Tap the **☰ Menu** button in the chat (or type `/`) to see all commands with descriptions and pick one by tap.

### Sending messages to Claude Code

Three ways — whichever feels most natural:

1. **Active-pane flow** *(recommended for long chats)* — pick a pane once via `/panes` or `/use %N`. Every normal message goes to that pane until you switch.
2. **Reply flow** — reply to any message that contains a pane ID (`%N`): bot confirmations, `/panes` listings, and 🤖 Claude replies all work.
3. **Quick-reply buttons** *(disabled by default; flip `_quick_reply_keyboard()` in `tele_claude_hooks.py` to re-enable)* — inline keyboard with `y` / `n` / `continue` / `/clear` / 🛑 ESC buttons under each 🤖 reply. Shipped off because plain typing into the chat turns out to be faster than tapping buttons for most flows.

### Triggering Claude slash commands from your phone

Claude Code's in-session commands (`/sdlc`, `/using-superpowers`, `/brainstorming`, anything from your skill set) can be invoked from Telegram in three complementary ways:

1. **Autocomplete-driven (zero typos, with args)** — in the Telegram chat, tap the text input and type `/`. Your registered shortcuts appear in the suggestion popup **above** the input bar. Tap one → command name is inserted into the input → finish the args → send. Manage the list with `/shortcut add <name> [description]`, `/shortcut rm <name>`, `/shortcut list`.
2. **☰ Menu-tap (one tap, then ForceReply prompts for args)** — tap the ☰ menu button next to the input bar and pick a shortcut. Telegram sends it immediately as a bare command. The bot detects it's a registered shortcut with no args and replies with a <kbd>ForceReply</kbd> prompt: *"Args for /sdlc?"*. Type your args and hit send — bot forwards `/sdlc <your args>` to the active pane. Send `.` (or any of `-`, `go`, `skip`, `bare`, empty) to forward bare.
3. **Direct typing** — send any message that starts with `/`. Anything unknown to the bot is forwarded verbatim. `/sdlc audit --fix` → pane receives exactly that.

**Why both #1 and #2?** Power users prefer autocomplete (fastest), but ☰ Menu is friendlier when typing is hard (driving, walking, small screen). The ForceReply prompt from #2 gives you a structured input field with a hint placeholder — no need to remember the command name a second time.

**Why the hyphen handling is tricky.** Telegram's bot command names are restricted to `[a-z0-9_]{1,32}` — **no hyphens allowed**. For shortcuts like `using-superpowers` the bot registers `using_superpowers` (underscores) in Telegram's menu, and the forwarding handler converts back to the canonical hyphenated form before sending to the pane. This is transparent to you — you see the Claude name in `/shortcut list`, Telegram shows the underscore version in its autocomplete, and the pane receives the hyphenated original.

**Preloaded shortcuts on fresh install.** None by default — run `/shortcut add <name>` for each Claude command you use often. Keep the list lean (5-10 favourites) so the autocomplete stays skimmable.

### Sending images to Claude Code

Attach any photo (or forward one) to the bot chat and it goes straight to the active pane — same resolution rules as text.

- **Photo** (compressed, default when you attach from gallery) and **Document** image (original quality, sent "as file") are both accepted
- Saved under `~/.cache/tele-claude/images/tg_<msg_id>_<file_unique_id>.<ext>` — deterministic, dedup-safe
- Claude Code picks up the absolute path and loads the image via its vision capability
- **With a caption**: the caption is sent first (on its own line), then the path — Claude reads the instruction and the image together: `check this screenshot for errors\n/home/you/.cache/tele-claude/images/tg_123_abc.jpg`
- **Without a caption**: just the path — follow up with text afterwards and Claude remembers the image
- Override destination via `export TELE_CLAUDE_IMAGE_DIR=/path/to/dir` in `~/.config/tele-claude/env`

> **Albums (multiple photos in one send):** Telegram delivers each as its own message; the bot forwards them one by one in order.

### Permission prompts from your phone

When Claude Code asks for permission, you get a 🔐 message with three buttons:

- **1 · Allow once** — sends `1` to the pane
- **2 · Always** — sends `2`
- **3 · Deny** — sends `3`

No need to type — tap and go. (The digits match Claude Code's default permission UI.)

**AskUserQuestion — single vs multi-select.** When Claude calls `AskUserQuestion` with `multiSelect=false` (default), each declared option becomes its own button labelled `1. <option label>`; one tap submits that choice. When `multiSelect=true`, the keyboard switches to a **toggle → submit → confirm** three-stage layout that mirrors Claude's TUI:

1. **Toggle stage.** Each option renders as a compact `☐ N` / `☑ N` button (full option text stays in the message body above), with a trailing `✅ Submit` button. Each toggle tap sends the matching digit keystroke — which Claude's TUI interprets as "flip this checkbox" — and the bot redraws the keyboard with the updated ☐/☑ glyphs so your phone mirrors the TUI state.
2. **Submit tap.** Tapping `✅ Submit` fires `Enter`, which advances the TUI from the toggle screen to its *"Review your answers — 1. Submit answers / 2. Cancel"* prompt.
3. **Final confirm.** Bot swaps the keyboard for two buttons: `✅ Submit answers` (sends `1`) and `❌ Cancel` (sends `2`). Tapping either fires the decisive keystroke and clears the keyboard.

Selection state during the toggle stage is encoded as a bitmask in `callback_data` (fits Telegram's 64-byte limit for up to 8 options), so there's no server-side session to lose across bot restarts.

### Progress indicator

When the bot forwards your prompt to a pane, Claude Code's `UserPromptSubmit` hook immediately replies with an ⏳ placeholder message that includes a preview of what you sent. When Claude finishes its turn, the `Stop` hook **edits that same message** into the formatted response. You get one message per turn that evolves in place — no clutter.

## Optional: Claude Code integration (bidirectional)

The base bot is one-way: Telegram → tmux. Three Claude Code hooks close the loop:

| Hook | Fires when | What it does |
|------|------------|--------------|
| `UserPromptSubmit` | You (or the bot) submit a prompt to Claude | Sends an ⏳ placeholder to Telegram with a preview |
| `PostToolUse` | After every tool call Claude runs | Throttled heartbeat that edits ⏳ with `Working… N tool calls, last: <Tool>` + a preview of the most recent text block. Prevents long agent turns from going radio-silent. |
| `SubagentStop` | A Task-spawned subagent finishes | Immediately refreshes the heartbeat, adds `✓ subagent done: <agent_type>` line, and lets the running-subagents list shrink without waiting for the PostToolUse throttle. |
| `TeammateIdle` | An Agent Team teammate is about to go idle (experimental — requires `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) | Sends a fresh 🧑‍💻 push notification with the teammate name + role + last message preview, so you know to send them more work. |
| `Stop` | Claude finishes a turn | Edits that placeholder into the full response (or sends new if no placeholder) |
| `Notification` | Claude needs attention (permission, idle) | Sends 🔐 / 💤 to Telegram — permission prompts include Allow/Always/Deny buttons |

All three share an opt-in gate: they exit silently unless `TELE_CLAUDE=1` is set on the Claude process. Aliasing `claude` to set that flag makes forwarding on-by-default; opting out for a single session is `command claude` or `TELE_CLAUDE=0 claude`.

```
Claude in pane %21                        Telegram
       |                                       |
       | (you tap the active pane message)     |
       | <---- "fix the failing test" ---------|  bot → tmux send-keys
       |                                       |
       | UserPromptSubmit ─────── ⏳ placeholder ──────→  ⏳ project · %21
       |                                                   "fix the failing test"
       |                                       |
       | Claude runs tools, reasons, writes…   |
       |                                       |
       | Stop ──────── edits placeholder ───→  🤖 project · %21
       |                                                   "<formatted response>"
       |                                                   [🔗 Open PR #42]
       |                                       |
       | (you type a reply in the chat)        |
       | <------------- "keep going" ----------|  bot → tmux send-keys
       |                                       |
       | Notification (permission) ─── 🔐 ───→  🔐 Permission needed · %21
       |                                                   [1 Allow once] [2 Always] [3 Deny]
       | <------------- "1" -------------------|  callback → tmux send-keys
```

### 1. Reuse the credentials file

All hooks source `~/.config/tele-claude/env` (created in [Setup → step 3](#3-create-the-credentials-file)) so you don't need to export secrets in every shell.

### 2. Make `claude` forward by default

Append to `~/.bashrc` (or `~/.zshrc`):

```bash
# tele-claude: every `claude` forwards to Telegram by default
alias claude='TELE_CLAUDE=1 command claude'
```

Usage:

| Command | Behavior |
|---------|----------|
| `claude` | Forwards notifications + responses to Telegram |
| `command claude` | Runs Claude silently (bypasses the alias) |
| `TELE_CLAUDE=0 claude` | One-off silent override |

> Prefer explicit opt-in? Use `tclaude() { TELE_CLAUDE=1 claude "$@"; }` and invoke `tclaude` on the sessions you want forwarded, plain `claude` for the rest.

### 3. Install the hook wrappers

Hook logic lives in Python modules in this repo (`tele_claude_hooks.py`, `tele_claude_format.py`, `tele_claude_state.py`). The shell hooks are thin wrappers that exec the Python module — keeps curl/jq complexity out of bash and lets the hooks build inline keyboards, dedup, and split long messages.

Save as `~/.claude/hooks/telegram-notify.sh`:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" notify
```

Save as `~/.claude/hooks/telegram-reply.sh`:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" reply
```

Save as `~/.claude/hooks/telegram-progress.sh`:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" progress
```

Save as `~/.claude/hooks/telegram-post-tool-use.sh`:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" post_tool_use
```

Save as `~/.claude/hooks/telegram-subagent-stop.sh`:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" subagent_stop
```

Save as `~/.claude/hooks/telegram-teammate-idle.sh` *(only useful if you set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and use Claude Code's Agent Teams feature)*:

```bash
#!/usr/bin/env bash
[ "${TELE_CLAUDE:-}" = "1" ] || exit 0
CONFIG="$HOME/.config/tele-claude/env"
[ -f "$CONFIG" ] && . "$CONFIG"
HOME_DIR="${TELE_CLAUDE_HOME:-$HOME/tele-claude}"
exec python3 "$HOME_DIR/tele_claude_hooks.py" teammate_idle
```

Make them executable:

```bash
chmod +x ~/.claude/hooks/telegram-{notify,reply,progress,post-tool-use,subagent-stop,teammate-idle}.sh
```

> **Where the repo lives.** All three wrappers look for the Python module at `~/tele-claude/tele_claude_hooks.py`. Cloned elsewhere? Add `export TELE_CLAUDE_HOME=/path/to/repo` to `~/.config/tele-claude/env`.

### 4. Register the hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-notify.sh" }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-reply.sh" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-progress.sh" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-post-tool-use.sh" }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-subagent-stop.sh" }
        ]
      }
    ],
    "TeammateIdle": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/hooks/telegram-teammate-idle.sh" }
        ]
      }
    ]
  }
}
```

### 5. Try it

```bash
source ~/.bashrc       # picks up the claude alias
tmux new -s work       # or attach an existing pane
claude                 # forwarding is on by default
```

From Telegram:

1. `/panes` → tap your pane to activate it
2. Send any message → goes straight to the pane (no reply-to needed)
3. ⏳ placeholder appears immediately with a preview of your prompt
4. When Claude finishes, the placeholder edits in place to the formatted 🤖 response with inline buttons for quick replies and any links in the output
5. Tap `continue`, `y`, `/clear`, or any `🔗 Open …` button to act — or type a new message

### What the reply hook does for you

- **Markdown → Telegram HTML** via `tele_claude_format.py` (headers → bold, `**foo**` → `<b>foo</b>`, fenced blocks → `<pre><code>`). **Tables adapt to width**: narrow tables (≤ 34 chars total) render as aligned `<pre>` monospace blocks with Unicode separators; wider tables flatten to vertical bullet blocks (`• <b>label</b>\n  → value` for 2-col, labeled sub-lines for 3+) because Telegram mobile wraps `<pre>` instead of scrolling horizontally. Tune via `_PRE_MAX_WIDTH` in the formatter
- **HTML-aware smart split** — markdown is chunked, each chunk is converted to HTML, and the raw-markdown budget shrinks iteratively (2500 → 800 chars) until every chunk's HTML form fits under Telegram's 4096-char limit. Prevents silent 400s from tag-inflated messages
- **Race-safe transcript read** — Claude Code's JSONL writer is buffered, so the Stop hook can fire a few hundred ms before the final text block is flushed to disk. The hook polls the transcript until two consecutive reads return the same text (max 1.5 s), guaranteeing it sees the complete turn
- **Typing indicator while Claude works** — the UserPromptSubmit hook detaches a pumper subprocess that re-sends `sendChatAction=typing` every 4 s so Telegram shows a live "is typing…" status in the chat header. The pumper exits automatically the instant the Stop hook clears the progress file (≤ 4 s lag) or after a hard 10-min cap if Stop never fires
- **Idle-prompt throttling** — Claude Code's `idle_prompt` fires on a hardcoded 60 s timer ([feature request](https://github.com/anthropics/claude-code/issues/13922) for configurability still open). The notify hook suppresses it unless the session has been genuinely silent for `$TELE_CLAUDE_IDLE_MIN_SECONDS` (default **900 s / 15 min**). Both `UserPromptSubmit` and `Stop` hooks count as activity. Override per install by adding e.g. `export TELE_CLAUDE_IDLE_MIN_SECONDS=600` to `~/.config/tele-claude/env`
- **Dedup** — identical bodies sent within 5 s for the same session are skipped (silences re-fires)
- **Mute aware** — panes muted via `/mute %N` get no reply hook at all
- **URL extraction** — up to 4 `http(s)://` links in the reply become `🔗 Open <last-path-segment>` buttons under the message
- **Quick-reply keyboard** *(disabled by default)* — `y` / `n` / `continue` / `/clear` / 🛑 ESC buttons; `tele_claude_hooks.py:_quick_reply_keyboard` returns `[]` — flip back to a populated list to re-enable

### Caveats

- **Per-session ⏳/🤖 editing** requires the UserPromptSubmit + Stop pair — if only Stop fires, you get a new message instead of an edit. Harmless but noisier.
- **Text blocks only** — tool calls and tool results are not forwarded (keeps noise down).
- **`TELE_CLAUDE` is session-scoped** — subagents and background jobs spawned from that session inherit it.
- **Shell aliases only apply to interactive shells** — scripts that call `claude` non-interactively bypass the alias and run silently. That's usually what you want.
- **Progress/dedup state lives in `~/.cache/tele-claude/`** — delete that directory to reset everything.

## Security notes

- Only chats listed in `CLAUDE_TELEGRAM_CHAT_ID` (comma-separated) are processed; everyone else is ignored silently
- Text is sent to tmux using the `-l` (literal) flag to prevent shell metacharacter interpretation
- The bot uses long polling (no webhook/exposed server needed)
- Callback data on inline buttons includes the `%PANE` it targets — the bot validates the pane still exists before acting
- Credentials live in `~/.config/tele-claude/env` (chmod 600) and a single `Cache-Control` state file at `~/.cache/tele-claude/state.json`

## Optional: forum-mode topics (one thread per pane)

When the bot runs inside a Telegram **supergroup with Topics enabled**, each Claude pane gets its own forum topic — so concurrent panes no longer interleave in a single scrollback. Replies and notifications from pane `%15` land in topic `%15 · …`, `%9`'s land in `%9 · …`, etc. Posting a message *inside* a topic auto-routes to that pane with no `/use` dance. Disable by leaving `TELE_CLAUDE_SUPERGROUP_ID` unset — the bot silently falls back to single-thread mode.

### Setup

1. **Create a new supergroup** in Telegram (private chat → can't enable topics)
2. **Enable Topics** in the supergroup settings
3. **Add the bot as admin** with these permissions:
   - `Manage topics` (required for create/edit/delete)
   - `Delete messages` (required for removing dead-pane topics)
4. **Get the supergroup id** — send any message in the group, visit `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy the negative `chat.id` (e.g. `-1001234567890`)
5. **Set the env var** in `~/.config/tele-claude/env`:
   ```bash
   export TELE_CLAUDE_SUPERGROUP_ID="-1001234567890"
   # Add the supergroup id to the auth list too — the bot only forwards to
   # chats listed in CLAUDE_TELEGRAM_CHAT_ID.
   export CLAUDE_TELEGRAM_CHAT_ID="123456789,-1001234567890"
   ```
6. **Restart the bot** (`systemctl --user restart tele-claude`). On startup it calls `getChat` + `getChatMember` to verify the chat is a forum and the bot has the right admin perms; any missing piece is logged as a warning (non-fatal — the bot keeps running in single-thread mode for other configured chats).
7. **Run `/panes`** once in the supergroup — the bot creates one topic for every currently-live Claude pane.

### Topic naming

Each topic is named `%N · <live tmux title> · <full cwd>`:

| Pane state | Example topic name |
|------------|--------------------|
| Working | `%15 · ⏳ 7t · Bash +2a · /home/you/Source/foo` |
| Idle | `%9 · 💤 idle · /home/you/tele-claude` |
| Permission pending | `%21 · 🔐 Edit · /home/you/genbook-mono` |
| Just replied | `%21 · 🤖 Found 3 issues · /home/you/genbook-mono` |

The name **refreshes every N Claude turns** so the topic list mirrors live activity without hammering Telegram's `editForumTopic` rate limit. Throttling is two-gated:

- **Turn counter** — a per-pane counter increments on each Stop hook; rename fires when the counter hits `TELE_CLAUDE_TOPIC_RENAME_EVERY` (default **15**, min 1). Override by adding e.g. `export TELE_CLAUDE_TOPIC_RENAME_EVERY=10` to `~/.config/tele-claude/env`.
- **Change detection** — skipped entirely when the composed name already matches the last-cached one (no-op API call avoided).

### Lifecycle

- **Lazy creation** — topics are created on first hook fire for a pane (or when `/panes` reconciles, whichever comes first). No migration command needed; `/panes` in the supergroup seeds topics for all currently-live panes.
- **Bidirectional routing** — post a message inside topic `%15 · …` and it goes straight to pane `%15`. The topic you're viewing *is* the active pane; `/use %N` and reply-to still work as fallbacks from the main thread.
- **Auto-delete on pane death** — `/panes` prunes dead panes from state and calls `deleteForumTopic` in the same pass. The topic is gone; scrollback with it. Matches the user's "don't keep zombies" preference.
- **Resilient to manual deletion** — if you delete a topic by hand in Telegram, the next hook send retries without the stale thread id (landing in the main thread); `/panes` cleans up the orphan state on its next run.

### Private chat silence (forum-exclusive mode)

By default, enabling forum mode silences hook notifications to your **private DM with the bot** — the supergroup topic already pings you, and double-notifying is just noise. Bot commands from the private chat still work (authorisation is independent); only the outbound hook messages (⏳, 🤖, 🔐, 💤) stop landing there.

To restore the old fan-out (every authorised chat gets every hook), set:

```bash
export TELE_CLAUDE_FORUM_EXCLUSIVE=0
```

in `~/.config/tele-claude/env` and restart the bot.

### Per-pane state (debug)

`state.json` adds two keys when forum mode is active:

```jsonc
{
  "pane_topics":       {"%9": 47, "%15": 53},
  "pane_topic_names":  {"%9": "%9 · 💤 idle · /home/you/tele-claude"}
}
```

`pane_topic_names` is a one-entry cache per pane so the Stage-3 renamer can skip the API call when the composed name hasn't changed — debug-friendly but doesn't need manual edits.

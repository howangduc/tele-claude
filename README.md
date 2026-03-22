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

### 3. Set environment variables

```bash
export CLAUDE_TELEGRAM_BOT_TOKEN="your-bot-token"
export CLAUDE_TELEGRAM_CHAT_ID="your-chat-id"
```

### 4. Run the bot

Run directly with `uvx` (no clone needed):

```bash
uvx --from git+https://github.com/chiendo97/tele-claude tele-claude
```

Or clone and run locally:

```bash
git clone https://github.com/chiendo97/tele-claude.git
cd tele-claude
uv run tele-claude
```

## Usage

### `/panes` command

Lists all tmux panes currently running Claude Code. Each pane is sent as a separate message showing the pane ID and working directory:

```
%21 ~/Source/genbook-api
%72 ~/Source/pipelines
```

### Sending messages to Claude Code

Reply to any message containing a tmux pane ID (e.g. `%21`) with the text you want to send. The bot will feed it into that pane via `tmux send-keys`.

## Optional: Claude Code notification hook

You can configure Claude Code to send Telegram notifications when it needs your attention (permission prompts, idle, etc.) — and reply directly to those notifications to send input back.

### 1. Create the hook script

Save this as `~/.claude/hooks/telegram-notify.sh`:

```bash
#!/usr/bin/env bash
BOT_TOKEN="${CLAUDE_TELEGRAM_BOT_TOKEN:?Missing CLAUDE_TELEGRAM_BOT_TOKEN}"
CHAT_ID="${CLAUDE_TELEGRAM_CHAT_ID:?Missing CLAUDE_TELEGRAM_CHAT_ID}"

INPUT=$(cat)
NOTIFICATION_TYPE=$(echo "$INPUT" | jq -r '.notification_type // "unknown"')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

case "$NOTIFICATION_TYPE" in
  permission_prompt)  EMOJI="🔐"; LABEL="Permission needed" ;;
  idle_prompt)        EMOJI="💤"; LABEL="Waiting for input" ;;
  *)                  EMOJI="🔔"; LABEL="Notification" ;;
esac

PROJECT="${CWD:+$(basename "$CWD")}"
PANE_ID="${TMUX_PANE:-}"

TEXT="${EMOJI} <b>${LABEL}</b>"
[ -n "$PROJECT" ] && TEXT="${TEXT}  ·  <code>${PROJECT}</code>"
[ -n "$PANE_ID" ] && TEXT="${TEXT}  ·  <code>${PANE_ID}</code>"

curl -s --connect-timeout 5 --max-time 10 \
  -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=$CHAT_ID" \
  --data-urlencode "parse_mode=HTML" \
  --data-urlencode "text=$TEXT" > /dev/null 2>&1
```

Make it executable:

```bash
chmod +x ~/.claude/hooks/telegram-notify.sh
```

### 2. Configure Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/telegram-notify.sh"
          }
        ]
      }
    ]
  }
}
```

Now when Claude Code sends a notification like:

```
🔐 Permission needed  ·  genbook-api  ·  %21
```

You can reply to it directly in Telegram, and the bot will forward your message to pane `%21`.

## Security notes

- Only messages from the configured `CLAUDE_TELEGRAM_CHAT_ID` are processed
- Text is sent to tmux using the `-l` (literal) flag to prevent shell metacharacter interpretation
- The bot uses long polling (no webhook/exposed server needed)

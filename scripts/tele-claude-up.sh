#!/bin/bash
# Keeps the `tele-claude` tmux session always alive — the Python Telegram bot
# that bridges Telegram <-> Claude Code panes. Invoked by
# ~/Library/LaunchAgents/com.savyu.tele-claude.plist at login and every 60s.
# If the session is missing, recreates it with one window running `uv run tele-claude`.
set -e
TMUX=/opt/homebrew/bin/tmux
REPO="$HOME/Projects/tele-claude"

if "$TMUX" has-session -t tele-claude 2>/dev/null; then
  exit 0
fi

"$TMUX" new-session -d -s tele-claude -n bot -c "$REPO"
"$TMUX" send-keys -t tele-claude:bot "set -a; . $HOME/.config/tele-claude/env; set +a; exec uv run tele-claude" Enter

#!/bin/bash
# Keeps the `tele-claude` tmux session always alive — the Python Telegram bot
# that bridges Telegram <-> Claude Code panes. Invoked by
# ~/Library/LaunchAgents/com.savyu.tele-claude.plist at login and every 60s.
# If the session is missing, recreates it with one window running `uv run tele-claude`.
set -e
# NB: variable is TMUX_BIN, not TMUX — `TMUX` is reserved by tmux itself
# for its socket path. Overwriting it breaks manual restarts from inside
# any existing tmux session ("Socket operation on non-socket").
TMUX_BIN=/opt/homebrew/bin/tmux
REPO="$HOME/Projects/tele-claude"

if "$TMUX_BIN" has-session -t tele-claude 2>/dev/null; then
  exit 0
fi

"$TMUX_BIN" new-session -d -s tele-claude -n bot -c "$REPO"
"$TMUX_BIN" send-keys -t tele-claude:bot "set -a; . $HOME/.config/tele-claude/env; set +a; exec uv run tele-claude" Enter

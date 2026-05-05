"""Thin CLI dispatcher for the ``tele-claude`` console script.

Why a dispatcher and not just ``tele_claude:main``? The bot module
reads ``CLAUDE_TELEGRAM_BOT_TOKEN`` at import time, so importing it
crashes when the env file isn't set up yet — which is precisely the
state ``tele-claude doctor`` needs to diagnose. Dispatching here lets
``doctor`` (and ``--help`` / ``-h``) run without touching the bot core.

Subcommands:
  ``tele-claude``           — start the bot (default; existing behavior).
  ``tele-claude doctor``    — run install verification checks.
  ``tele-claude --help``    — print usage.
"""

from __future__ import annotations

import sys

_USAGE = """\
Usage: tele-claude [SUBCOMMAND]

Subcommands:
  (none)    Start the Telegram bot (requires CLAUDE_TELEGRAM_BOT_TOKEN +
            CLAUDE_TELEGRAM_CHAT_ID env vars; the bot reads
            ~/.config/tele-claude/env via the wrapper shells).
  doctor    Verify the install: alias resolution, TELE_CLAUDE propagation,
            hook scripts, credentials file, tmux + python-telegram-bot.
  -h, --help
            Show this help and exit.
"""


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(_USAGE, end="")
        return 0
    if args and args[0] == "doctor":
        # Lazy import so doctor still runs when the bot env vars are
        # missing (the exact state we're diagnosing).
        from tele_claude_doctor import main as doctor_main

        return doctor_main()
    if args:
        print(f"tele-claude: unknown subcommand: {args[0]}\n", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2
    # No subcommand → run the bot as before. Import lazily so any
    # ImportError (missing env vars, missing deps) only fires when the
    # user actually meant to start the bot.
    from tele_claude import main as bot_main

    bot_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

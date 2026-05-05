"""Verify the tele-claude install is wired up correctly.

Invoked as ``tele-claude doctor`` (dispatched by ``tele_claude_cli``).

Each check prints one line with ✓ or ✗ followed by a short status; on
✗ a hint suggests the fix. Designed to surface the most common reasons
hooks silently no-op:

  * ``claude`` not on PATH at all.
  * The ``alias claude='TELE_CLAUDE=1 command claude'`` step from the
    README never ran (or the user's ``~/.bashrc`` never sourced it).
  * The alias is set up, but ``TELE_CLAUDE`` is not actually exported
    into the resulting child process (e.g. alias was wrong, or shell
    ate the assignment).
  * Hook scripts missing under ``~/.claude/hooks/``.
  * ``~/.config/tele-claude/env`` missing or missing required keys.
  * ``tmux`` or ``python-telegram-bot`` not installed.

Doctor never imports the bot core (``tele_claude``) so it stays
runnable even when ``CLAUDE_TELEGRAM_BOT_TOKEN`` / ``CLAUDE_TELEGRAM_CHAT_ID``
are missing — that's the exact state we need to diagnose.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- Output helpers ----------

# Use unicode tick/cross when stdout is a TTY that likely supports it;
# fall back to ASCII for log capture / dumb terminals.
_USE_UNICODE = sys.stdout.encoding and sys.stdout.encoding.lower().startswith("utf")
_OK = "✓" if _USE_UNICODE else "[OK]"
_BAD = "✗" if _USE_UNICODE else "[FAIL]"


def _print_check(ok: bool, label: str, detail: str, hint: str | None = None) -> None:
    """Print one check result; on failure also print the hint indented."""
    mark = _OK if ok else _BAD
    print(f"{mark} {label}: {detail}")
    if not ok and hint:
        print(f"   hint: {hint}")


# ---------- Individual checks ----------


def check_claude_on_path() -> bool:
    """Is the ``claude`` binary resolvable from PATH?"""
    path = shutil.which("claude")
    if path:
        _print_check(True, "claude on PATH", path)
        return True
    _print_check(
        False,
        "claude on PATH",
        "not found",
        "install Claude Code (https://docs.claude.com/en/docs/claude-code) "
        "and ensure its bin directory is on PATH.",
    )
    return False


def _run_login_shell(cmd: str) -> tuple[int, str, str]:
    """Run ``cmd`` inside the user's interactive login shell.

    Returns ``(returncode, stdout, stderr)``. Uses ``$SHELL -i -c`` so
    aliases defined in ``~/.bashrc`` / ``~/.zshrc`` are loaded — that's
    the exact same environment a fresh terminal sees, which is what we
    want to verify.
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    proc = subprocess.run(
        [shell, "-i", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def check_claude_alias() -> bool:
    """Is ``claude`` an alias that sets ``TELE_CLAUDE=1``?

    Runs ``type claude`` inside an interactive shell so aliases from
    ``~/.bashrc`` / ``~/.zshrc`` are visible. The alias text must
    contain ``TELE_CLAUDE=1`` to count as wired up.
    """
    try:
        rc, out, err = _run_login_shell("type claude 2>&1")
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _print_check(
            False,
            "claude alias resolves",
            f"could not run interactive shell: {exc}",
            "set $SHELL to your interactive shell (bash/zsh) and re-run.",
        )
        return False
    text = (out + err).strip()
    if rc != 0:
        _print_check(
            False,
            "claude alias resolves",
            text or "shell exited non-zero",
            "open a fresh terminal and run `type claude` manually to see why.",
        )
        return False
    if "TELE_CLAUDE=1" in text:
        _print_check(True, "claude alias resolves", text.splitlines()[0])
        return True
    if "alias" in text:
        _print_check(
            False,
            "claude alias resolves",
            f"alias exists but missing TELE_CLAUDE=1: {text.splitlines()[0]}",
            "edit ~/.bashrc and set: alias claude='TELE_CLAUDE=1 command claude' "
            "then `source ~/.bashrc`.",
        )
        return False
    _print_check(
        False,
        "claude alias resolves",
        f"no alias defined: {text.splitlines()[0]}",
        "add to ~/.bashrc (or ~/.zshrc): alias claude='TELE_CLAUDE=1 command claude' "
        "then `source ~/.bashrc`.",
    )
    return False


def check_tele_claude_propagates() -> bool:
    """Does ``TELE_CLAUDE=1`` actually reach a child process?

    Spawns ``$SHELL -i -c 'env'`` and greps the output for
    ``TELE_CLAUDE=1``. Catches the case where the alias technically
    exists but the assignment doesn't survive — e.g. zsh global aliases,
    bash ``shopt -u expand_aliases``, or a stray `export TELE_CLAUDE=`
    later in the rc file.

    Note: the alias ``alias claude='TELE_CLAUDE=1 command claude'`` only
    sets ``TELE_CLAUDE`` in the environment of the *expanded* command
    (``claude``), not in the ambient shell. So we check by simulating
    the alias expansion: run a child shell that itself runs
    ``alias claude && claude --version`` semantics by invoking
    ``TELE_CLAUDE=1 env`` inside the alias-expanded form.
    """
    cmd = (
        # Force alias expansion in the non-interactive subshell that
        # `bash -i -c` spawns by enabling expand_aliases explicitly.
        "shopt -s expand_aliases 2>/dev/null; "
        "alias claude >/dev/null 2>&1 && "
        # Replace `claude` in the alias body with `env` so we can read
        # the env vars that WOULD be set on a real claude invocation.
        # bash's `alias` prints in the form: alias claude='TELE_CLAUDE=1 command claude'
        # so eval the body with claude swapped to env.
        'eval "$(alias claude | sed -E '
        "\"s/^alias claude=//; s/^'//; s/'\\$//; "
        's/command claude/env/; s/[[:space:]]claude\\$/ env/")" '
        "| grep '^TELE_CLAUDE='"
    )
    try:
        rc, out, err = _run_login_shell(cmd)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _print_check(
            False,
            "TELE_CLAUDE propagates to child",
            f"could not run interactive shell: {exc}",
            "set $SHELL to your interactive shell (bash/zsh) and re-run.",
        )
        return False
    line = (out or err).strip()
    if rc == 0 and line.startswith("TELE_CLAUDE=1"):
        _print_check(True, "TELE_CLAUDE propagates to child", line)
        return True
    _print_check(
        False,
        "TELE_CLAUDE propagates to child",
        line or "TELE_CLAUDE not set in child env",
        "the alias exists but doesn't export TELE_CLAUDE=1 to the child. "
        "Confirm the alias body matches: TELE_CLAUDE=1 command claude.",
    )
    return False


_HOOK_SCRIPTS = (
    "telegram-notify.sh",
    "telegram-reply.sh",
    "telegram-progress.sh",
    "telegram-post-tool-use.sh",
    "telegram-subagent-stop.sh",
)


def check_hook_scripts() -> bool:
    """Are the hook wrapper scripts installed under ~/.claude/hooks/?"""
    hooks_dir = Path.home() / ".claude" / "hooks"
    missing = [name for name in _HOOK_SCRIPTS if not (hooks_dir / name).exists()]
    if not missing:
        _print_check(
            True,
            "hook scripts present",
            f"all {len(_HOOK_SCRIPTS)} found in {hooks_dir}",
        )
        return True
    _print_check(
        False,
        "hook scripts present",
        f"missing: {', '.join(missing)}",
        "follow README → 'Install the hook wrappers' to create them, "
        "then `chmod +x ~/.claude/hooks/telegram-*.sh`.",
    )
    return False


_REQUIRED_ENV_KEYS = ("CLAUDE_TELEGRAM_BOT_TOKEN", "CLAUDE_TELEGRAM_CHAT_ID")


def check_credentials_file() -> bool:
    """Is ``~/.config/tele-claude/env`` present and minimally populated?"""
    env_path = Path.home() / ".config" / "tele-claude" / "env"
    if not env_path.exists():
        _print_check(
            False,
            "credentials file",
            f"{env_path} missing",
            "follow README → 'Create the credentials file' to seed it with "
            "CLAUDE_TELEGRAM_BOT_TOKEN and CLAUDE_TELEGRAM_CHAT_ID.",
        )
        return False
    contents = env_path.read_text(errors="replace")
    missing = [key for key in _REQUIRED_ENV_KEYS if f"{key}=" not in contents]
    if missing:
        _print_check(
            False,
            "credentials file",
            f"{env_path} missing keys: {', '.join(missing)}",
            "add the missing key(s); see README → 'Create the credentials file'.",
        )
        return False
    _print_check(True, "credentials file", str(env_path))
    return True


def check_tmux_installed() -> bool:
    """Is the ``tmux`` binary available?"""
    path = shutil.which("tmux")
    if path:
        _print_check(True, "tmux installed", path)
        return True
    _print_check(
        False,
        "tmux installed",
        "not found",
        "install tmux via your package manager (apt/brew/pacman). The bot "
        "uses tmux to send keystrokes to Claude panes.",
    )
    return False


def check_python_telegram_bot() -> bool:
    """Is ``python-telegram-bot`` importable in the active Python?"""
    spec = importlib.util.find_spec("telegram")
    if spec is None:
        _print_check(
            False,
            "python-telegram-bot installed",
            "not importable",
            "install with `uv pip install python-telegram-bot` (or `pip`); "
            "doing `uv tool install .` from the repo also satisfies this.",
        )
        return False
    # Show the source path so users can tell which interpreter has it.
    location = spec.origin or "(no origin)"
    _print_check(True, "python-telegram-bot installed", location)
    return True


def check_pin_perm() -> bool:
    """Verify the bot has can_pin_messages in the supergroup.

    Required for the TodoWrite pinned-card feature (issue #28). Skipped
    (returns OK) when forum mode isn't enabled — pinning falls back to
    the originating chat in that case and the user has personal-chat
    pin slot semantics anyway.
    """
    chat_id = os.environ.get("TELE_CLAUDE_SUPERGROUP_ID", "").strip()
    bot_token = os.environ.get("CLAUDE_TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id:
        _print_check(
            True,
            "bot can_pin_messages",
            "forum mode disabled (TELE_CLAUDE_SUPERGROUP_ID unset) — skipped",
        )
        return True
    if not bot_token:
        _print_check(
            False,
            "bot can_pin_messages",
            "CLAUDE_TELEGRAM_BOT_TOKEN missing",
            "set CLAUDE_TELEGRAM_BOT_TOKEN in ~/.config/tele-claude/env.",
        )
        return False
    try:
        import json as _json
        import urllib.parse
        import urllib.request

        # getMe → bot user id; getChatMember → can_pin_messages
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{bot_token}/getMe", timeout=5
        ) as r:
            me = _json.loads(r.read().decode())
        if not me.get("ok"):
            _print_check(
                False,
                "bot can_pin_messages",
                f"getMe failed: {me.get('description', 'unknown')}",
                "check CLAUDE_TELEGRAM_BOT_TOKEN is valid.",
            )
            return False
        bot_id = me["result"]["id"]
        params = urllib.parse.urlencode({"chat_id": chat_id, "user_id": bot_id})
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{bot_token}/getChatMember?{params}",
            timeout=5,
        ) as r:
            member = _json.loads(r.read().decode())
        if not member.get("ok"):
            _print_check(
                False,
                "bot can_pin_messages",
                f"getChatMember failed: {member.get('description', 'unknown')}",
                "check the bot is a member of TELE_CLAUDE_SUPERGROUP_ID.",
            )
            return False
        result = member.get("result", {})
        can_pin = bool(result.get("can_pin_messages"))
        if can_pin:
            _print_check(
                True, "bot can_pin_messages", "bot has can_pin_messages in supergroup"
            )
            return True
        _print_check(
            False,
            "bot can_pin_messages",
            "bot lacks can_pin_messages in supergroup",
            "promote the bot to admin with the Pin Messages permission, "
            "or disable pinning via /pinned off.",
        )
        return False
    except (OSError, ValueError) as e:
        _print_check(
            False,
            "bot can_pin_messages",
            f"could not check pin perm: {e}",
            "verify network access to api.telegram.org.",
        )
        return False


# ---------- Entry point ----------


def main() -> int:
    """Run all checks, return 0 on full pass else 1."""
    print("Running tele-claude doctor checks...\n")
    checks = (
        check_claude_on_path,
        check_claude_alias,
        check_tele_claude_propagates,
        check_hook_scripts,
        check_credentials_file,
        check_tmux_installed,
        check_python_telegram_bot,
        check_pin_perm,
    )
    results = [check() for check in checks]
    passed = sum(results)
    total = len(results)
    print()
    if passed == total:
        print(f"All {total} checks passed.")
        return 0
    print(f"{passed}/{total} checks passed. Fix the {_BAD} items above and re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

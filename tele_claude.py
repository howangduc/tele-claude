import os
import re
import subprocess
import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["CLAUDE_TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["CLAUDE_TELEGRAM_CHAT_ID"])


def list_claude_panes() -> list[tuple[str, str]]:
    """List tmux panes running Claude Code. Returns [(pane_id, path), ...]."""
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_path}",
         "-f", "#{m:*claude*,#{pane_current_command}}"],
        capture_output=True, text=True,
    )
    panes: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines():
        if line:
            parts = line.split(" ", 1)
            panes.append((parts[0], parts[1]))
    return panes


def send_to_tmux(pane_id: str, text: str) -> None:
    """Send text to a tmux pane via send-keys with literal flag."""
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", text], check=True)
    _ = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], check=True)


def extract_pane_id(text: str) -> str | None:
    """Extract tmux pane ID from a message.

    Searches for %N pattern anywhere in the text.
    """
    match = re.search(r"(?<!\w)%\d+(?!\w)", text)
    return match.group(0) if match else None


async def panes(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    if message.chat_id != CHAT_ID:
        return

    claude_panes = list_claude_panes()
    if not claude_panes:
        _ = await message.reply_text("No Claude Code panes found.")
        return

    for pane_id, path in claude_panes:
        short_path = path.replace(os.path.expanduser("~"), "~")
        _ = await message.reply_text(f"{pane_id} {short_path}")


async def handle_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    if message.chat_id != CHAT_ID:
        return

    reply_to = message.reply_to_message
    if not reply_to or not reply_to.text:
        _ = await message.reply_text("Reply to a message containing a tmux pane ID (e.g. %0).")
        return

    pane_id = extract_pane_id(reply_to.text)
    if not pane_id:
        _ = await message.reply_text("Could not find a tmux pane ID (e.g. %0) in the replied message.")
        return

    user_text = message.text
    if not user_text:
        return

    logger.info("Sending to tmux pane %s: %s", pane_id, user_text)

    try:
        send_to_tmux(pane_id, user_text)
        _ = await message.reply_text(f"Sent to pane {pane_id}", reply_to_message_id=message.message_id)
    except subprocess.CalledProcessError as e:
        _ = await message.reply_text(f"Failed to send to pane {pane_id}: {e}", reply_to_message_id=message.message_id)


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("panes", panes))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

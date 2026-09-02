"""Telegram trading journal bot — entry point and handler wiring."""

import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
import journal

BOT_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def load_env() -> None:
    """Load key=value pairs from a .env file next to this script (if any)."""
    env_file = BOT_DIR / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def get_token() -> str:
    """Return the bot token from the TELEGRAM_BOT_TOKEN env var or .env file."""
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Bot token not found.\n"
            "Set the TELEGRAM_BOT_TOKEN environment variable or create a .env "
            "file with TELEGRAM_BOT_TOKEN=your-token (get a token from "
            "@BotFather on Telegram)."
        )
    return token


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the command overview when the /start command is issued."""
    await update.message.reply_text(
        "Trading Journal\n\n"
        "/trade — log a closed trade\n"
        "/recent — last 10 trades\n"
        "/stats — overall performance\n"
        "/delete <id> — delete a trade, e.g. /delete 12\n\n"
        "Inside /trade you'll get buttons for choices (long/short, auto P&L, "
        "today's date, skip notes, save/discard); type values where asked.\n"
        "Send /cancel to abort."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception raised while handling an update."""
    logger.error("Error while handling an update", exc_info=context.error)


def main() -> None:
    db.init_db()
    app = Application.builder().token(get_token()).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(journal.build_conversation())
    app.add_handler(CommandHandler("recent", journal.recent))
    app.add_handler(CommandHandler("stats", journal.stats))
    app.add_handler(CommandHandler("delete", journal.delete_cmd))
    app.add_error_handler(on_error)
    logger.info("Bot is running — press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()

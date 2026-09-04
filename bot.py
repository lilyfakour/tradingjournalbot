"""Telegram trading journal bot — entry point and handler wiring."""

import logging
import os
from pathlib import Path

from telegram.ext import Application, CommandHandler, ContextTypes

import db
import journal

BOT_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        # Console output disappears with the window — everything (including
        # tracebacks) must also land in bot.log so bugs stay diagnosable.
        logging.FileHandler(BOT_DIR / "bot.log", encoding="utf-8"),
    ],
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
            "توکن ربات پیدا نشد.\n"
            "متغیر محیطی TELEGRAM_BOT_TOKEN را تنظیم کنید یا فایل .env بسازید "
            "و داخل آن TELEGRAM_BOT_TOKEN=توکن‌شما را بگذارید "
            "(توکن را از @BotFather در تلگرام بگیرید)."
        )
    return token


async def post_init(app: Application) -> None:
    """Remove the legacy ☰ command list / menu button.

    The inline UI needs no slash-command menu — everything is a button on the
    bot's messages. The old registration would otherwise keep living in
    Telegram clients, so it is cleared on every start.
    """
    await app.bot.set_my_commands([])
    await app.bot.set_chat_menu_button()
    logger.info("Legacy command menu cleared (everything is inline now).")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception raised while handling an update."""
    logger.error("Error while handling an update", exc_info=context.error)


def _register_handlers(app: Application) -> None:
    """Register every command, conversation and callback handler."""
    app.add_handler(CommandHandler("start", journal.show_menu))
    app.add_handler(CommandHandler("settings", journal.show_settings))
    app.add_handler(journal.build_conversation())
    app.add_handler(journal.build_open_conversation())
    app.add_handler(journal.build_close_conversation())
    app.add_handler(journal.build_stats_callbacks())
    app.add_handler(journal.build_recent_callbacks())
    app.add_handler(journal.build_open_callbacks())
    app.add_handler(journal.build_menu_callbacks())
    for handler in journal.build_settings_handlers():
        app.add_handler(handler)
    app.add_handler(CommandHandler("recent", journal.recent))
    app.add_handler(CommandHandler("opens", journal.open_trades))
    app.add_handler(CommandHandler("export", journal.export_trades))
    app.add_handler(CommandHandler("stats", journal.stats))
    app.add_handler(CommandHandler("delete", journal.delete_cmd))
    app.add_error_handler(on_error)


def main() -> None:
    db.init_db()
    app = (
        Application.builder()
        .token(get_token())
        .post_init(post_init)
        .build()
    )
    _register_handlers(app)
    logger.info("Bot is running — press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()

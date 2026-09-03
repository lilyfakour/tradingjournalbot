"""Telegram trading journal bot — entry point and handler wiring."""

import logging
import os
from pathlib import Path

from telegram import BotCommand, MenuButtonCommands, Update
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


# The '/' commands shown when the ☰ menu button next to the message bar is
# tapped (registered with setMyCommands on startup).
BOT_COMMANDS = [
    BotCommand("start", "🏠 منوی اصلی"),
    BotCommand("help", "❓ راهنمای استفاده از ربات"),
    BotCommand("trade", "📈 ثبت معامله بسته‌شده"),
    BotCommand("recent", "🕘 معاملات اخیر — هر معامله یک دکمه، جزئیات کامل و حذف"),
    BotCommand("stats", "📊 آمار — فیلتر بازه و نماد (دکمه‌های داخل پیام)"),
    BotCommand("export", "📥 دانلود همه معاملات به‌صورت اکسل"),
    BotCommand("delete", "🗑 حذف یک معامله با شماره"),
    BotCommand("cancel", "✖️ لغو ثبت جاری"),
]


async def post_init(app: Application) -> None:
    """Register the menu-button command list before polling starts."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Menu commands registered (%s commands).", len(BOT_COMMANDS))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception raised while handling an update."""
    logger.error("Error while handling an update", exc_info=context.error)


def main() -> None:
    db.init_db()
    app = (
        Application.builder()
        .token(get_token())
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", journal.show_menu))
    app.add_handler(CommandHandler("help", journal.show_menu))
    app.add_handler(journal.build_conversation())
    for handler in journal.build_menu_handlers():
        app.add_handler(handler)
    app.add_handler(journal.build_stats_callbacks())
    app.add_handler(journal.build_recent_callbacks())
    app.add_handler(CommandHandler("recent", journal.recent))
    app.add_handler(CommandHandler("export", journal.export_trades))
    app.add_handler(CommandHandler("stats", journal.stats))
    app.add_handler(CommandHandler("delete", journal.delete_cmd))
    app.add_error_handler(on_error)
    logger.info("Bot is running — press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()

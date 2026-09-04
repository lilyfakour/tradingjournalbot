"""Guided /trade conversation (reply-keyboard prompts) plus the open-trades
flow, the /recent, /open, /stats and /delete commands.

Choices are offered as reply-keyboard buttons — the buttons that appear under
the message input field at the bottom of the screen. Tapping one sends its
label as a normal text message, so typed answers work everywhere too. A chart
screenshot can be attached near the end of the questionnaire and is later
reachable through the 📷 button on the trade's detail card in /recent.

Open trades work in two phases: the 🟢 open-trades questionnaire (market,
symbol, side, timeframe, reason, screenshot, date, time, risk, entry, TP, SL)
stores a running position in db.open_trades; when it closes, the trader taps
it in the 🟢 panel and fills a second short questionnaire (status, exit date,
time, price, up to 4 exit screenshots, reason, mood) which moves it into the
normal closed-trades history.
"""

from __future__ import annotations

import html
import logging
import math
import os
import re
import traceback
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
import export

logger = logging.getLogger(__name__)

# Conversation states, in the order the questions are asked.
(
    MARKET,
    SYMBOL,
    DIRECTION,
    LEVERAGE,
    TIMEFRAME,
    ENTRY,
    TAKE_PROFIT,
    STOP_LOSS,
    RESULT,
    MARGIN,
    RISK,
    TRADE_DATE,
    TRADE_HOUR,
    NOTES,
    MOOD,
    SCREENSHOT,
    SCREENSHOT_AFTER,
    CONFIRM,
) = range(18)

# States of the 🟢 open-trades questionnaire (starting at 100 to keep the two
# conversations' state numbers disjoint).
(
    OPEN_MARKET,
    OPEN_SYMBOL,
    OPEN_DIRECTION,
    OPEN_TIMEFRAME,
    OPEN_REASON,
    OPEN_SCREENSHOT,
    OPEN_TRADE_DATE,
    OPEN_TRADE_HOUR,
    OPEN_RISK,
    OPEN_ENTRY,
    OPEN_TAKE_PROFIT,
    OPEN_STOP_LOSS,
    OPEN_CONFIRM,
) = range(100, 113)

# States of the close-an-open-trade questionnaire (started from the 🏁 button
# on an open trade's detail card — the open trade id travels in user_data).
(
    CLOSE_STATUS,
    CLOSE_DATE,
    CLOSE_HOUR,
    CLOSE_PRICE,
    CLOSE_PHOTOS,
    CLOSE_REASON,
    CLOSE_MOOD,
    CLOSE_CONFIRM,
) = range(200, 208)

_TEXT = filters.TEXT & ~filters.COMMAND
_CANCEL_RE = re.compile(
    r"^\s*(?:/cancel|cancel|لغو|انصراف|✖️\s*(?:cancel|لغو|انصراف))\s*$",
    re.IGNORECASE,
)
# Main-menu button labels. The emoji stays required so plain words can still
# be typed as answers (notes, symbols, ...); case-insensitive, and both the
# Persian and the original English words route.
_NEW_TRADE_RE = re.compile(r"^\s*📈\s*(?:new\s*trade|معامله\s*جدید)\s*$", re.IGNORECASE)
_STATS_RE = re.compile(r"^\s*📊\s*(?:stats|آمار)\s*$", re.IGNORECASE)
_RECENT_RE = re.compile(r"^\s*🕘\s*(?:recent|معاملات\s*اخیر|اخیر)\s*$", re.IGNORECASE)
_OPEN_RE = re.compile(r"^\s*🟢\s*(?:open\s*trades|معاملات\s*باز|باز)\s*$", re.IGNORECASE)
_EXPORT_RE = re.compile(r"^\s*📥\s*(?:export|اکسل)\s*$", re.IGNORECASE)
_MENU_HOME_RE = re.compile(r"^\s*🏠\s*(?:menu|منو)\s*$", re.IGNORECASE)
_MENU_HELP_RE = re.compile(r"^\s*❓\s*(?:help|راهنما)\s*$", re.IGNORECASE)
_MENU_RE = re.compile(
    r"^\s*(?:📈\s*(?:new\s*trade|معامله\s*جدید)|📊\s*(?:stats|آمار)"
    r"|🕘\s*(?:recent|معاملات\s*اخیر|اخیر)|🟢\s*(?:open\s*trades|معاملات\s*باز|باز)"
    r"|📥\s*(?:export|اکسل)"
    r"|🏠\s*(?:menu|منو)|❓\s*(?:help|راهنما))\s*$",
    re.IGNORECASE,
)
_ANSWER = _TEXT & ~filters.Regex(_CANCEL_RE) & ~filters.Regex(_MENU_RE)

_LONG_ALIASES = {"long", "l", "buy", "b", "📈 long", "خرید", "📈 خرید"}
_SHORT_ALIASES = {"short", "s", "sell", "📉 short", "فروش", "📉 فروش"}
# Includes the exact ⏭ button labels so tapping «⏭ رد کردن» really skips
# (bugfix: the emoji-prefixed label never matched the bare word before).
_SKIP_TOKENS = {"", "-", "skip", "رد کردن", "⏭ رد کردن", "⏭ skip"}
_TODAY_TOKENS = {"today", "📅 today", "امروز", "📅 امروز"}
_SKIP_NOTES_TOKENS = _SKIP_TOKENS | {"بدون دلیل", "⏭ بدون دلیل"}
_SKIP_SHOT_TOKENS = _SKIP_TOKENS | {
    "skip screenshot",
    "بدون اسکرین‌شات",
    "⏭ بدون اسکرین‌شات",
}
_NOW_TOKENS = {"now", "الان", "🕐 الان"}
_SKIP_HOUR_TOKENS = _SKIP_TOKENS
_SKIP_MOOD_TOKENS = _SKIP_TOKENS
_SKIP_LEV_TOKENS = _SKIP_TOKENS | {"بدون اهرم", "⏭ بدون اهرم"}
_SKIP_RISK_TOKENS = _SKIP_TOKENS | {"بدون درصد", "⏭ بدون درصد"}
_MARKET_CRYPTO_TOKENS = {
    "crypto",
    "کریپتو",
    "🪙 کریپتو",
    "🪙 crypto",
    "c",
}
_MARKET_FOREX_TOKENS = {
    "forex",
    "فارکس",
    "💵 فارکس",
    "💵 forex",
    "f",
}
_RESULT_WIN_TOKENS = {"win", "w", "✅ win", "برد", "✅ برد"}
_RESULT_LOSE_TOKENS = {
    "lose",
    "loss",
    "l",
    "❌ loss",
    "❌ lose",
    "باخت",
    "❌ باخت",
}
_RESULT_BE_TOKENS = {
    "be",
    "breakeven",
    "➖ be",
    "➖ breakeven",
    "سربه‌سر",
    "سربه سر",
    "➖ سربه‌سر",
}
_HOUR_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{1,2}))?\s*$")

# Manual exit tokens for the close flow's status question.
_MANUAL_TOKENS = {"manual", "manual exit", "exit", "دستی", "خروج دستی", "e", "m"}
_STATUS_TOKENS = {
    "win": "win",
    "w": "win",
    "✅ win": "win",
    "tp hit": "win",
    "tp": "win",
    "برد": "win",
    "✅ برد": "win",
    "loss": "loss",
    "lose": "loss",
    "l": "loss",
    "❌ loss": "loss",
    "sl hit": "loss",
    "sl": "loss",
    "باخت": "loss",
    "❌ باخت": "loss",
    "be": "be",
    "breakeven": "be",
    "➖ be": "be",
    "➖ be (breakeven)": "be",
    "سربه‌سر": "be",
    "سربه سر": "be",
    "➖ سربه‌سر": "be",
} | {token: "manual" for token in _MANUAL_TOKENS}
_STATUS_TOKENS.update(
    {
        # Exact reply-keyboard button labels of the status question.
        "✅ win (tp)": "win",
        "❌ loss (sl)": "loss",
        "✏️ manual": "manual",
    }
)
# Stored value -> detail-card emoji/label (manual uses ➖ in stats roll-ups).
_OPEN_EMOJI = {
    "win": "🟢",
    "loss": "🔴",
    "be": "⚪",
    "manual": "✏️",
}
_OPEN_STATUS_LABELS = {"win": "TP hit (Win)", "loss": "SL hit (Loss)", "be": "Breakeven", "manual": "Manual exit"}
# Maximum number of exit screenshots per close (spec: 4).
_MAX_EXIT_PHOTOS = 4

# Chart screenshots are stored here; override with the SCREENSHOT_DIR env var.
SCREENSHOT_DIR = Path(
    os.getenv(
        "SCREENSHOT_DIR", str(Path(__file__).resolve().parent / "screenshots")
    )
)


# --------------------------------------------------------------------------
# Reply keyboards — the buttons shown under the message input field
# --------------------------------------------------------------------------

def _rk(rows: list[list[str]], one_time: bool = True) -> ReplyKeyboardMarkup:
    """Build a reply keyboard (one-time keyboards hide after a tap)."""
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=one_time,
        is_persistent=not one_time,
    )


_CANCEL_ROW = ["✖️ لغو"]
_MARKET_KEYBOARD = _rk([["🪙 کریپتو", "💵 فارکس"], _CANCEL_ROW])
_DIR_KEYBOARD = _rk([["📈 Long", "📉 Short"], _CANCEL_ROW])
_TF_BUTTONS = [["1m", "5m", "15m"], ["30m", "1h", "4h"], ["1D", "1W", "1M"]]
_TF_KEYBOARD = _rk([*_TF_BUTTONS, _CANCEL_ROW])
_LEV_KEYBOARD = _rk(
    [
        ["×2", "×3", "×5"],
        ["×10", "×20", "×50"],
        ["×100", "×125", "⏭ بدون اهرم"],
        _CANCEL_ROW,
    ]
)
_RESULT_KEYBOARD = _rk(
    [["✅ Win", "❌ Loss", "➖ BE"], _CANCEL_ROW]
)
_RISK_KEYBOARD = _rk(
    [
        ["0.5%", "1%", "2%"],
        ["3%", "5%", "10%"],
        ["⏭ بدون درصد"],
        _CANCEL_ROW,
    ]
)
_DATE_KEYBOARD = _rk([["📅 امروز"], _CANCEL_ROW])
_STATUS_KEYBOARD = _rk(
    [["✅ Win (TP)", "❌ Loss (SL)"], ["➖ BE", "✏️ Manual"], _CANCEL_ROW]
)
_OPEN_CONFIRM_KEYBOARD = _rk([["✅ ثبت", "❌ ثبت نشود"], _CANCEL_ROW])
_HOUR_KEYBOARD = _rk(
    [["00", "03", "06", "09"], ["12", "15", "18", "21"], ["⏭ رد کردن"], _CANCEL_ROW]
)

# Predetermined moods: button label -> value stored in the database.
# Professional plain-word labels (no emojis), Latin technical terms kept.
_MOODS = {
    "آرام": "calm",
    "مطمئن": "confident",
    "مضطرب": "anxious",
    "طمع": "greedy",
    "FOMO": "fomo",
    "انتقامی": "revenge",
}
_MOOD_LABELS = {value: label for label, value in _MOODS.items()}
# English display word for a stored direction value (no DB change).
_DIR_LABEL = {"long": "Long", "short": "Short"}
# Result values stored in the `hit` column, with legacy values mapped too.
_RESULT_LABELS = {
    "win": "Win",
    "lose": "Loss",
    "be": "BE",
    "tp": "TP",
    "sl": "SL",
}
# Accept the button label, the English word, or the stored value itself.
_MOOD_ALIASES = {
    alias: value
    for label, value in _MOODS.items()
    for alias in (label.lower(), value)
}
_MOOD_ALIASES["فومو"] = "fomo"  # Persian spelling of FOMO
_MOOD_KEYBOARD = _rk(
    [
        list(_MOODS)[i : i + 2]
        for i in range(0, len(_MOODS), 2)
    ]
    + [["⏭ رد کردن"], _CANCEL_ROW]
)
_NOTES_KEYBOARD = _rk([["⏭ بدون دلیل"], _CANCEL_ROW])
_SHOT_KEYBOARD = _rk([["⏭ بدون اسکرین‌شات"], _CANCEL_ROW])
_SHOT2_KEYBOARD = _rk([["⏭ بدون اسکرین‌شات"], _CANCEL_ROW])
_CONFIRM_KEYBOARD = _rk([["✅ ذخیره", "❌ ثبت نشود"], _CANCEL_ROW])
# Sent during plain-typing steps so the screen stays clean; the menu bar
# comes back only when the flow ends (save/discard/cancel).
_KEYBOARD_GONE = ReplyKeyboardRemove()


def _symbol_keyboard() -> Optional[ReplyKeyboardMarkup]:
    """Reply keyboard offering the most used and recently traded symbols."""
    recent, top = db.get_symbol_suggestions()
    rows: list[list[str]] = []
    shown: set[str] = set()
    if top:
        rows.append(top)
        shown.update(top)
    extra = [symbol for symbol in recent if symbol not in shown]
    if extra:
        rows.append(extra)
    if not rows:
        return None
    rows.append(list(_CANCEL_ROW))
    return _rk(rows)


# --------------------------------------------------------------------------
# Main menu (sent by /start and the 🏠 Menu button)
# --------------------------------------------------------------------------

MENU_TEXT = (
    "📈 /trade — ثبت معامله بسته‌شده\n"
    "🟢 /open — ثبت معامله باز و بستن آن بعداً\n"
    "🕘 /recent — معاملات اخیر، صفحه‌بندی‌شده (۱۰ تای آخر در هر صفحه؛ برای جزئیات روی معامله بزنید)\n"
    "📊 /stats — آمار عملکرد؛ فیلتر بازه زمانی و نماد با دکمه‌های داخل پیام\n"
    "📥 /export — دریافت همه معاملات به‌صورت فایل اکسل\n"
    "🗑 /delete <id> — حذف یک معامله\n"
    "✖️ /cancel — لغو ثبت جاری\n\n"
    "درون /trade هر انتخاب به‌صورت دکمه در پایین صفحه نمایش داده می‌شود؛ "
    "هر جا لازم بود می‌توانید مقدار را تایپ کنید. در پایان هم می‌توانید "
    "اسکرین‌شات قبل و بعد از معامله را ضمیمه کنید. برای شروع یکی از "
    "دکمه‌های زیر را بزنید."
)

# The persistent main-menu bar. Every flow ends by re-sending it so the
# buttons never disappear (one-time question keyboards vanish after a tap).
_MENU_KEYBOARD = _rk(
    [
        ["📈 معامله جدید", "🟢 معاملات باز"],
        ["📊 آمار", "🕘 معاملات اخیر"],
        ["📥 اکسل", "🏠 منو"],
    ],
    one_time=False,
)


async def show_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send the welcome text and the main-menu keyboard (also /start)."""
    user = getattr(update, "effective_user", None)
    name = (getattr(user, "first_name", "") or "").strip()
    hello = f"سلام {name}!\n" if name else "سلام!\n"
    await update.effective_chat.send_message(
        hello + MENU_TEXT, reply_markup=_MENU_KEYBOARD
    )


def build_menu_handlers() -> list[MessageHandler]:
    """Handlers for the menu buttons that run outside the conversation.

    📈 New trade is deliberately NOT here — it is an entry point of the
    conversation itself so that a tap properly registers the SYMBOL state.
    """
    return [
        MessageHandler(filters.Regex(_STATS_RE), stats),
        MessageHandler(filters.Regex(_RECENT_RE), recent),
        # 🟢 معاملات باز opens the open-trades PANEL here (not the
        # questionnaire — adding happens through the panel's ➕ button).
        MessageHandler(filters.Regex(_OPEN_RE), open_trades),
        MessageHandler(filters.Regex(_EXPORT_RE), export_trades),
        MessageHandler(filters.Regex(_MENU_HELP_RE), show_menu),
        MessageHandler(filters.Regex(_MENU_HOME_RE), show_menu),
    ]


async def export_trades(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send every logged trade as an .xlsx spreadsheet (📥 Export, /export)."""
    await update.message.reply_chat_action(ChatAction.UPLOAD_DOCUMENT)
    path = export.build_export_file()
    logger.info("Sending export %s", path.name)
    with path.open("rb") as document:
        await update.message.reply_document(
            document,
            filename=path.name,
            caption=f"📊 {path.stem} — همه معاملات",
            reply_markup=_MENU_KEYBOARD,
        )
    path.unlink(missing_ok=True)  # sent; don't leave copies on disk


# --------------------------------------------------------------------------
# Timeframe parsing
# --------------------------------------------------------------------------

_TF_ALIASES = {
    "s": "1s", "1s": "1s", "sec": "1s", "1sec": "1s",
    "m": "1m", "1m": "1m", "1min": "1m", "min": "1m", "minute": "1m",
    "3m": "3m", "5m": "5m", "5min": "5m", "10m": "10m",
    "15m": "15m", "15min": "15m", "30m": "30m", "30min": "30m", "45m": "45m",
    "h": "1h", "1h": "1h", "1hr": "1h", "60m": "1h",
    "hour": "1h", "hourly": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h",
    "d": "1D", "1d": "1D", "day": "1D", "1day": "1D", "daily": "1D",
    "w": "1W", "1w": "1W", "week": "1W", "1week": "1W", "weekly": "1W",
    "M": "1M", "1M": "1M", "mo": "1M", "1mo": "1M",
    "month": "1M", "1month": "1M", "monthly": "1M",
}
_TF_PATTERN = re.compile(r"^(\d{1,3})([smhdw])$", re.IGNORECASE)


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _fa_num(value) -> str:
    """Render a number with Persian digits (e.g. 3 -> ۳)."""
    return str(value).translate(_FA_DIGITS)


def _parse_timeframe(raw: str) -> Optional[str]:
    """Normalize a timeframe token (e.g. '5min' -> '5m', '1day' -> '1D')."""
    text = raw.strip()
    if not text or len(text) > 8 or any(ch.isspace() for ch in text):
        return None
    if text in _TF_ALIASES:  # case-sensitive first: 'M' means month
        return _TF_ALIASES[text]
    if text.lower() in _TF_ALIASES:
        return _TF_ALIASES[text.lower()]
    match = _TF_PATTERN.match(text)
    if match:
        count, unit = match.group(1), match.group(2).lower()
        return f"{count}{unit if unit in 'mh' else unit.upper()}"
    return None


# --------------------------------------------------------------------------
# Number / text helpers
# --------------------------------------------------------------------------

def _parse_number(raw: str) -> Optional[float]:
    """Parse a float written with a dot decimal separator (no commas)."""
    value = raw.strip().replace("_", "")
    if not value or "," in value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_positive(raw: str) -> Optional[float]:
    number = _parse_number(raw)
    return number if number is not None and number > 0 else None


def _fmt_num(value: float) -> str:
    """Compact price formatting without trailing zeros."""
    return f"{value:.10g}"


def _fmt_size(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _fmt_pnl(value: float) -> str:
    """Signed dollar amount, e.g. +$12.50 or -$3.00."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _fmt_roi(value: Optional[float]) -> str:
    """Signed percent with two decimals, e.g. +12.50%; '-' when unknown."""
    return f"{value:+.2f}%" if value is not None else "-"


def _parse_percent(raw: str) -> Optional[float]:
    """Parse a risk percentage; accepts '2', '2%', '0.5 %' and Persian digits."""
    text = (raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    text = text.rstrip("%٪").strip()
    number = _parse_positive(text)
    if number is None or number > 100:
        return None
    return number


def _pnl_from_hit(data: dict) -> float:
    """P&L in quote currency from margin, leverage and which level hit.

    The position is margin * leverage, so the price move translates into
    (move / entry) * position — i.e. the margin grows/shrinks by the move
    scaled with the leverage. A breakeven result (exit == entry) yields 0.
    """
    if not data.get("exit_price"):
        return 0.0
    margin = data["size"]
    leverage = data.get("leverage") or 1.0
    move = (
        (data["exit_price"] - data["entry_price"])
        if data["direction"] == "long"
        else (data["entry_price"] - data["exit_price"])
    )
    return margin * leverage * (move / data["entry_price"])


def _roi_from_hit(data: dict, pnl: float) -> float:
    """ROI in percent: P&L relative to the committed margin.

    A leveraged position moves the margin by (move / entry) * leverage, so
    ROI is simply pnl / margin * 100 — e.g. 10x leverage on a 1% move is
    +10% ROI. Breakeven trades yield 0%.
    """
    margin = data.get("size") or 0
    return (pnl / margin * 100.0) if margin else 0.0


def _screenshot_path(name: str) -> Path:
    return SCREENSHOT_DIR / name


def _drop_screenshot(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete attached screenshot files (used when a draft is thrown away)."""
    keys = ("screenshot", "screenshot_after", "exit_photos")
    for key in keys:
        names = context.user_data.get(key)
        if not names:
            continue
        for name in names.splitlines():
            try:
                _screenshot_path(name).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove screenshot file %s", name)
        context.user_data.pop(key, None)


def _purge_screenshots(row) -> None:
    """Delete the screenshot files of a trade row (after it was deleted)."""
    for key in ("screenshot", "screenshot_after"):
        if row[key]:
            try:
                _screenshot_path(row[key]).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove screenshot for deleted trade #%s", row["id"]
                )


# --------------------------------------------------------------------------
# Prompt helpers (each prompt carries its own reply keyboard)
# --------------------------------------------------------------------------

async def _prompt_direction(update: Update) -> int:
    await update.effective_chat.send_message(
        "جهت معامله؟", reply_markup=_DIR_KEYBOARD
    )
    return DIRECTION


async def _prompt_leverage(update: Update) -> int:
    await update.effective_chat.send_message(
        "Leverage — دکمه را بزنید یا عدد بفرستید:",
        reply_markup=_LEV_KEYBOARD,
    )
    return LEVERAGE


async def _prompt_timeframe(update: Update) -> int:
    await update.effective_chat.send_message(
        "تایم‌فریم (Timeframe):",
        reply_markup=_TF_KEYBOARD,
    )
    return TIMEFRAME


async def _prompt_entry(update: Update) -> int:
    await update.effective_chat.send_message(
        "Entry price:", reply_markup=_KEYBOARD_GONE
    )
    return ENTRY


async def _prompt_take_profit(update: Update) -> int:
    await update.effective_chat.send_message(
        "🎯 Take Profit (TP):", reply_markup=_KEYBOARD_GONE
    )
    return TAKE_PROFIT


async def _prompt_stop_loss(update: Update) -> int:
    await update.effective_chat.send_message(
        "🛑 Stop Loss (SL):", reply_markup=_KEYBOARD_GONE
    )
    return STOP_LOSS


async def _prompt_result(update: Update) -> int:
    await update.effective_chat.send_message(
        "نتیجه معامله؟",
        reply_markup=_RESULT_KEYBOARD,
    )
    return RESULT


async def _prompt_margin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if (context.user_data.get("market") or "crypto") == "forex":
        detail = "USD (حساب فارکس)"
    else:
        detail = "USDT"
    await update.effective_chat.send_message(
        f"💰 Margin ({detail}):",
        reply_markup=_KEYBOARD_GONE,
    )
    return MARGIN


async def _prompt_risk(update: Update) -> int:
    await update.effective_chat.send_message(
        "⚠️ Risk — چند درصد از حساب؟ (مثلاً 1 یا 1%)",
        reply_markup=_RISK_KEYBOARD,
    )
    return RISK


async def _prompt_trade_date(update: Update) -> int:
    await update.effective_chat.send_message(
        "تاریخ بستن معامله:\n"
        "YYYY-MM-DD  (e.g. 2026-02-09)",
        reply_markup=_DATE_KEYBOARD,
    )
    return TRADE_DATE


async def _prompt_notes(update: Update) -> int:
    await update.effective_chat.send_message(
        "📝 دلیل ورود (دقیقاً چرا وارد شدی؟):",
        reply_markup=_NOTES_KEYBOARD,
    )
    return NOTES


async def _prompt_screenshot(update: Update) -> int:
    await update.effective_chat.send_message(
        "📸 اسکرین‌شات چارت — قبل از ورود (اختیاری):",
        reply_markup=_SHOT_KEYBOARD,
    )
    return SCREENSHOT


async def _prompt_screenshot_after(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.effective_chat.send_message(
        "📸 اسکرین‌شات چارت — بعد از معامله (اختیاری):",
        reply_markup=_SHOT2_KEYBOARD,
    )
    return SCREENSHOT_AFTER


_RESULT_EMOJI = {"win": "🟢", "lose": "🔴", "be": "⚪"}


def _result_emoji(hit: Optional[str]) -> str:
    """🟢 win · 🔴 loss · ⚪ breakeven · ➖ unknown/legacy."""
    return _RESULT_EMOJI.get(hit or "", "➖")


def _summary(data: dict) -> str:
    """Render the airy confirmation summary for the current draft (HTML)."""
    pnl = _pnl_from_hit(data)
    roi = _roi_from_hit(data, pnl)
    hit = data.get("hit") or ""
    emoji = _result_emoji(hit)
    market = data.get("market") or "crypto"
    market_fa = "🪙 کریپتو" if market == "crypto" else "💵 فارکس"
    lev = data.get("leverage")
    result = _RESULT_LABELS.get(hit, "-")
    risk = data.get("risk_percent")
    shots = []
    if data.get("screenshot"):
        shots.append("قبل")
    if data.get("screenshot_after"):
        shots.append("بعد")
    mood = data.get("mood")
    symbol = _ESC(data["symbol"])
    notes = _ESC(data["notes"]) if data["notes"] else ""
    return (
        "🔎 <b>تأیید نهایی</b>\n"
        "————————————————\n"
        "\n"
        "◾ <i>معامله</i>\n"
        f"• Market    {market_fa}\n"
        f"• Symbol    <b>{symbol}</b>\n"
        f"• Side      {_DIR_LABEL.get(data['direction'], data['direction'])}\n"
        f"• TF·Lev    {data.get('timeframe') or '-'}"
        f" · {(_fmt_num(lev) + 'x') if lev else '-'}\n"
        f"• Entry     {_fmt_num(data['entry_price'])}   →   "
        f"{_fmt_num(data['exit_price']) if data['exit_price'] else '-'}\n"
        f"• TP / SL   {_fmt_num(data['take_profit']) if data.get('take_profit') else '-'}"
        f" / {_fmt_num(data['stop_loss']) if data.get('stop_loss') else '-'}\n"
        "\n"
        "◾ <i>نتیجه</i>\n"
        f"• Result    {emoji} {result}\n"
        f"• Margin    {_fmt_size(data['size'])}"
        + (f" · Risk {_fmt_num(risk)}%" if risk else "")
        + "\n"
        f"• Date      {data['trade_date']}\n"
        + (f"• Mood      {_MOOD_LABELS.get(mood, mood)}\n" if mood else "")
        + (f"• Reason    {notes}\n" if data["notes"] else "")
        + (f"• Shots     {' و '.join(shots)}\n" if shots else "")
        + "\n"
        "————————————————\n"
        f"• <i>P&L</i>    <b>{_fmt_pnl(pnl)}</b>\n"
        f"• <i>ROI</i>    <b>{_fmt_roi(roi)}</b>\n"
        "\n"
        "ذخیره شود؟"
    )


async def _prompt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    summary = _summary(context.user_data)
    try:
        await update.effective_chat.send_message(
            summary,
            reply_markup=_CONFIRM_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # Never leave the trader without a confirmation — fall back to the
        # plain text (HTML tags stripped) so the flow can always continue.
        logger.error("HTML confirm summary failed:\n%s", traceback.format_exc())
        await update.effective_chat.send_message(
            re.sub(r"</?[bi]>", "", summary), reply_markup=_CONFIRM_KEYBOARD
        )
    return CONFIRM


async def _save_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Persist the confirmed draft and reply where the user interacted."""
    data = dict(context.user_data)
    context.user_data.clear()
    pnl = _pnl_from_hit(data)
    roi = _roi_from_hit(data, pnl)
    trade_id = db.add_trade(
        symbol=data["symbol"],
        direction=data["direction"],
        timeframe=data.get("timeframe") or "",
        entry_price=data["entry_price"],
        exit_price=data["exit_price"],
        size=data["size"],
        pnl=pnl,
        trade_date=data["trade_date"],
        notes=data["notes"],
        mood=data.get("mood") or "",
        roi=roi,
        screenshot=data.get("screenshot"),
        market=data.get("market") or "crypto",
        leverage=data.get("leverage"),
        risk_percent=data.get("risk_percent"),
        take_profit=data.get("take_profit"),
        stop_loss=data.get("stop_loss"),
        hit=data.get("hit") or "",
        screenshot_after=data.get("screenshot_after"),
    )
    logger.info("Saved trade #%s %s", trade_id, data["symbol"])
    tf = data.get("timeframe") or ""
    lev = data.get("leverage")
    hit = data.get("hit") or ""
    result = _RESULT_LABELS.get(hit, "-")
    symbol = _ESC(data["symbol"])
    text = (
        f"{_result_emoji(hit)} <b>معامله #{trade_id} ذخیره شد</b>\n"
        "\n"
        f"• <b>{symbol}</b> · "
        f"{_DIR_LABEL.get(data['direction'], data['direction'])}"
        + (f" · {tf}" if tf else "")
        + (f" · {_fmt_num(lev)}x" if lev else "")
        + "\n"
        f"• Entry: {_fmt_num(data['entry_price'])}"
        f" → {_fmt_num(data['exit_price'])}\n"
        f"• {result} · Margin {_fmt_size(data['size'])}\n"
        "\n"
        "————————————————\n"
        f"• <i>P&L</i>  <b>{_fmt_pnl(pnl)}</b>\n"
        f"• <i>ROI</i>  <b>{_fmt_roi(roi)}</b>"
        + (
            "  📷"
            if data.get("screenshot") or data.get("screenshot_after")
            else ""
        )
    )
    try:
        await update.message.reply_text(
            text, reply_markup=_MENU_KEYBOARD, parse_mode=ParseMode.HTML
        )
    except Exception:
        # The trade IS saved — never skip the confirmation. Retry as plain
        # text (HTML tags stripped) and log the real cause for debugging.
        logger.error(
            "HTML save confirmation failed:\n%s", traceback.format_exc()
        )
        await update.message.reply_text(
            re.sub(r"</?[bi]>", "", text), reply_markup=_MENU_KEYBOARD
        )
    return ConversationHandler.END


async def _discard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Drop the current draft and reply where the user interacted."""
    _drop_screenshot(context)
    context.user_data.clear()
    await update.message.reply_text(
        "❌ ثبت نشد — چیزی ذخیره نشد.", reply_markup=_MENU_KEYBOARD
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# /trade conversation steps
# --------------------------------------------------------------------------

async def trade_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the guided entry with the market question (crypto vs forex)."""
    _drop_screenshot(context)
    context.user_data.clear()
    await update.message.reply_text(
        "معامله جدید — در کدام بازار معامله کردی؟\n"
        "(برای انصراف /cancel را بفرستید)",
        reply_markup=_MARKET_KEYBOARD,
    )
    return MARKET


async def ask_market(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip().lower()
    if raw in _MARKET_CRYPTO_TOKENS:
        context.user_data["market"] = "crypto"
    elif raw in _MARKET_FOREX_TOKENS:
        context.user_data["market"] = "forex"
    else:
        await update.message.reply_text(
            "یکی از دو دکمه را بزنید: 🪙 کریپتو یا 💵 فارکس",
            reply_markup=_MARKET_KEYBOARD,
        )
        return MARKET
    return await _prompt_symbol(update)


async def _prompt_symbol(update: Update) -> int:
    symbol_kb = _symbol_keyboard()
    if symbol_kb is not None:
        text = (
            "نماد — یکی از نمادهای زیر را بزنید یا نماد دیگری بنویسید؛\n"
            "برای انصراف /cancel را بفرستید."
        )
    else:
        text = (
            "برای انصراف /cancel را بفرستید.\n\n"
            "Symbol:\n"
            "e.g. EURUSD · BTCUSD · XAUUSD"
        )
    await update.message.reply_text(
        text, reply_markup=symbol_kb or _KEYBOARD_GONE
    )
    return SYMBOL


async def ask_symbol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    symbol = (update.message.text or "").strip().upper()
    if not symbol or len(symbol) > 24 or any(ch.isspace() for ch in symbol):
        await update.message.reply_text(
            "این شبیه نماد نیست — یکی از دکمه‌های زیر را بزنید یا دوباره "
            "تایپ کنید (مثلاً EURUSD):",
            reply_markup=_symbol_keyboard() or _KEYBOARD_GONE,
        )
        return SYMBOL
    context.user_data["symbol"] = symbol
    return await _prompt_direction(update)


async def ask_direction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    word = (update.message.text or "").strip().lower()
    if word in _LONG_ALIASES:
        direction = "long"
    elif word in _SHORT_ALIASES:
        direction = "short"
    else:
        await update.message.reply_text(
            "لطفاً یکی از دکمه‌ها را بزنید یا Long / Short بنویسید:",
            reply_markup=_DIR_KEYBOARD,
        )
        return DIRECTION
    context.user_data["direction"] = direction
    return await _prompt_leverage(update)


async def ask_leverage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _SKIP_LEV_TOKENS:
        context.user_data.pop("leverage", None)
        return await _prompt_timeframe(update)
    text = raw.lower().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    text = text.replace("×", "x").replace(" ", "")
    if text.startswith("x"):
        text = text[1:]
    if text.endswith("x"):
        text = text[:-1]
    number = _parse_positive(text)
    if number is None or number > 1000:
        await update.message.reply_text(
            "Leverage نامعتبر — عدد بفرستید (مثلاً 10 یا 10x) یا «⏭ بدون اهرم»:"
        )
        return LEVERAGE
    context.user_data["leverage"] = number
    return await _prompt_timeframe(update)


async def ask_timeframe(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    timeframe = _parse_timeframe(update.message.text or "")
    if timeframe is None:
        await update.message.reply_text(
            "تایم‌فریم نامعتبر.\n"
            "1m · 5m · 15m · 30m · 1h · 4h · 1D · 1W · 1M"
        )
        return TIMEFRAME
    context.user_data["timeframe"] = timeframe
    return await _prompt_entry(update)


async def ask_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "یک عدد مثبت وارد کنید (اعشار با نقطه، مثل 10.5):"
        )
        return ENTRY
    context.user_data["entry_price"] = number
    return await _prompt_take_profit(update)


async def ask_take_profit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "حد سود را به‌صورت یک عدد مثبت وارد کنید (اعشار با نقطه):"
        )
        return TAKE_PROFIT
    context.user_data["take_profit"] = number
    return await _prompt_stop_loss(update)


async def ask_stop_loss(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "حد ضرر را به‌صورت یک عدد مثبت وارد کنید (اعشار با نقطه):"
        )
        return STOP_LOSS
    context.user_data["stop_loss"] = number
    return await _prompt_result(update)


async def ask_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    low = raw.lower()
    if low in _RESULT_WIN_TOKENS:
        context.user_data["hit"] = "win"
        # A win means TP was hit; the exit price is the TP level.
        context.user_data["exit_price"] = context.user_data.get("take_profit")
    elif low in _RESULT_LOSE_TOKENS:
        context.user_data["hit"] = "lose"
        # A loss means SL was hit; the exit price is the SL level.
        context.user_data["exit_price"] = context.user_data.get("stop_loss")
    elif low in _RESULT_BE_TOKENS:
        context.user_data["hit"] = "be"
        context.user_data["exit_price"] = context.user_data["entry_price"]
    else:
        await update.message.reply_text(
            "نتیجه را انتخاب کنید: ✅ Win / ❌ Loss / ➖ BE"
        )
        return RESULT
    return await _prompt_margin(update, context)


async def ask_margin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "Margin نامعتبر — یک عدد مثبت بفرستید (اعشار با نقطه):"
        )
        return MARGIN
    context.user_data["size"] = number  # 'size' column stores the margin
    return await _prompt_risk(update)


async def ask_risk(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _SKIP_RISK_TOKENS:
        context.user_data.pop("risk_percent", None)
        return await _prompt_trade_date(update)
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد ریسک نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2 یا 2%):"
        )
        return RISK
    context.user_data["risk_percent"] = number
    # P&L is auto-calculated from margin, leverage and the exit price —
    # the trader is never asked for it.
    return await _prompt_trade_date(update)


async def ask_trade_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS or raw.lower() in _TODAY_TOKENS:
        context.user_data["trade_date"] = date.today().isoformat()
        return await _prompt_trade_hour(update)
    parsed = None
    with_time = False
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            with_time = fmt.endswith("%H:%M")
            break
        except ValueError:
            continue
    if parsed is None:
        await update.message.reply_text(
            "تاریخ نامعتبر.\n"
            "YYYY-MM-DD یا YYYY-MM-DD HH:MM  (e.g. 2026-02-09 14:30)\n"
            "یا دکمه «📅 امروز» را بزنید."
        )
        return TRADE_DATE
    if with_time:
        context.user_data["trade_date"] = parsed.strftime("%Y-%m-%d %H:%M")
        return await _prompt_notes(update)
    context.user_data["trade_date"] = parsed.date().isoformat()
    return await _prompt_trade_hour(update)


async def _prompt_trade_hour(update: Update) -> int:
    await update.effective_chat.send_message(
        "ساعت بسته‌شدن (اختیاری):\n"
        "HH:MM  (e.g. 14:30)",
        reply_markup=_HOUR_KEYBOARD,
    )
    return TRADE_HOUR


async def ask_trade_hour(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _NOW_TOKENS:
        context.user_data["trade_date"] += (
            f" {datetime.now().strftime('%H:%M')}"
        )
        return await _prompt_notes(update)
    if raw.lower() in _SKIP_HOUR_TOKENS:
        return await _prompt_notes(update)
    match = _HOUR_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        await update.message.reply_text(
            "ساعت نامعتبر.\nHH یا HH:MM  (e.g. 14:30)"
        )
        return TRADE_HOUR
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    context.user_data["trade_date"] += f" {hour:02d}:{minute:02d}"
    return await _prompt_notes(update)


async def ask_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["notes"] = "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    return await _prompt_mood(update)


async def _prompt_mood(update: Update) -> int:
    await update.effective_chat.send_message(
        "Mood — حال‌وهوای حین معامله (اختیاری):",
        reply_markup=_MOOD_KEYBOARD,
    )
    return MOOD


async def ask_mood(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    mood = _MOOD_ALIASES.get(raw.lower())
    if raw.lower() in _SKIP_MOOD_TOKENS:
        context.user_data.pop("mood", None)
    elif mood is not None:
        context.user_data["mood"] = mood
    else:
        await update.message.reply_text(
            "یکی از حال‌وهواهای زیر را انتخاب کنید، مثلاً «آرام» یا «فومو» "
            "بنویسید، یا رد کنید:"
        )
        return MOOD
    return await _prompt_screenshot(update)


async def _store_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE, key: str
) -> bool:
    """Download the attached image into SCREENSHOT_DIR and remember its name."""
    message = update.message
    if message.photo:
        tg_file = await message.photo[-1].get_file()
        ext = ".jpg"
    elif message.document is not None and (
        message.document.mime_type or ""
    ).startswith("image/"):
        tg_file = await message.document.get_file()
        ext = Path(message.document.file_name or "chart.png").suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            ext = ".png"
    else:
        return False
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = SCREENSHOT_DIR / f"trade-{stem}-{uuid.uuid4().hex[:6]}{ext}"
    await tg_file.download_to_drive(dest)
    context.user_data[key] = dest.name
    return True


async def ask_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await _store_screenshot(update, context, "screenshot"):
        await update.message.reply_text(
            "تصویر خوانده نشد — دوباره امتحان کنید یا دکمه ⏭ بدون اسکرین‌شات را بزنید."
        )
        return SCREENSHOT
    return await _prompt_screenshot_after(update, context)


async def ask_screenshot_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() not in _SKIP_SHOT_TOKENS:
        await update.message.reply_text(
            "لطفاً یک تصویر بفرستید، دکمه ⏭ بدون اسکرین‌شات را بزنید، یا '-' را بنویسید."
        )
        return SCREENSHOT
    return await _prompt_screenshot_after(update, context)


async def ask_screenshot_after(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await _store_screenshot(update, context, "screenshot_after"):
        await update.message.reply_text(
            "تصویر خوانده نشد — دوباره امتحان کنید یا دکمه ⏭ بدون اسکرین‌شات را بزنید."
        )
        return SCREENSHOT_AFTER
    return await _prompt_confirm(update, context)


async def ask_screenshot_after_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() not in _SKIP_SHOT_TOKENS:
        await update.message.reply_text(
            "لطفاً یک تصویر بفرستید، دکمه ⏭ بدون اسکرین‌شات را بزنید، یا '-' را بنویسید."
        )
        return SCREENSHOT_AFTER
    return await _prompt_confirm(update, context)


async def save_trade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer = (update.message.text or "").strip().lower()
    if answer in ("y", "yes", "✅ save", "بله", "✅ ذخیره", "ذخیره"):
        return await _save_and_reply(update, context)
    if answer in ("n", "no", "❌ discard", "خیر", "❌ ثبت نشود", "ثبت نشود"):
        return await _discard(update, context)
    await update.message.reply_text("لطفاً بله یا خیر را انتخاب کنید:")
    return CONFIRM


async def cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    had_draft = bool(context.user_data)
    _drop_screenshot(context)
    context.user_data.clear()
    text = "ثبت لغو شد." if had_draft else "چیزی برای لغو نبود."
    if update.message is not None:
        await update.message.reply_text(text, reply_markup=_MENU_KEYBOARD)
    else:
        await update.effective_chat.send_message(
            text, reply_markup=_MENU_KEYBOARD
        )
    return ConversationHandler.END


def build_conversation() -> ConversationHandler:
    """Build the guided /trade conversation handler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("trade", trade_start),
            # The 📈 New trade menu button must start a REAL conversation
            # (registering the SYMBOL state), otherwise the symbol keyboard
            # taps would be dropped — a plain message would never do that.
            MessageHandler(filters.Regex(_NEW_TRADE_RE), trade_start),
        ],
        states={
            MARKET: [MessageHandler(_ANSWER, ask_market)],
            SYMBOL: [MessageHandler(_ANSWER, ask_symbol)],
            DIRECTION: [MessageHandler(_ANSWER, ask_direction)],
            LEVERAGE: [MessageHandler(_ANSWER, ask_leverage)],
            TIMEFRAME: [MessageHandler(_ANSWER, ask_timeframe)],
            ENTRY: [MessageHandler(_ANSWER, ask_entry)],
            TAKE_PROFIT: [MessageHandler(_ANSWER, ask_take_profit)],
            STOP_LOSS: [MessageHandler(_ANSWER, ask_stop_loss)],
            RESULT: [MessageHandler(_ANSWER, ask_result)],
            MARGIN: [MessageHandler(_ANSWER, ask_margin)],
            RISK: [MessageHandler(_ANSWER, ask_risk)],
            TRADE_DATE: [MessageHandler(_ANSWER, ask_trade_date)],
            TRADE_HOUR: [MessageHandler(_ANSWER, ask_trade_hour)],
            NOTES: [MessageHandler(_ANSWER, ask_notes)],
            MOOD: [MessageHandler(_ANSWER, ask_mood)],
            SCREENSHOT: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, ask_screenshot
                ),
                MessageHandler(_ANSWER, ask_screenshot_text),
            ],
            SCREENSHOT_AFTER: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, ask_screenshot_after
                ),
                MessageHandler(_ANSWER, ask_screenshot_after_text),
            ],
            CONFIRM: [MessageHandler(_ANSWER, save_trade)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(_CANCEL_RE), cancel),
        ],
        allow_reentry=True,
    )


# --------------------------------------------------------------------------
# /stats panel — the report text carries its own INLINE keyboard, so the
# filter buttons are attached to the message (not to the bot's reply bar)
# and the panel updates in place. Filters: period, symbol, reset + export.
# --------------------------------------------------------------------------

# Period token -> (button label, description, look-back days)
_STATS_PERIODS = {
    "1w": ("1W", "هفت روز گذشته", 7),
    "1m": ("1M", "سی روز گذشته", 30),
    "3m": ("3M", "سه ماه گذشته", 90),
    "6m": ("6M", "شش ماه گذشته", 180),
    "1y": ("1Y", "یک سال گذشته", 365),
    "all": ("All", "همه زمان‌ها", None),
}
_STATS_PERIOD_ORDER = ["1w", "1m", "3m", "6m", "1y", "all"]

# Button labels (English — the trader knows the terms).
_STATS_SYMBOLS = "🔤 Symbols"
_STATS_RESET = "♻️ Reset"
_STATS_EXPORT = "📤 Export"
_STATS_CLOSE = "✖️ Close"
_STATS_ALL_SYMBOLS = "همه نمادها"

# Callback-data namespace (never collides with plain text).
_CB = "stat"
_CB_PERIOD = f"{_CB}:p:"
_CB_SYMBOL = f"{_CB}:sym:"
_CB_SYMS_PAGE = f"{_CB}:syms:"
_CB_OPEN = f"{_CB}:open"
_CB_SALL = f"{_CB}:sall"
_CB_RESET = f"{_CB}:reset"
_CB_EXPORT = f"{_CB}:export"
_CB_CLOSE = f"{_CB}:close"
_CB_NOOP = f"{_CB}:noop"
_STATS_CB_RE = re.compile(
    r"^" + re.escape(_CB) + r":(?:p:(?P<period>\w+)"
    r"|sym:(?P<sym>[A-Z0-9.\-]{1,24})"
    r"|syms:(?P<page>\d+)"
    r"|open|sall|reset|export|close|noop)$"
)

# Symbols per page in the symbol picker (10 per page per spec).
_SYMBOLS_PER_PAGE = 10

# Escape user-influenced text (symbols, moods) for the HTML parse mode.
_ESC = html.escape


def _stats_since(period_token: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD cutoff for a period token (None = all time)."""
    days = _STATS_PERIODS.get(period_token or "all", (None, None, None))[2]
    if days is None:
        return None
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _render_stats(symbol: Optional[str], period_token: Optional[str]) -> str:
    """Build the stats panel text (HTML) for the given filters.

    Technical keys stay in English (Win rate, P&L, Profit Factor, ...) so
    the RTL/LTR mixing never scrambles the numbers; Persian carries the
    headings and descriptions.
    """
    _, period_desc, _ = _STATS_PERIODS.get(
        period_token or "all", _STATS_PERIODS["all"]
    )
    since = _stats_since(period_token)
    s = db.get_stats(symbol=symbol, since=since)
    title = "📊 <b>آمار معاملات</b>"
    sub = (
        f"<i>نماد: <b>{_ESC(symbol) if symbol else 'همه نمادها'}</b>"
        f" · بازه: {period_desc}</i>"
    )
    if not s.get("trades"):
        return f"{title}\n{sub}\n\nمعامله‌ای در این بازه پیدا نشد."
    wins, losses, be = s["wins"] or 0, s["losses"] or 0, s["be"] or 0
    decided = wins + losses
    win_rate = f"{wins / decided * 100:.1f}%" if decided else "-"
    pf = (
        f"{s['gross_win'] / -s['gross_loss']:.2f}" if s["gross_loss"] else "-"
    )
    avg_win = _fmt_pnl(s["avg_win"]) if s["avg_win"] is not None else "-"
    avg_loss = _fmt_pnl(s["avg_loss"]) if s["avg_loss"] is not None else "-"
    lines = [
        title,
        sub,
        "",
        "◾ <b>خلاصه</b>",
        f"• Trades: <b>{s['trades']}</b> · Win: <b>{wins}</b>"
        f" · Loss: <b>{losses}</b> · BE: <b>{be}</b>",
        f"• Win rate: <b>{win_rate}</b>",
        f"• P&L: <b>{_fmt_pnl(s['total'])}</b>",
        f"• Avg ROI: <b>{_fmt_roi(s.get('avg_roi'))}</b>",
        f"• Avg Win: {avg_win} · Avg Loss: {avg_loss}",
        f"• Profit Factor: <b>{pf}</b>",
        f"• Best: {_fmt_pnl(s['best'])} · Worst: {_fmt_pnl(s['worst'])}",
    ]
    moods = db.get_mood_breakdown(symbol=symbol, since=since)
    if moods:
        lines += ["", "◾ <b>حالت ذهنی</b>"]
        for row in moods:
            mood_wr = (row["wins"] or 0) / row["trades"] * 100
            lines.append(
                f"• {_ESC(_MOOD_LABELS.get(row['mood'], row['mood']))} — "
                f"{row['trades']} معامله · {_fmt_pnl(row['total'])} · "
                f"{mood_wr:.0f}%"
            )
    lines += [
        "",
        "<i>⬇️ دکمه‌های زیر فقط «فیلتر» هستند: بازه زمانی و نماد را عوض "
        "می‌کنند — نه تایم‌فریم، نه معامله.</i>",
    ]
    return "\n".join(lines)


def _stats_panel_kb(flt: dict) -> InlineKeyboardMarkup:
    """Inline filter keyboard attached to the stats panel message itself."""
    period = flt.get("period") or "all"

    def _pbtn(token: str) -> InlineKeyboardButton:
        label = _STATS_PERIODS[token][0]
        return InlineKeyboardButton(
            (f"✓ {label}") if period == token else label,
            callback_data=_CB_PERIOD + token,
        )

    symbol = flt.get("symbol")
    return InlineKeyboardMarkup(
        [
            [_pbtn("1w"), _pbtn("1m"), _pbtn("3m")],
            [_pbtn("6m"), _pbtn("1y"), _pbtn("all")],
            [
                InlineKeyboardButton(
                    f"{_STATS_SYMBOLS}: {symbol or 'همه'}",
                    callback_data=_CB_OPEN,
                )
            ],
            [
                InlineKeyboardButton(_STATS_RESET, callback_data=_CB_RESET),
                InlineKeyboardButton(_STATS_EXPORT, callback_data=_CB_EXPORT),
            ],
        ]
    )


def _symbol_picker_kb(
    page: int, pages: int, symbols: list[tuple[str, int]]
) -> InlineKeyboardMarkup:
    """Symbol filter picker: 10 symbols per page (last trade first)."""
    chunk = symbols[(page - 1) * _SYMBOLS_PER_PAGE : page * _SYMBOLS_PER_PAGE]
    rows = [
        [
            InlineKeyboardButton(f"{sym} ({uses})", callback_data=_CB_SYMBOL + sym)
            for sym, uses in chunk[i : i + 2]
        ]
        for i in range(0, len(chunk), 2)
    ]
    prev_cb = _CB_SYMS_PAGE + str(page - 1) if page > 1 else _CB_NOOP
    next_cb = _CB_SYMS_PAGE + str(page + 1) if page < pages else _CB_NOOP
    rows.append(
        [
            InlineKeyboardButton("◀️", callback_data=prev_cb),
            InlineKeyboardButton(
                f"{_fa_num(page)} / {_fa_num(pages)}", callback_data=_CB_NOOP
            ),
            InlineKeyboardButton("▶️", callback_data=next_cb),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(_STATS_ALL_SYMBOLS, callback_data=_CB_SALL),
            InlineKeyboardButton(_STATS_CLOSE, callback_data=_CB_CLOSE),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """📊 Stats panel — /stats [SYMBOL] [1w|1m|3m|6m|1y|all].

    Sends the report with the filter buttons attached to the message
    (inline keyboard). Tapping a filter edits this same message in place
    instead of stacking new ones.
    """
    flt = context.user_data.setdefault("stats_filter", {})
    if context.args:
        flt["symbol"] = None
        flt["period"] = None
        for arg in context.args:
            token = arg.strip().lower()
            if token in _STATS_PERIODS:
                flt["period"] = token
            elif flt["symbol"] is None:
                flt["symbol"] = token.upper()
    flt.setdefault("symbol", None)
    flt.setdefault("period", None)
    message = await update.effective_chat.send_message(
        _render_stats(flt.get("symbol"), flt.get("period")),
        reply_markup=_stats_panel_kb(flt),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["stats_msg_id"] = message.message_id


def _picker_text(page: int, pages: int) -> str:
    """Header text of the symbol-picker message."""
    return (
        "نمادها — مرتب‌شده بر اساس آخرین معامله\n"
        f"صفحه {_fa_num(page)} از {_fa_num(pages)} · "
        "برای فیلتر آمار یک نماد را بزنید:"
    )


async def _refresh_panel(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, flt: dict
) -> None:
    """Re-render the stats panel on its original message (or send a new one)."""
    text = _render_stats(flt.get("symbol"), flt.get("period"))
    markup = _stats_panel_kb(flt)
    msg_id = context.user_data.get("stats_msg_id")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            logger.info("Stats panel message is gone; sending a new one.")
    message = await context.bot.send_message(
        chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
    )
    context.user_data["stats_msg_id"] = message.message_id


async def _send_export(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    """Build and send the .xlsx file (the panel's Export button)."""
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    path = export.build_export_file()
    logger.info("Sending export %s", path.name)
    with path.open("rb") as document:
        await context.bot.send_document(
            chat_id,
            document,
            filename=path.name,
            caption=f"📊 {path.stem} — همه معاملات",
        )
    path.unlink(missing_ok=True)  # sent; don't leave copies on disk


async def _close_picker(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the symbol-picker message if this tap came from it."""
    picker_id = context.user_data.pop("stats_picker_msg_id", None)
    message = query.message
    if picker_id and message is not None and message.message_id == picker_id:
        try:
            await message.delete()
        except Exception:
            logger.info("Symbol picker message could not be deleted.")


async def on_stats_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle every stat:* inline-button tap (panel + symbol picker)."""
    query = update.callback_query
    match = _STATS_CB_RE.match(query.data or "")
    if match is None:
        await query.answer()
        return
    flt = context.user_data.setdefault("stats_filter", {})
    flt.setdefault("symbol", None)
    flt.setdefault("period", None)
    chat_id = update.effective_chat.id

    if match.group("period"):
        flt["period"] = match.group("period")
        await query.answer()
        await query.edit_message_text(
            _render_stats(flt.get("symbol"), flt.get("period")),
            reply_markup=_stats_panel_kb(flt),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("page"):
        symbols = db.get_all_symbols()
        pages = max(1, math.ceil(len(symbols) / _SYMBOLS_PER_PAGE))
        page = min(max(int(match.group("page")), 1), pages)
        await query.answer()
        await query.edit_message_text(
            _picker_text(page, pages),
            reply_markup=_symbol_picker_kb(page, pages, symbols),
        )
        return
    await query.answer()

    action = query.data
    if match.group("sym") is not None:
        symbol = match.group("sym")
        # Tapping the active symbol again clears the symbol filter.
        flt["symbol"] = None if flt.get("symbol") == symbol else symbol
        await _close_picker(query, context)
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_OPEN:
        # The symbol list gets its own message so the panel stays clean.
        symbols = db.get_all_symbols()
        if not symbols:
            await context.bot.send_message(
                chat_id, "هنوز معامله‌ای ثبت نشده — نمادی برای فیلتر نیست."
            )
            return
        pages = max(1, math.ceil(len(symbols) / _SYMBOLS_PER_PAGE))
        message = await context.bot.send_message(
            chat_id,
            _picker_text(1, pages),
            reply_markup=_symbol_picker_kb(1, pages, symbols),
        )
        context.user_data["stats_picker_msg_id"] = message.message_id
    elif action == _CB_SALL:
        flt["symbol"] = None
        await _close_picker(query, context)
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_RESET:
        flt["symbol"] = None
        flt["period"] = None
        await _close_picker(query, context)
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_EXPORT:
        await _send_export(context, chat_id)
    elif action == _CB_CLOSE:
        await _close_picker(query, context)
    # _CB_NOOP: nothing to do; the spinner is already cleared.


def build_stats_callbacks() -> CallbackQueryHandler:
    """Handler for the inline buttons of the stats panel / symbol picker."""
    return CallbackQueryHandler(on_stats_callback, pattern=_STATS_CB_RE)


# Range buttons of the /recent panel: token -> (label, look-back days).
_RECENT_RANGES = {
    "all": ("All", None),
    "1w": ("1W", 7),
    "1m": ("1M", 30),
}
_RECENT_RANGE_ORDER = ["all", "1w", "1m"]

# Page/range the user is currently browsing; detail views use them for the
# ◀️ Back button (single-user bot, so one slot is enough — same idea as
# stats_msg_id for the stats panel).
_recent_page = 1
_recent_range = "all"


def _recent_since(token: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD cutoff for a recent-panel range token (None = all time)."""
    days = _RECENT_RANGES.get(token or "all", (None, None))[1]
    if days is None:
        return None
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


async def recent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """🕘 Recent — one inline button per trade; tapping SENDS the details.

    10 trades per page. Range buttons (All / 1W / 1M) and ◀️/▶️ paging edit
    the panel message in place; tapping a trade SENDS a separate, airy
    detail message with 🗑 Delete + ❌ Close on it.
    """
    global _recent_range, _recent_page
    flt = context.user_data.setdefault("recent_filter", {})
    if context.args:
        for arg in context.args:
            token = arg.strip().lower()
            if token in _RECENT_RANGES:
                flt["range"] = token
    flt.setdefault("range", "all")
    _recent_range = flt.get("range") or "all"
    _recent_page = 1
    since = _recent_since(flt.get("range"))
    total = db.count_trades(since)
    if not total:
        await update.message.reply_text(
            "هنوز معامله‌ای ثبت نشده — با /trade اولین معامله را ثبت کنید.",
            reply_markup=_MENU_KEYBOARD,
        )
        return
    pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
    rows = db.get_recent(
        _RECENT_PER_PAGE, offset=0, since=since
    )
    message = await update.effective_chat.send_message(
        _recent_panel_text(1, pages),
        reply_markup=_recent_panel_kb(rows, 1, pages),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["recent_panel_msg"] = message.message_id


async def on_recent_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle every rec:* tap: paging, detail send, delete, close, home."""
    global _recent_range, _recent_page
    query = update.callback_query
    match = _RECENT_CB_RE.match(query.data or "")
    if match is None:
        await query.answer()
        return
    chat_id = update.effective_chat.id
    flt = context.user_data.setdefault("recent_filter", {})
    flt.setdefault("range", "all")
    since = _recent_since(flt.get("range"))
    total = db.count_trades(since)
    pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
    if match.group("page") is not None:
        page = min(max(int(match.group("page")), 1), pages)
        _recent_page = page
        rows = db.get_recent(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE, since=since
        )
        await query.answer()
        await query.edit_message_text(
            _recent_panel_text(page, pages),
            reply_markup=_recent_panel_kb(rows, page, pages),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("range") is not None:
        # Tapping the active range again goes back to all-time.
        token = match.group("range")
        flt["range"] = None if flt.get("range") == token else token
        _recent_range = flt.get("range") or "all"
        _recent_page = 1
        since = _recent_since(flt.get("range"))
        total = db.count_trades(since)
        await query.answer()
        if not total:
            await query.edit_message_text(
                "در این بازه معامله‌ای نیست.",
                reply_markup=_recent_panel_kb([], 1, 1),
            )
            return
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        rows = db.get_recent(_RECENT_PER_PAGE, offset=0, since=since)
        await query.edit_message_text(
            _recent_panel_text(1, pages),
            reply_markup=_recent_panel_kb(rows, 1, pages),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("view") is not None:
        row = db.get_trade(int(match.group("view")))
        await query.answer()
        if row is None:
            await update.effective_chat.send_message(
                "این معامله دیگر وجود ندارد."
            )
            return
        # SEND the detail as its own big message; the panel stays intact.
        has_shots = bool(row["screenshot"] or row["screenshot_after"])
        await update.effective_chat.send_message(
            _recent_detail_text(row),
            reply_markup=_recent_detail_kb(row["id"], has_shots),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("photo") is not None:
        await query.answer()
        await _send_trade_photos(update, int(match.group("photo")))
        return
    if match.group("del") is not None:
        trade_id = int(match.group("del"))
        row = db.delete_trade(trade_id)
        if row is None:
            await query.answer("این معامله دیگر وجود ندارد.")
            return
        _purge_screenshots(row)
        logger.info("Deleted trade #%s", trade_id)
        total = db.count_trades(since)
        if not total:
            context.user_data.pop("recent_filter", None)
            await query.answer("حذف شد.")
            await query.edit_message_text(
                "🗑 معامله حذف شد — این آخرین معامله بود.",
                reply_markup=None,
            )
            return
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        page = min(max(_recent_page, 1), pages)
        rows = db.get_recent(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE, since=since
        )
        panel_kb = _recent_panel_kb(rows, page, pages)
        panel_msg = context.user_data.get("recent_panel_msg")
        query_msg = getattr(query, "message", None)
        query_msg_id = getattr(query_msg, "message_id", None)
        await query.answer("🗑 حذف شد.")
        if query_msg_id is not None and panel_msg == query_msg_id:
            # Delete button on the panel itself: refresh it in place.
            await query.edit_message_text(
                _recent_panel_text(page, pages),
                reply_markup=panel_kb,
                parse_mode=ParseMode.HTML,
            )
        else:
            # Delete inside a sent detail message: confirm + refresh panel.
            await query.edit_message_text(
                "🗑 معامله حذف شد ✅",
                reply_markup=None,
            )
            if panel_msg is not None and context.bot is not None:
                try:
                    await context.bot.edit_message_text(
                        _recent_panel_text(page, pages),
                        chat_id=chat_id,
                        message_id=panel_msg,
                        reply_markup=panel_kb,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    logger.info("Could not refresh the recent panel.")
        _recent_page = page
        return
    # home / close / noop
    await query.answer()
    if match.group(0) == _RCB_HOME:
        context.user_data.pop("recent_filter", None)
        _recent_range = "all"
        _recent_page = 1
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.info("Recent panel already cleared.")
    elif match.group(0) == _RCB_CLOSE:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.info("Detail message already closed.")


def build_recent_callbacks() -> CallbackQueryHandler:
    """Handler for the inline buttons of the /recent panel."""
    return CallbackQueryHandler(on_recent_callback, pattern=_RECENT_CB_RE)


# --------------------------------------------------------------------------
# 🟢 Open trades — add questionnaire, inline panel, close flow
# --------------------------------------------------------------------------

# Callback-data namespace for the open-trades panel.
_OCB = "opn"
_OCB_PAGE = f"{_OCB}:p:"
_OCB_VIEW = f"{_OCB}:v:"
_OCB_PHOTO = f"{_OCB}:ph:"
_OCB_CLOSE = f"{_OCB}:c:"
_OCB_DEL = f"{_OCB}:d:"
_OCB_HOME = f"{_OCB}:home"
_OCB_CLOSE_MSG = f"{_OCB}:close"
_OCB_NOOP = f"{_OCB}:noop"
_OCB_ADD = f"{_OCB}:add"
_OPEN_CB_RE = re.compile(
    r"^" + re.escape(_OCB)
    + r":(?:p:(?P<page>\d+)|v:(?P<view>\d+)|ph:(?P<photo>\d+)"
    r"|c:(?P<close>\d+)|d:(?P<del>\d+)|home|close|noop|add)$"
)

# The ➕ panel button and the 🏁 detail button are ENTRY POINTS of their
# conversations (CallbackQueryHandler entry points), so the flows start
# directly from those taps — no synthetic-message dispatch involved.
_OPEN_ADD_RE = re.compile(r"^" + re.escape(_OCB_ADD) + r"$")
_OPEN_CLOSE_RE = re.compile(r"^" + re.escape(_OCB_CLOSE) + r"(?P<id>\d+)$")

# Page the user is currently browsing (same idea as _recent_page).
_open_page = 1


def _open_button(row) -> str:
    """Label of an open trade's list button: emoji, id, symbol, entry, 📷."""
    shots = " 📷" if row["screenshot"] else ""
    return (
        f"🟢 #{row['id']} — {_ESC(row['symbol'])}"
        f" · {_fmt_num(row['entry_price'])}{shots}"
    )


def _open_panel_text(page: int, pages: int) -> str:
    """Heading above the 🟢 button list."""
    return (
        "🟢 <b>معاملات باز</b>\n"
        f"📄 صفحه {_fa_num(page)} از {_fa_num(pages)} — "
        "برای جزئیات و بستن معامله، روی آن بزنید 👇"
    )


def _open_panel_kb(rows: list, page: int, pages: int) -> InlineKeyboardMarkup:
    """Inline keyboard: ➕ add + one button per open trade + pager + home."""
    trade_rows = [
        [
            InlineKeyboardButton(
                _open_button(row), callback_data=_OCB_VIEW + str(row["id"])
            )
        ]
        for row in rows
    ]
    prev_cb = _OCB_PAGE + str(page - 1) if page > 1 else _OCB_NOOP
    next_cb = _OCB_PAGE + str(page + 1) if page < pages else _OCB_NOOP
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ ثبت معامله باز", callback_data=_OCB_ADD)]]
        + trade_rows
        + [
            [
                InlineKeyboardButton("◀️", callback_data=prev_cb),
                InlineKeyboardButton(
                    f"{_fa_num(page)} / {_fa_num(pages)}",
                    callback_data=_OCB_NOOP,
                ),
                InlineKeyboardButton("▶️", callback_data=next_cb),
            ],
            [InlineKeyboardButton("🏠 Home", callback_data=_OCB_HOME)],
        ]
    )


def _open_detail_text(row) -> str:
    """Big, airy detail card for one OPEN trade (HTML)."""
    side = _DIR_LABEL.get(row["direction"], row["direction"].upper())
    side_icon = "📈" if row["direction"] == "long" else "📉"
    market_fa = (
        "🪙 کریپتو" if (row["market"] or "crypto") == "crypto" else "💵 فارکس"
    )
    tf = row["timeframe"] or "—"
    risk = f"{_fmt_num(row['risk_percent'])}%" if row["risk_percent"] else "—"
    tp = _fmt_num(row["take_profit"]) if row["take_profit"] else "—"
    sl = _fmt_num(row["stop_loss"]) if row["stop_loss"] else "—"
    when = row["trade_date"]
    if row["entry_time"]:
        when += f" {row['entry_time']}"
    reason = _ESC(row["reason"]) if row["reason"] else "—"
    return (
        f"🟢 معامله باز #{row['id']} — <b>{_ESC(row['symbol'])}</b>\n"
        f"{side_icon} {side}  •  {market_fa}  •  ⏱ {tf}\n"
        "\n"
        f"• Entry: <code>{_fmt_num(row['entry_price'])}</code>\n"
        f"• 🎯 TP: <code>{tp}</code>\n"
        f"• 🛑 SL: <code>{sl}</code>\n"
        "\n"
        f"• Risk: {risk}\n"
        f"• 📅 Date: {_ESC(when)}\n"
        f"• 💭 Reason: {reason}\n"
        "\n"
        "<i>برای بستن این معامله، دکمه 🏁 را بزنید.</i>"
    )


def _open_detail_kb(trade_id: int, has_shots: bool) -> InlineKeyboardMarkup:
    """Buttons on a sent open-trade detail: 📷, 🏁 close, 🗑 delete, ❌ close."""
    rows = []
    if has_shots:
        rows.append(
            [InlineKeyboardButton("📷 عکس چارت", callback_data=_OCB_PHOTO + str(trade_id))]
        )
    rows.append(
        [InlineKeyboardButton("🏁 Close trade", callback_data=_OCB_CLOSE + str(trade_id))]
    )
    rows.append(
        [
            InlineKeyboardButton("🗑 حذف", callback_data=_OCB_DEL + str(trade_id)),
            InlineKeyboardButton("❌ بستن", callback_data=_OCB_CLOSE_MSG),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def open_trades(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """🟢 Open trades — one inline button per running position (like /recent)."""
    global _open_page
    _open_page = 1
    total = db.count_open_trades()
    if not total:
        # Nothing to show yet — still send the panel (➕ starts the flow)
        # so the button always behaves the same way.
        rows: list = []
        message = await update.effective_chat.send_message(
            "🟢 <b>معاملات باز</b>\n\n"
            "معامله‌ای باز نیست. با دکمه زیر یک معاملهٔ باز ثبت کنید:",
            reply_markup=_open_panel_kb(rows, 1, 1),
            parse_mode=ParseMode.HTML,
        )
        context.user_data["open_panel_msg"] = message.message_id
        return
    pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
    rows = db.get_open_trades(_RECENT_PER_PAGE, offset=0)
    message = await update.effective_chat.send_message(
        _open_panel_text(1, pages),
        reply_markup=_open_panel_kb(rows, 1, pages),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["open_panel_msg"] = message.message_id


async def open_trades_add_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """➕ panel button — entry point of the open-trades questionnaire."""
    await update.callback_query.answer()
    return await open_trade_start(update, context)


async def open_trades_close_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """🏁 detail button — entry point of the close-an-open-trade flow."""
    match = _OPEN_CLOSE_RE.match(update.callback_query.data or "")
    if match is None:
        await update.callback_query.answer()
        return ConversationHandler.END
    open_id = int(match.group("id"))
    await update.callback_query.answer()
    return await _close_begin(open_id, update, context)


async def on_open_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle every opn:* tap: paging, detail send, close start, delete."""
    global _open_page
    query = update.callback_query
    match = _OPEN_CB_RE.match(query.data or "")
    if match is None:
        await query.answer()
        return
    chat_id = update.effective_chat.id
    if match.group("page") is not None:
        total = db.count_open_trades()
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        page = min(max(int(match.group("page")), 1), pages)
        _open_page = page
        rows = db.get_open_trades(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE
        )
        await query.answer()
        await query.edit_message_text(
            _open_panel_text(page, pages),
            reply_markup=_open_panel_kb(rows, page, pages),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("view") is not None:
        row = db.get_open_trade(int(match.group("view")))
        await query.answer()
        if row is None:
            await update.effective_chat.send_message("این معامله دیگر باز نیست.")
            return
        # SEND the detail as its own message; the panel stays intact.
        await update.effective_chat.send_message(
            _open_detail_text(row),
            reply_markup=_open_detail_kb(row["id"], bool(row["screenshot"])),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("photo") is not None:
        await query.answer()
        row = db.get_open_trade(int(match.group("photo")))
        if row is None or not row["screenshot"]:
            await update.effective_chat.send_message("این معامله اسکرین‌شات ندارد.")
            return
        path = _screenshot_path(row["screenshot"])
        if not path.is_file():
            await update.effective_chat.send_message(
                f"فایل اسکرین‌شات معامله #{row['id']} روی دیسک پیدا نشد."
            )
            return
        with path.open("rb") as photo:
            await update.effective_chat.send_photo(
                photo,
                caption=f"#{row['id']} {row['symbol']} {row['trade_date']} — چارت ورود",
            )
        return


    if match.group("del") is not None:
        open_id = int(match.group("del"))
        row = db.delete_open_trade(open_id)
        if row is None:
            await query.answer("این معامله دیگر باز نیست.")
            return
        for name in (row["screenshot"] or "").splitlines():
            try:
                _screenshot_path(name).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove screenshot for deleted open trade #%s",
                    open_id,
                )
        logger.info("Deleted open trade #%s", open_id)
        total = db.count_open_trades()
        panel_msg = context.user_data.get("open_panel_msg")
        query_msg = getattr(query, "message", None)
        query_msg_id = getattr(query_msg, "message_id", None)
        if not total:
            context.user_data.pop("open_panel_msg", None)
            await query.answer("حذف شد.")
            await query.edit_message_text(
                "🗑 معامله باز حذف شد — معامله بازی نمانده است.",
                reply_markup=None,
            )
            return
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        page = min(max(_open_page, 1), pages)
        _open_page = page
        rows = db.get_open_trades(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE
        )
        panel_kb = _open_panel_kb(rows, page, pages)
        await query.answer("🗑 حذف شد.")
        if query_msg_id is not None and panel_msg == query_msg_id:
            # Delete button on the panel itself: refresh it in place.
            await query.edit_message_text(
                _open_panel_text(page, pages),
                reply_markup=panel_kb,
                parse_mode=ParseMode.HTML,
            )
        else:
            # Delete inside a sent detail message: confirm + refresh panel.
            await query.edit_message_text(
                "🗑 معامله باز حذف شد ✅", reply_markup=None
            )
            if panel_msg is not None and context.bot is not None:
                try:
                    await context.bot.edit_message_text(
                        _open_panel_text(page, pages),
                        chat_id=chat_id,
                        message_id=panel_msg,
                        reply_markup=panel_kb,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    logger.info("Could not refresh the open-trades panel.")
        return
    if match.group("close") is not None:
        open_id = int(match.group("close"))
        row = db.get_open_trade(open_id)
        if row is None:
            await query.answer("این معامله دیگر باز نیست.")
            return
        context.user_data.clear()
        context.user_data["open_id"] = open_id
        context.user_data["open_symbol"] = row["symbol"]
        await query.answer()
        await update.effective_chat.send_message(
            f"بستن معامله #{open_id} {_ESC(row['symbol'])} — نتیجه؟",
            reply_markup=_STATUS_KEYBOARD,
        )
        return
    # home / close-msg / noop  (➕ and 🏁 are conversation entry points)
    await query.answer()
    if match.group(0) == _OCB_HOME:
        context.user_data.pop("open_panel_msg", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.info("Open-trades panel already cleared.")
    elif match.group(0) == _OCB_CLOSE_MSG:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.info("Detail message already closed.")


def build_open_callbacks() -> CallbackQueryHandler:
    """Handler for the inline buttons of the 🟢 open-trades panel."""
    return CallbackQueryHandler(on_open_callback, pattern=_OPEN_CB_RE)


# --- 🟢 open-trades questionnaire (add) ---------------------------------------

async def open_trade_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the open-trades questionnaire with the market question."""
    _drop_screenshot(context)
    context.user_data.clear()
    text = (
        "معامله باز جدید — در کدام بازار معامله کردی؟\n"
        "(برای انصراف /cancel را بفرستید)"
    )
    if update.message is not None:
        await update.message.reply_text(text, reply_markup=_MARKET_KEYBOARD)
    else:
        # Entry via the ➕ button: there is no message to reply to.
        await update.effective_chat.send_message(
            text, reply_markup=_MARKET_KEYBOARD
        )
    return OPEN_MARKET


async def ask_open_market(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip().lower()
    if raw in _MARKET_CRYPTO_TOKENS:
        context.user_data["market"] = "crypto"
    elif raw in _MARKET_FOREX_TOKENS:
        context.user_data["market"] = "forex"
    else:
        await update.message.reply_text(
            "یکی از دو دکمه را بزنید: 🪙 کریپتو یا 💵 فارکس",
            reply_markup=_MARKET_KEYBOARD,
        )
        return OPEN_MARKET
    symbol_kb = _symbol_keyboard()
    if symbol_kb is not None:
        text = (
            "نماد — یکی از نمادهای زیر را بزنید یا نماد دیگری بنویسید؛\n"
            "برای انصراف /cancel را بفرستید."
        )
    else:
        text = (
            "برای انصراف /cancel را بفرستید.\n\n"
            "Symbol:\n"
            "e.g. EURUSD · BTCUSD · XAUUSD"
        )
    await update.message.reply_text(
        text, reply_markup=symbol_kb or _KEYBOARD_GONE
    )
    return OPEN_SYMBOL


async def ask_open_symbol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    symbol = (update.message.text or "").strip().upper()
    if not symbol or len(symbol) > 24 or any(ch.isspace() for ch in symbol):
        await update.message.reply_text(
            "این شبیه نماد نیست — دوباره تایپ کنید (مثلاً EURUSD):",
            reply_markup=_symbol_keyboard() or _KEYBOARD_GONE,
        )
        return OPEN_SYMBOL
    context.user_data["symbol"] = symbol
    await update.effective_chat.send_message(
        "جهت معامله؟", reply_markup=_DIR_KEYBOARD
    )
    return OPEN_DIRECTION


async def ask_open_direction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    word = (update.message.text or "").strip().lower()
    if word in _LONG_ALIASES:
        context.user_data["direction"] = "long"
    elif word in _SHORT_ALIASES:
        context.user_data["direction"] = "short"
    else:
        await update.message.reply_text(
            "لطفاً یکی از دکمه‌ها را بزنید یا Long / Short بنویسید:",
            reply_markup=_DIR_KEYBOARD,
        )
        return OPEN_DIRECTION
    await update.effective_chat.send_message(
        "تایم‌فریم (Timeframe):", reply_markup=_TF_KEYBOARD
    )
    return OPEN_TIMEFRAME


async def ask_open_timeframe(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    timeframe = _parse_timeframe(update.message.text or "")
    if timeframe is None:
        await update.message.reply_text(
            "تایم‌فریم نامعتبر.\n"
            "1m · 5m · 15m · 30m · 1h · 4h · 1D · 1W · 1M"
        )
        return OPEN_TIMEFRAME
    context.user_data["timeframe"] = timeframe
    return await _prompt_open_reason(update)


async def _prompt_open_reason(update: Update) -> int:
    await update.effective_chat.send_message(
        "📝 دلیل ورود — چرا وارد این معامله شدی؟",
        reply_markup=_NOTES_KEYBOARD,
    )
    return OPEN_REASON


async def ask_open_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["reason"] = (
        "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    )
    await update.effective_chat.send_message(
        "📸 اسکرین‌شات چارت — لحظه ورود (اختیاری):",
        reply_markup=_SHOT_KEYBOARD,
    )
    return OPEN_SCREENSHOT


async def ask_open_screenshot(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not await _store_screenshot(update, context, "screenshot"):
        await update.message.reply_text(
            "تصویر خوانده نشد — دوباره امتحان کنید یا دکمه ⏭ بدون اسکرین‌شات را بزنید."
        )
        return OPEN_SCREENSHOT
    return await _prompt_open_date(update)


async def ask_open_screenshot_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() not in _SKIP_SHOT_TOKENS:
        await update.message.reply_text(
            "لطفاً یک تصویر بفرستید، دکمه ⏭ بدون اسکرین‌شات را بزنید، یا '-' را بنویسید."
        )
        return OPEN_SCREENSHOT
    return await _prompt_open_date(update)


async def _prompt_open_date(update: Update) -> int:
    await update.effective_chat.send_message(
        "تاریخ ورود:\nYYYY-MM-DD  (e.g. 2026-02-09)",
        reply_markup=_DATE_KEYBOARD,
    )
    return OPEN_TRADE_DATE


async def ask_open_trade_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS or raw.lower() in _TODAY_TOKENS:
        context.user_data["trade_date"] = date.today().isoformat()
        return await _prompt_open_hour(update)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt.endswith("%H:%M"):
            context.user_data["trade_date"] = parsed.strftime("%Y-%m-%d")
            context.user_data["entry_time"] = parsed.strftime("%H:%M")
            return await _prompt_open_risk(update)
        context.user_data["trade_date"] = parsed.date().isoformat()
        return await _prompt_open_hour(update)
    await update.message.reply_text(
        "تاریخ نامعتبر.\n"
        "YYYY-MM-DD یا YYYY-MM-DD HH:MM  (e.g. 2026-02-09 14:30)\n"
        "یا دکمه «📅 امروز» را بزنید."
    )
    return OPEN_TRADE_DATE


async def _prompt_open_hour(update: Update) -> int:
    await update.effective_chat.send_message(
        "ساعت ورود:\nHH:MM  (e.g. 14:30)",
        reply_markup=_HOUR_KEYBOARD,
    )
    return OPEN_TRADE_HOUR


async def ask_open_trade_hour(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _NOW_TOKENS:
        context.user_data["entry_time"] = datetime.now().strftime("%H:%M")
        return await _prompt_open_risk(update)
    if raw.lower() in _SKIP_HOUR_TOKENS:
        context.user_data.setdefault("entry_time", "")
        return await _prompt_open_risk(update)
    match = _HOUR_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        await update.message.reply_text(
            "ساعت نامعتبر.\nHH یا HH:MM  (e.g. 14:30)"
        )
        return OPEN_TRADE_HOUR
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    context.user_data["entry_time"] = f"{hour:02d}:{minute:02d}"
    return await _prompt_open_risk(update)


async def _prompt_open_risk(update: Update) -> int:
    await update.effective_chat.send_message(
        "⚠️ Risk — چند درصد از حساب؟ (مثلاً 1 یا 1%)",
        reply_markup=_RISK_KEYBOARD,
    )
    return OPEN_RISK


async def ask_open_risk(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _SKIP_RISK_TOKENS:
        context.user_data.pop("risk_percent", None)
        return await _prompt_open_entry(update)
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد ریسک نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2 یا 2%):"
        )
        return OPEN_RISK
    context.user_data["risk_percent"] = number
    return await _prompt_open_entry(update)


async def _prompt_open_entry(update: Update) -> int:
    await update.effective_chat.send_message(
        "Entry price:", reply_markup=_KEYBOARD_GONE
    )
    return OPEN_ENTRY


async def ask_open_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "یک عدد مثبت وارد کنید (اعشار با نقطه، مثل 10.5):"
        )
        return OPEN_ENTRY
    context.user_data["entry_price"] = number
    await update.effective_chat.send_message(
        "🎯 Take Profit (TP):", reply_markup=_KEYBOARD_GONE
    )
    return OPEN_TAKE_PROFIT


async def ask_open_take_profit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "حد سود را به‌صورت یک عدد مثبت وارد کنید (اعشار با نقطه):"
        )
        return OPEN_TAKE_PROFIT
    context.user_data["take_profit"] = number
    await update.effective_chat.send_message(
        "🛑 Stop Loss (SL):", reply_markup=_KEYBOARD_GONE
    )
    return OPEN_STOP_LOSS


async def ask_open_stop_loss(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "حد ضرر را به‌صورت یک عدد مثبت وارد کنید (اعشار با نقطه):"
        )
        return OPEN_STOP_LOSS
    context.user_data["stop_loss"] = number
    return await _prompt_open_confirm(update, context)


def _open_summary(data: dict) -> str:
    """Render the airy confirmation summary for the open-trade draft (HTML)."""
    market_fa = (
        "🪙 کریپتو" if (data.get("market") or "crypto") == "crypto" else "💵 فارکس"
    )
    symbol = _ESC(data["symbol"])
    risk = data.get("risk_percent")
    when = data.get("trade_date", "")
    if data.get("entry_time"):
        when += f" {data['entry_time']}"
    reason = _ESC(data["reason"]) if data.get("reason") else ""
    shot = "📷" if data.get("screenshot") else ""
    return (
        "🔎 <b>تأیید معامله باز</b>\n"
        "————————————————\n"
        "\n"
        "◾ <i>معامله</i>\n"
        f"• Market    {market_fa}\n"
        f"• Symbol    <b>{symbol}</b>\n"
        f"• Side      {_DIR_LABEL.get(data['direction'], data['direction'])}\n"
        f"• TF        {data.get('timeframe') or '-'}\n"
        + (f"• Reason    {reason}\n" if data.get("reason") else "")
        + (f"• Shot      {shot}\n" if shot else "")
        + "\n"
        "◾ <i>ورود</i>\n"
        f"• Date      {when or '-'}\n"
        f"• Risk      {(_fmt_num(risk) + '%') if risk else '-'}\n"
        f"• Entry     {_fmt_num(data['entry_price'])}\n"
        f"• TP / SL   {_fmt_num(data['take_profit'])}"
        f" / {_fmt_num(data['stop_loss'])}\n"
        "\n"
        "این معامله در «معاملات باز» ذخیره می‌شود؛ بعداً از همان‌جا ببندیدش.\n"
        "ثبت شود؟"
    )


async def _prompt_open_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    summary = _open_summary(context.user_data)
    try:
        await update.effective_chat.send_message(
            summary,
            reply_markup=_OPEN_CONFIRM_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.error("HTML open confirm failed:\n%s", traceback.format_exc())
        await update.effective_chat.send_message(
            re.sub(r"</?[bi]>", "", summary),
            reply_markup=_OPEN_CONFIRM_KEYBOARD,
        )
    return OPEN_CONFIRM


async def save_open_trade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer = (update.message.text or "").strip().lower()
    if answer in ("y", "yes", "✅ ثبت", "ثبت", "بله", "✅ ذخیره", "ذخیره"):
        data = dict(context.user_data)
        context.user_data.clear()
        open_id = db.add_open_trade(
            symbol=data["symbol"],
            direction=data["direction"],
            market=data.get("market") or "crypto",
            timeframe=data.get("timeframe") or "",
            reason=data.get("reason") or "",
            screenshot=data.get("screenshot"),
            trade_date=data["trade_date"],
            entry_time=data.get("entry_time") or "",
            risk_percent=data.get("risk_percent"),
            entry_price=data["entry_price"],
            take_profit=data.get("take_profit"),
            stop_loss=data.get("stop_loss"),
        )
        logger.info("Saved open trade #%s %s", open_id, data["symbol"])
        symbol = _ESC(data["symbol"])
        when = data.get("trade_date", "")
        if data.get("entry_time"):
            when += f" {data['entry_time']}"
        text = (
            f"🟢 <b>معامله باز #{open_id} ثبت شد</b>\n"
            "\n"
            f"• <b>{symbol}</b> · "
            f"{_DIR_LABEL.get(data['direction'], data['direction'])}"
            + (f" · {data.get('timeframe')}" if data.get("timeframe") else "")
            + "\n"
            f"• Entry: {_fmt_num(data['entry_price'])}\n"
            f"• TP / SL: {_fmt_num(data['take_profit'])}"
            f" / {_fmt_num(data['stop_loss'])}\n"
            f"• 📅 {when}\n"
            "\n"
            "وقتی بستی، از 🟢 معاملات باز با دکمه 🏁 ببندش."
        )
        try:
            await update.message.reply_text(
                text, reply_markup=_MENU_KEYBOARD, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.error(
                "HTML open confirmation failed:\n%s", traceback.format_exc()
            )
            await update.message.reply_text(
                re.sub(r"</?[bi]>", "", text), reply_markup=_MENU_KEYBOARD
            )
        return ConversationHandler.END
    if answer in ("n", "no", "❌ ثبت نشود", "❌ discard", "خیر", "ثبت نشود"):
        _drop_screenshot(context)
        context.user_data.clear()
        await update.message.reply_text(
            "❌ ثبت نشد — چیزی ذخیره نشد.", reply_markup=_MENU_KEYBOARD
        )
        return ConversationHandler.END
    await update.message.reply_text("لطفاً یکی از دکمه‌ها را انتخاب کنید:")
    return OPEN_CONFIRM


# --- 🏁 close-an-open-trade questionnaire -------------------------------------

async def close_start_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """/close <id> — start the close flow from a command."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "روش استفاده: /close <شماره> (شماره‌ها در 🟢 معاملات باز)"
        )
        return ConversationHandler.END
    return await _close_begin(int(context.args[0]), update, context)


async def _close_begin(
    open_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    row = db.get_open_trade(open_id)
    if row is None:
        text = f"معامله بازی با شماره #{open_id} پیدا نشد."
        if update.message is not None:
            await update.message.reply_text(text)
        else:
            await update.effective_chat.send_message(text)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["open_id"] = open_id
    context.user_data["open_symbol"] = row["symbol"]
    await update.effective_chat.send_message(
        f"بستن معامله #{open_id} {_ESC(row['symbol'])} — نتیجه؟",
        reply_markup=_STATUS_KEYBOARD,
    )
    return CLOSE_STATUS


async def ask_close_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip().lower()
    status = _STATUS_TOKENS.get(raw)
    if status is None:
        await update.message.reply_text(
            "نتیجه را انتخاب کنید: ✅ Win (TP) / ❌ Loss (SL) / ➖ BE / ✏️ Manual"
        )
        return CLOSE_STATUS
    context.user_data["hit"] = status
    await update.effective_chat.send_message(
        "تاریخ بستن معامله:\nYYYY-MM-DD  (e.g. 2026-02-09)",
        reply_markup=_DATE_KEYBOARD,
    )
    return CLOSE_DATE


async def ask_close_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS or raw.lower() in _TODAY_TOKENS:
        context.user_data["trade_date"] = date.today().isoformat()
        return await _prompt_close_hour(update)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt.endswith("%H:%M"):
            context.user_data["trade_date"] = parsed.strftime("%Y-%m-%d")
            context.user_data["exit_time"] = parsed.strftime("%H:%M")
            return await _prompt_close_price(update, context)
        context.user_data["trade_date"] = parsed.date().isoformat()
        return await _prompt_close_hour(update)
    await update.message.reply_text(
        "تاریخ نامعتبر.\n"
        "YYYY-MM-DD یا YYYY-MM-DD HH:MM  (e.g. 2026-02-09 14:30)\n"
        "یا دکمه «📅 امروز» را بزنید."
    )
    return CLOSE_DATE


async def _prompt_close_hour(update: Update) -> int:
    await update.effective_chat.send_message(
        "ساعت بستن:\nHH:MM  (e.g. 14:30)",
        reply_markup=_HOUR_KEYBOARD,
    )
    return CLOSE_HOUR


async def ask_close_hour(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _NOW_TOKENS:
        context.user_data["exit_time"] = datetime.now().strftime("%H:%M")
        return await _prompt_close_price(update, context)
    if raw.lower() in _SKIP_HOUR_TOKENS:
        context.user_data.setdefault("exit_time", "")
        return await _prompt_close_price(update, context)
    match = _HOUR_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        await update.message.reply_text(
            "ساعت نامعتبر.\nHH یا HH:MM  (e.g. 14:30)"
        )
        return CLOSE_HOUR
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    context.user_data["exit_time"] = f"{hour:02d}:{minute:02d}"
    return await _prompt_close_price(update, context)


async def _prompt_close_price(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """TP/SL/BE exits auto-fill the exit price; manual exits ask for it."""
    hit = context.user_data.get("hit")
    open_id = context.user_data.get("open_id")
    row = db.get_open_trade(open_id) if open_id else None
    if hit == "win" and row is not None and row["take_profit"]:
        context.user_data["exit_price"] = row["take_profit"]
        await update.effective_chat.send_message(
            f"🎯 Exit price: {_fmt_num(row['take_profit'])} (TP hit)",
            reply_markup=_KEYBOARD_GONE,
        )
        return await _prompt_close_photos(update)
    if hit == "loss" and row is not None and row["stop_loss"]:
        context.user_data["exit_price"] = row["stop_loss"]
        await update.effective_chat.send_message(
            f"🛑 Exit price: {_fmt_num(row['stop_loss'])} (SL hit)",
            reply_markup=_KEYBOARD_GONE,
        )
        return await _prompt_close_photos(update)
    if hit == "be" and row is not None:
        context.user_data["exit_price"] = row["entry_price"]
        await update.effective_chat.send_message(
            f"➖ Exit price: {_fmt_num(row['entry_price'])} (breakeven)",
            reply_markup=_KEYBOARD_GONE,
        )
        return await _prompt_close_photos(update)
    await update.effective_chat.send_message(
        "Exit price:", reply_markup=_KEYBOARD_GONE
    )
    return CLOSE_PRICE


async def ask_close_price(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "یک عدد مثبت وارد کنید (اعشار با نقطه، مثل 10.5):"
        )
        return CLOSE_PRICE
    context.user_data["exit_price"] = number
    return await _prompt_close_photos(update)


async def _prompt_close_photos(update: Update) -> int:
    await update.effective_chat.send_message(
        "📸 اسکرین‌شات خروج — تا ۴ تصویر، یکی‌یکی بفرستید (اختیاری):",
        reply_markup=_SHOT_KEYBOARD,
    )
    return CLOSE_PHOTOS


async def ask_close_photos(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    names = context.user_data.get("exit_photos") or ""
    count = len([n for n in names.splitlines() if n])
    if count >= _MAX_EXIT_PHOTOS:
        await update.message.reply_text(
            f"حداکثر {_MAX_EXIT_PHOTOS} تصویر — دکمه ⏭ را بزنید."
        )
        return CLOSE_PHOTOS
    if not await _store_screenshot(update, context, "exit_photo"):
        await update.message.reply_text(
            "تصویر خوانده نشد — دوباره امتحان کنید یا دکمه ⏭ را بزنید."
        )
        return CLOSE_PHOTOS
    new_name = context.user_data.pop("exit_photo")
    context.user_data["exit_photos"] = (names + "\n" + new_name).strip()
    kept = len(context.user_data["exit_photos"].splitlines())
    if kept >= _MAX_EXIT_PHOTOS:
        return await _prompt_close_reason(update)
    await update.effective_chat.send_message(
        f"📥 {_fa_num(kept)} از {_fa_num(_MAX_EXIT_PHOTOS)} — "
        "تصویر بعدی یا ⏭ بدون اسکرین‌شات:",
        reply_markup=_SHOT_KEYBOARD,
    )
    return CLOSE_PHOTOS


async def ask_close_photos_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() not in _SKIP_SHOT_TOKENS:
        await update.message.reply_text(
            "لطفاً یک تصویر بفرستید، دکمه ⏭ بدون اسکرین‌شات را بزنید، یا '-' را بنویسید."
        )
        return CLOSE_PHOTOS
    return await _prompt_close_reason(update)


async def _prompt_close_reason(update: Update) -> int:
    await update.effective_chat.send_message(
        "📝 دلیل خروج — چرا از معامله خارج شدی؟",
        reply_markup=_NOTES_KEYBOARD,
    )
    return CLOSE_REASON


async def ask_close_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["notes"] = (
        "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    )
    return await _prompt_mood(update)


async def ask_close_mood(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    mood = _MOOD_ALIASES.get(raw.lower())
    if raw.lower() in _SKIP_MOOD_TOKENS:
        context.user_data.pop("mood", None)
    elif mood is not None:
        context.user_data["mood"] = mood
    else:
        await update.message.reply_text(
            "یکی از حال‌وهواهای زیر را انتخاب کنید، مثلاً «آرام» یا «فومو» "
            "بنویسید، یا رد کنید:"
        )
        return MOOD
    return await _prompt_close_confirm(update, context)


def _close_summary(data: dict) -> str:
    """Render the airy confirmation summary for the close draft (HTML)."""
    hit = data.get("hit") or ""
    emoji = _OPEN_EMOJI.get(hit, "✏️")
    label = _OPEN_STATUS_LABELS.get(hit, hit)
    symbol = _ESC(data.get("open_symbol", ""))
    when = data.get("trade_date", "")
    if data.get("exit_time"):
        when += f" {data['exit_time']}"
    shots = len((data.get("exit_photos") or "").splitlines())
    reason = _ESC(data["notes"]) if data.get("notes") else ""
    mood = data.get("mood")
    return (
        "🔎 <b>تأیید بستن معامله</b>\n"
        "————————————————\n"
        "\n"
        "◾ <i>خروج</i>\n"
        f"• Symbol    <b>{symbol}</b>\n"
        f"• Status    {emoji} {label}\n"
        f"• Exit      {_fmt_num(data['exit_price'])}\n"
        f"• Date      {when or '-'}\n"
        + (f"• Shots     {_fa_num(shots)}\n" if shots else "")
        + (f"• Reason    {reason}\n" if data.get("notes") else "")
        + (f"• Mood      {_MOOD_LABELS.get(mood, mood)}\n" if mood else "")
        + "\n"
        "معامله به تاریخچه معاملات بسته‌شده منتقل می‌شود.\n"
        "ثبت شود؟"
    )


async def _prompt_close_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    summary = _close_summary(context.user_data)
    try:
        await update.effective_chat.send_message(
            summary,
            reply_markup=_OPEN_CONFIRM_KEYBOARD,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.error("HTML close confirm failed:\n%s", traceback.format_exc())
        await update.effective_chat.send_message(
            re.sub(r"</?[bi]>", "", summary),
            reply_markup=_OPEN_CONFIRM_KEYBOARD,
        )
    return CLOSE_CONFIRM


async def save_close_trade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer = (update.message.text or "").strip().lower()
    if answer in ("y", "yes", "✅ ثبت", "ثبت", "بله", "✅ ذخیره", "ذخیره"):
        data = dict(context.user_data)
        context.user_data.clear()
        open_id = data.pop("open_id")
        new_id = db.close_open_trade(
            open_id,
            hit=data["hit"],
            exit_price=data["exit_price"],
            trade_date=data["trade_date"],
            exit_time=data.get("exit_time") or "",
            notes=data.get("notes") or "",
            mood=data.get("mood") or "",
            exit_photos=data.get("exit_photos") or None,
            screenshot_after=None,
        )
        if new_id is None:
            await update.message.reply_text(
                "این معامله دیگر باز نیست — شاید قبلاً بسته شده باشد.",
                reply_markup=_MENU_KEYBOARD,
            )
            return ConversationHandler.END
        logger.info(
            "Closed open trade #%s -> trade #%s (%s)",
            open_id, new_id, data.get("hit"),
        )
        emoji = _OPEN_EMOJI.get(data["hit"], "✏️")
        label = _OPEN_STATUS_LABELS.get(data["hit"], data["hit"])
        text = (
            f"{emoji} <b>معامله بسته شد</b>\n"
            "\n"
            f"• <b>{_ESC(data.get('open_symbol', ''))}</b>"
            f" · #{new_id}\n"
            f"• {label} · Exit {_fmt_num(data['exit_price'])}\n"
            f"• 📅 {data['trade_date']}"
            + (f" {data['exit_time']}" if data.get("exit_time") else "")
            + "\n"
            "\n"
            "در 🕘 معاملات اخیر و 📊 آمار قابل مشاهده است."
        )
        try:
            await update.message.reply_text(
                text, reply_markup=_MENU_KEYBOARD, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.error(
                "HTML close confirmation failed:\n%s", traceback.format_exc()
            )
            await update.message.reply_text(
                re.sub(r"</?[bi]>", "", text), reply_markup=_MENU_KEYBOARD
            )
        return ConversationHandler.END
    if answer in ("n", "no", "❌ ثبت نشود", "❌ discard", "خیر", "ثبت نشود"):
        _drop_screenshot(context)
        context.user_data.clear()
        await update.message.reply_text(
            "❌ ثبت نشد — معامله هنوز باز است.", reply_markup=_MENU_KEYBOARD
        )
        return ConversationHandler.END
    await update.message.reply_text("لطفاً یکی از دکمه‌ها را انتخاب کنید:")
    return CLOSE_CONFIRM


def build_open_conversation() -> ConversationHandler:
    """Build the 🟢 open-trades questionnaire handler.

    The ➕ button on the 🟢 panel is the conversation's entry point
    (CallbackQueryHandler), so the flow starts directly from that tap. /open
    and the 🟢 menu button always land on the panel itself.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(open_trades_add_entry, pattern=_OPEN_ADD_RE),
        ],
        states={
            OPEN_MARKET: [MessageHandler(_ANSWER, ask_open_market)],
            OPEN_SYMBOL: [MessageHandler(_ANSWER, ask_open_symbol)],
            OPEN_DIRECTION: [MessageHandler(_ANSWER, ask_open_direction)],
            OPEN_TIMEFRAME: [MessageHandler(_ANSWER, ask_open_timeframe)],
            OPEN_REASON: [MessageHandler(_ANSWER, ask_open_reason)],
            OPEN_SCREENSHOT: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, ask_open_screenshot
                ),
                MessageHandler(_ANSWER, ask_open_screenshot_text),
            ],
            OPEN_TRADE_DATE: [MessageHandler(_ANSWER, ask_open_trade_date)],
            OPEN_TRADE_HOUR: [MessageHandler(_ANSWER, ask_open_trade_hour)],
            OPEN_RISK: [MessageHandler(_ANSWER, ask_open_risk)],
            OPEN_ENTRY: [MessageHandler(_ANSWER, ask_open_entry)],
            OPEN_TAKE_PROFIT: [MessageHandler(_ANSWER, ask_open_take_profit)],
            OPEN_STOP_LOSS: [MessageHandler(_ANSWER, ask_open_stop_loss)],
            OPEN_CONFIRM: [MessageHandler(_ANSWER, save_open_trade)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(_CANCEL_RE), cancel),
        ],
        allow_reentry=True,
        per_message=False,
    )


def build_close_conversation() -> ConversationHandler:
    """Build the 🏁 close-an-open-trade questionnaire handler.

    Entry points: the 🏁 button on an open-trade detail card and the
    /close <id> command.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(open_trades_close_entry, pattern=_OPEN_CLOSE_RE),
            CommandHandler("close", close_start_text),
        ],
        states={
            CLOSE_STATUS: [MessageHandler(_ANSWER, ask_close_status)],
            CLOSE_DATE: [MessageHandler(_ANSWER, ask_close_date)],
            CLOSE_HOUR: [MessageHandler(_ANSWER, ask_close_hour)],
            CLOSE_PRICE: [MessageHandler(_ANSWER, ask_close_price)],
            CLOSE_PHOTOS: [
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, ask_close_photos
                ),
                MessageHandler(_ANSWER, ask_close_photos_text),
            ],
            CLOSE_REASON: [MessageHandler(_ANSWER, ask_close_reason)],
            CLOSE_MOOD: [MessageHandler(_ANSWER, ask_close_mood)],
            CLOSE_CONFIRM: [MessageHandler(_ANSWER, save_close_trade)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(_CANCEL_RE), cancel),
        ],
        allow_reentry=True,
        per_message=False,
    )




# --------------------------------------------------------------------------
# /recent — paginated INLINE panel (10 per page, tap for details).
# --------------------------------------------------------------------------

# Trades per page in the /recent panel (spec: 10).
_RECENT_PER_PAGE = 10

# Callback-data namespace for the recent panel (same style as stat:*).
_RCB = "rec"
_RCB_PAGE = f"{_RCB}:p:"
_RCB_RANGE = f"{_RCB}:r:"
_RCB_VIEW = f"{_RCB}:v:"
_RCB_PHOTO = f"{_RCB}:ph:"
_RCB_DEL = f"{_RCB}:d:"
_RCB_HOME = f"{_RCB}:home"
_RCB_CLOSE = f"{_RCB}:close"
_RCB_NOOP = f"{_RCB}:noop"
_RECENT_CB_RE = re.compile(
    r"^" + re.escape(_RCB)
    + r":(?:p:(?P<page>\d+)|r:(?P<range>all|1w|1m)"
    r"|v:(?P<view>\d+)|ph:(?P<photo>\d+)|d:(?P<del>\d+)|home|close|noop)$"
)


def _recent_button(row) -> str:
    """Label of a trade's list button: emoji, id, symbol, P&L, 📷 mark."""
    emoji = _result_emoji(row["hit"])
    shots = " 📷" if row["screenshot"] or row["screenshot_after"] else ""
    # Open-flow closes have no margin question, so P&L can be NULL.
    pnl_txt = _fmt_pnl(row["pnl"]) if row["pnl"] is not None else "—"
    return (
        f"{emoji} #{row['id']} — {_ESC(row['symbol'])}"
        f" · {pnl_txt}{shots}"
    )


def _recent_panel_text(page: int, pages: int) -> str:
    """Heading above the /recent button list (the trades ARE the buttons)."""
    return (
        "🕘 <b>معاملات اخیر</b>\n"
        f"📄 صفحه {_fa_num(page)} از {_fa_num(pages)} — "
        "برای دیدن جزئیات کامل، روی معامله بزنید 👇"
    )


def _recent_detail_text(row) -> str:
    """Big, airy detail card for one trade (HTML) — sent as its own message."""
    roi = row["roi"]
    if roi is None and row["size"]:
        roi = row["pnl"] / row["size"] * 100.0
    emoji = _result_emoji(row["hit"])
    side = _DIR_LABEL.get(row["direction"], row["direction"].upper())
    side_icon = "📈" if row["direction"] == "long" else "📉"
    market_fa = (
        "🪙 کریپتو" if (row["market"] or "crypto") == "crypto" else "💵 فارکس"
    )
    tf = row["timeframe"] or "—"
    lev = f"{_fmt_num(row['leverage'])}x" if row["leverage"] else "—"
    risk = f"{_fmt_num(row['risk_percent'])}%" if row["risk_percent"] else "—"
    entry = _fmt_num(row["entry_price"])
    exit_ = _fmt_num(row["exit_price"]) if row["exit_price"] else "—"
    tp = _fmt_num(row["take_profit"]) if row["take_profit"] else "—"
    sl = _fmt_num(row["stop_loss"]) if row["stop_loss"] else "—"
    hit = row["hit"]
    result = (
        f"{_result_emoji(hit)} {_RESULT_LABELS.get(hit, '—')}" if hit else "—"
    )
    mood = _MOOD_LABELS.get(row["mood"], row["mood"]) if row["mood"] else "—"
    notes = _ESC(row["notes"]) if row["notes"] else "—"
    shots = []
    if row["screenshot"]:
        shots.append("قبل")
    if row["screenshot_after"]:
        shots.append("بعد")
    shots_txt = "  •  ".join(shots) if shots else "—"
    return (
        f"{emoji} معامله #{row['id']} — <b>{_ESC(row['symbol'])}</b>\n"
        f"{side_icon} {side}  •  {market_fa}  •  ⏱ {tf}\n"
        "\n"
        f"• ورود: <code>{entry}</code>\n"
        f"• خروج: <code>{exit_}</code>\n"
        f"• 🎯 هدف: <code>{tp}</code>\n"
        f"• 🛑 ضرر: <code>{sl}</code>\n"
        "\n"
        f"• نتیجه: {result}\n"
        f"• 💰 مارجین: {_fmt_size(row['size'])}\n"
        f"• ⚡ اهرم: {lev}\n"
        f"• ⚠️ ریسک: {risk}\n"
        "\n"
        f"• 📅 تاریخ: {_ESC(row['trade_date'])}\n"
        f"• 🧠 حالت: {_ESC(mood)}\n"
        f"• 💭 دلیل: {notes}\n"
        f"• 📸 عکس: {shots_txt}\n"
        "\n"
        f"💵 سود و زیان: <b>{_fmt_pnl(row['pnl']) if row['pnl'] is not None else '—'}</b>\n"
        f"📊 بازدهی (ROI): <b>{_fmt_roi(roi)}</b>"
    )


def _recent_panel_kb(rows: list, page: int, pages: int) -> InlineKeyboardMarkup:
    """Inline keyboard: one button per trade + range filter + pager + home."""

    def _rbtn(token: str) -> InlineKeyboardButton:
        label = _RECENT_RANGES[token][0]
        return InlineKeyboardButton(
            (f"✓ {label}") if token == _recent_range else label,
            callback_data=_RCB_RANGE + token,
        )

    trade_rows = [
        [
            InlineKeyboardButton(
                _recent_button(row), callback_data=_RCB_VIEW + str(row["id"])
            )
        ]
        for row in rows
    ]
    prev_cb = _RCB_PAGE + str(page - 1) if page > 1 else _RCB_NOOP
    next_cb = _RCB_PAGE + str(page + 1) if page < pages else _RCB_NOOP
    return InlineKeyboardMarkup(
        trade_rows
        + [
            [_rbtn(token) for token in _RECENT_RANGE_ORDER],
            [
                InlineKeyboardButton("◀️", callback_data=prev_cb),
                InlineKeyboardButton(
                    f"{_fa_num(page)} / {_fa_num(pages)}",
                    callback_data=_RCB_NOOP,
                ),
                InlineKeyboardButton("▶️", callback_data=next_cb),
            ],
            [InlineKeyboardButton("🏠 Home", callback_data=_RCB_HOME)],
        ]
    )


def _recent_detail_kb(
    trade_id: int, has_shots: bool = False
) -> InlineKeyboardMarkup:
    """Buttons on a sent detail message: 📷 (with shots), 🗑 delete, ❌ close."""
    rows = []
    if has_shots:
        rows.append(
            [InlineKeyboardButton("📷 عکس چارت", callback_data=_RCB_PHOTO + str(trade_id))]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🗑 حذف", callback_data=_RCB_DEL + str(trade_id)
            ),
            InlineKeyboardButton("❌ بستن", callback_data=_RCB_CLOSE),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_trade_photos(update: Update, trade_id: int) -> None:
    """Send the screenshots attached to a trade (📷 button on the detail)."""
    row = db.get_trade(trade_id)
    if row is None:
        await update.effective_chat.send_message(
            f"معامله‌ای با شماره #{trade_id} پیدا نشد."
        )
        return
    sent_any = False
    for key, label in (("screenshot", "قبل از معامله"), ("screenshot_after", "بعد از معامله")):
        if not row[key]:
            continue
        path = _screenshot_path(row[key])
        if not path.is_file():
            await update.effective_chat.send_message(
                f"فایل اسکرین‌شات معامله #{trade_id} روی دیسک پیدا نشد."
            )
            continue
        with path.open("rb") as photo:
            await update.effective_chat.send_photo(
                photo,
                caption=f"#{trade_id} {row['symbol']} {row['trade_date']} — {label}",
            )
        sent_any = True
    if not sent_any:
        await update.effective_chat.send_message(
            f"معامله #{trade_id} اسکرین‌شات ندارد."
        )


async def delete_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    usage = "روش استفاده: /delete <شماره> (شماره‌ها در /recent)"
    if not context.args:
        await update.message.reply_text(usage)
        return
    try:
        trade_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(usage)
        return
    row = db.delete_trade(trade_id)
    if row is None:
        await update.message.reply_text(
            f"معامله‌ای با شماره #{trade_id} پیدا نشد."
        )
        return
    for key in ("screenshot", "screenshot_after"):
        if row[key]:
            try:
                _screenshot_path(row[key]).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove screenshot for deleted trade #%s", trade_id
                )
    logger.info("Deleted trade #%s", trade_id)
    pnl_txt = _fmt_pnl(row["pnl"]) if row["pnl"] is not None else "—"
    await update.message.reply_text(
        f"🗑 حذف شد: #{row['id']} {row['trade_date']} {row['symbol']} "
        f"{_DIR_LABEL.get(row['direction'], row['direction'].upper())} — "
        f"P&L {pnl_txt}"
    )
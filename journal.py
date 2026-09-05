"""Guided /trade conversation (inline-button questions) plus the open-trades
flow, the /recent, /open, /stats and /delete commands.

Every choice is offered as an INLINE BUTTON attached to the bot's own message —
each question message is deleted the moment it is answered, so the chat reads
like a clean step-by-step menu. Typed answers still work everywhere. The main
menu (sent by /start) is a welcome message with inline buttons that stays in
place; every secondary screen (⚙️ settings, 💰 budget, stats/recent/open
panels and the stats symbol picker) is ONE morphing message per chat that is
EDITED in place as the trader navigates — 🔙 goes one level back, 🏠 returns
straight to the main menu. A chart screenshot can be attached near the end of
the questionnaire and is later reachable through the 📷 button on the trade's
detail card in /recent.

P&L and ROI are the trader's typed numbers: the result question (✅ Win /
❌ Loss / ➖ BE) only signs them. Margin and leverage are optional (⏭ skip)
info-only fields — the bot computes nothing.

Open trades work in two phases: the 🟢 open-trades questionnaire (market,
symbol, side, timeframe, reason, screenshot, date, time, risk, margin,
entry, TP, SL) stores a running position in db.open_trades; when it closes,
the trader taps it in the 🟢 panel and fills a second short questionnaire
(status, dollar result, exit date, time, price, up to 4 exit screenshots,
reason, mood) which moves it into the normal closed-trades history.
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
    Message,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction, KeyboardButtonStyle, ParseMode
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
    TIMEFRAME,
    ENTRY,
    TAKE_PROFIT,
    STOP_LOSS,
    RESULT,
    PNL_AMOUNT,
    PNL_ROI,
    MARGIN,
    RISK,
    TRADE_DATE,
    TRADE_HOUR,
    NOTES,
    MOOD,
    SCREENSHOT,
    SCREENSHOT_AFTER,
    CONFIRM,
) = range(19)

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
    OPEN_MARGIN,
    OPEN_LEVERAGE,
    OPEN_ENTRY,
    OPEN_TAKE_PROFIT,
    OPEN_STOP_LOSS,
    OPEN_CONFIRM,
) = range(100, 115)

# States of the close-an-open-trade questionnaire (started from the 🏁 button
# on an open trade's detail card — the open trade id travels in user_data).
(
    CLOSE_STATUS,
    CLOSE_AMOUNT,
    CLOSE_ROI,
    CLOSE_DATE,
    CLOSE_HOUR,
    CLOSE_PRICE,
    CLOSE_PHOTOS,
    CLOSE_REASON,
    CLOSE_MOOD,
    CLOSE_CONFIRM,
) = range(200, 210)

_TEXT = filters.TEXT & ~filters.COMMAND
_CANCEL_RE = re.compile(
    r"^\s*(?:/cancel|cancel|لغو|انصراف|✖️\s*(?:cancel|لغو|انصراف))\s*$",
    re.IGNORECASE,
)
# Menu labels (inline buttons and their old reply-bar spellings). A bare text
# like this is never a flow answer (notes/symbols are free text, so a typed
# "stats" cannot be a note) — it is excluded from the answer filter.
_MENU_RE = re.compile(
    r"^\s*(?:📈\s*"
    r"(?:بستن\s*معامله|ثبت\s*معامله\s*بسته|close\s*trade|new\s*trade|معامله\s*جدید)"
    r"|🟢\s*(?:ثبت\s*معامله\s*باز|open\s*trades|معاملات\s*باز|باز)"
    r"|📊\s*(?:stats|آمار)"
    r"|⚙️\s*(?:settings|تنظیمات)"
    r"|🕘\s*(?:recent|معاملات\s*اخیر|اخیر)"
    r"|📥\s*(?:export|اکسل)"
    r"|🏠\s*(?:menu|منو|home|خانه)"
    r"|❓\s*(?:help|راهنما))\s*$",
    re.IGNORECASE,
)
_ANSWER = _TEXT & ~filters.Regex(_CANCEL_RE) & ~filters.Regex(_MENU_RE)
# --------------------------------------------------------------------------
# Inline UI primitives — every screen and question is a message with inline
# buttons; the reply-keyboard bar is gone. Questions are deleted right after
# they are answered; screens drill down (delete current, send next) while the
# main menu message stays in place.
# --------------------------------------------------------------------------

def _ik(rows: list[list]) -> InlineKeyboardMarkup:
    """Inline keyboard from rows of labels or buttons.

    Plain labels become buttons whose callback data is the label prefixed
    with "q:" (the question-answer namespace the conversation states match);
    InlineKeyboardButton objects pass through untouched.
    """
    return InlineKeyboardMarkup(
        [
            [
                (
                    InlineKeyboardButton(label, callback_data="q:" + label)
                    if isinstance(label, str)
                    else label
                )
                for label in row
            ]
            for row in rows
        ]
    )


_CANCEL_IK_ROW = [InlineKeyboardButton("✖️ لغو", callback_data="q:cancel")]
_Q_CANCEL_CB_RE = re.compile(r"^q:cancel$")
# A question answer is a q: tap that is NOT q:cancel (the cancel row goes to
# the conversation fallback, not to the step parser).
_Q_CB_RE = re.compile(r"^q:(?!cancel)")
_BACK_FLOW_ROW = [InlineKeyboardButton("🔙 بازگشت", callback_data="nav:back:flow")]
# Screen navigation: 🔙 = one level back, 🏠 = straight to the main menu.
_BACK_NAV_ROW = [
    InlineKeyboardButton("🔙", callback_data="nav:back"),
    InlineKeyboardButton("🏠", callback_data="nav:home"),
]
_HOME_NAV_ROW = [InlineKeyboardButton("🏠", callback_data="nav:home")]
_NAV_CB_RE = re.compile(r"^nav:(?:back|home)$")

_TF_BUTTONS = [["1m", "5m", "15m"], ["30m", "1h", "4h"], ["1D", "1W", "1M"]]


_SHOT_KEYBOARD = _ik([["⏭ بدون اسکرین‌شات"], _CANCEL_IK_ROW])


# ---------------------------------------------------------------------------
# Morphing-screen navigation
#
# The main menu is one persistent message. Every secondary screen (⚙️ settings,
# 💰 budget, 📊 stats, 🕘 recent, 🟢 open trades) is a SINGLE message per chat
# that is EDITED in place when the trader drills into a sub-screen and back —
# nothing is deleted and re-sent while navigating. `_nav` (user_data) holds the
# content stack so 🔙 can re-render exactly the previous level, 🏠 jumps
# straight back to the main menu, and the main-menu message never moves.
# ---------------------------------------------------------------------------

_MAIN_NAV_KEY = "main"
_SCREEN_NAV_KEY = "screen"


def _nav_stack(context: ContextTypes.DEFAULT_TYPE) -> list:
    return context.user_data.setdefault("_nav", [])


def _nav_update_top(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    """Keep the nav stack's top entry in sync after an in-place edit."""
    nav = _nav_stack(context)
    if not nav or nav[-1].get("key") != key:
        nav[:] = [e for e in nav if e.get("key") != key]
        nav.append({"key": key, "text": text, "kb": markup})
    else:
        nav[-1]["text"] = text
        nav[-1]["kb"] = markup


def _nav_prune(context: ContextTypes.DEFAULT_TYPE, *keys: str) -> None:
    """Drop the given keys from the nav stack (they are no longer shown)."""
    _nav_stack(context)[:] = [
        e for e in _nav_stack(context) if e.get("key") not in keys
    ]


def _screen_msg_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """The message id of the current morphing screen (None if not sent)."""
    return (context.user_data.get("_screens") or {}).get(_SCREEN_NAV_KEY)


async def _edit_or_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    key: str,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    """Edit the morphing screen message in place (send it if it is gone)."""
    screens = context.user_data.setdefault("_screens", {})
    mid = screens.get(key)
    if mid:
        try:
            await context.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=mid,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            logger.info("%s screen message is gone; sending a fresh one.", key)
    screens.pop(key, None)
    msg = await context.bot.send_message(
        chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
    )
    screens[key] = msg.message_id


async def _drop_screen_message(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str
) -> None:
    """Forget and delete a screen message (best-effort)."""
    screens = context.user_data.setdefault("_screens", {})
    old = screens.pop(key, None)
    if old:
        try:
            await context.bot.delete_message(chat_id, old)
        except Exception:
            logger.info("%s screen message could not be deleted.", key)


async def _show_screen(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    key: str,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    """Show a screen. The main menu owns its own message; every other
    screen morphs the single secondary-screen message (edit in place)."""
    if key == _MAIN_NAV_KEY:
        screens = context.user_data.setdefault("_screens", {})
        old = screens.pop(key, None)
        if old:
            try:
                await context.bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=old,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
                screens[key] = old
                return
            except Exception:
                logger.info("Stale main-menu message could not be edited.")
        msg = await context.bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
        screens[key] = msg.message_id
        return
    # A secondary screen: push it onto the content stack and morph.
    nav = _nav_stack(context)
    nav[:] = [entry for entry in nav if entry.get("key") != key]
    nav.append({"key": key, "text": text, "kb": markup})
    await _edit_or_send(context, chat_id, _SCREEN_NAV_KEY, text, markup)


async def _screen_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🔙 — go exactly one level back (re-render the previous screen content).

    The morphing message is edited in place; when the stack empties the
    message is removed and the (persistent) main menu is refreshed.
    """
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    nav = _nav_stack(context)
    if nav:
        nav.pop()
    if nav:
        top = nav[-1]
        await _edit_or_send(
            context, chat_id, _SCREEN_NAV_KEY, top["text"], top["kb"]
        )
        return
    await _drop_screen_message(context, chat_id, _SCREEN_NAV_KEY)
    await _ensure_menu(update, context)


async def _screen_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🏠 — drop every secondary screen and re-show the main menu."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _nav_stack(context).clear()
    await _drop_screen_message(context, chat_id, _SCREEN_NAV_KEY)
    await _ensure_menu(update, context)


async def on_nav_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """🔙 / 🏠 taps coming from any screen message."""
    data = update.callback_query.data or ""
    if data == "nav:home":
        await _screen_home(update, context)
    else:
        await _screen_back(update, context)


def build_nav_callbacks() -> CallbackQueryHandler:
    """Handler for the 🔙 / 🏠 buttons of every screen message."""
    return CallbackQueryHandler(on_nav_callback, pattern=_NAV_CB_RE)


def _reset_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the flow draft but keep the screen bookkeeping (_screens, _nav)."""
    screens = context.user_data.get("_screens")
    nav = context.user_data.get("_nav")
    context.user_data.clear()
    if screens is not None:
        context.user_data["_screens"] = screens
    if nav is not None:
        context.user_data["_nav"] = nav


def _cb_text_update(update: Update, text: str) -> Update:
    """A real text-message Update built from a callback tap.

    Lets the existing message-based handlers (panels, flow starts) run
    unchanged when their button now lives on an inline keyboard.
    """
    user = update.effective_user
    chat = update.effective_chat
    query = update.callback_query
    message_id = query.message.message_id if query.message is not None else 1
    msg = Message(
        message_id=message_id,
        date=datetime.now(),
        chat=chat,
        from_user=user,
        text=text,
    )
    bot = update.get_bot()
    msg.set_bot(bot)
    synthesized = Update(update_id=1, message=msg)
    synthesized.set_bot(bot)
    return synthesized


def _cb_to(fn):
    """Adapt an inline tap to a message-based handler (answer + synthesize)."""

    async def _wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        label = query.data or ""
        await query.answer()
        return await fn(_cb_text_update(update, label), context)

    return _wrapped


async def _q_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Send a flow question (with its inline buttons) and remember its id.

    The question is deleted as soon as it is answered (see _q_drop), so the
    chat reads like a clean step-by-step menu instead of a wall of prompts.
    """
    msg = await update.effective_chat.send_message(
        text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )
    if msg is not None and getattr(msg, "message_id", None):
        context.user_data["_flow_q"] = msg.message_id


async def _q_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the current flow question (best-effort)."""
    mid = context.user_data.pop("_flow_q", None)
    chat = update.effective_chat
    if mid is not None and chat is not None and context.bot is not None:
        try:
            await context.bot.delete_message(chat.id, mid)
        except Exception:
            logger.info("Flow question could not be deleted.")


def _msg_step(fn):
    """Typed-answer wrapper: drop the question, then run the step parser."""

    async def _wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _q_drop(update, context)
        return await fn(update, context)

    return _wrapped


def _tap_step(fn):
    """Inline-tap wrapper: answer, drop the question, run the step parser."""

    async def _wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        label = query.data or ""
        # Question buttons carry the "q:" namespace prefix (see _ik) — the
        # step parsers must see the plain label, exactly like a typed answer.
        if label.startswith("q:"):
            label = label[2:]
        await query.answer()
        await _q_drop(update, context)
        return await fn(_cb_text_update(update, label), context)

    return _wrapped


def _build_keyboards() -> None:
    """(Re)build the inline keyboards (import time, after the primitives)."""
    global _DIR_KEYBOARD, _TF_KEYBOARD, _CONFIRM_KEYBOARD, _STATUS_KEYBOARD
    _DIR_KEYBOARD = _ik([["📈 Long", "📉 Short"], _CANCEL_IK_ROW])
    _TF_KEYBOARD = _ik([*_TF_BUTTONS, _CANCEL_IK_ROW])
    _CONFIRM_KEYBOARD = _ik([["✅ ذخیره", "❌ ثبت نشود"], _CANCEL_IK_ROW])
    _STATUS_KEYBOARD = _ik(
        [
            ["✅ Win", "❌ Loss"],
            ["➖ BE"],
            _CANCEL_IK_ROW,
        ]
    )


_build_keyboards()


# --------------------------------------------------------------------------
# Main menu — a welcome MESSAGE with inline buttons (sent by /start).
# --------------------------------------------------------------------------
MENU_TEXT = (
    "📈 ثبت معامله بسته — بعد از خروج از معامله\n"
    "🟢 ثبت معامله باز — معامله‌ای که همین الان در آن هستی\n"
    "🟢 معاملات باز — دیدن، بستن یا حذف معامله‌های جاری\n"
    "🕘 معاملات اخیر — با جزئیات کامل هر معامله\n"
    "📊 آمار — عملکرد کلی با فیلتر بازه و نماد\n"
    "⚙️ تنظیمات — بودجهٔ حساب (USD)\n\n"
    "در هر صفحه: 🔙 یک مرحله عقب، 🏠 بازگشت به همین منو. "
    "به‌جای دکمه‌ها می‌توانی جواب را تایپ کنی."
)

# The main menu is a MESSAGE with inline buttons (no reply bar anymore).
# Every button carries a Telegram button style (Bot API 10.0 / PTB 22.7+):
# a stable two-column pattern — left column blue (primary), right column
# green (success), ⚙️ settings red (danger). Old Telegram clients simply
# ignore the style and show plain buttons.
_MENU_IK = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "📈 ثبت معامله بسته",
                callback_data="menu:trade",
                style=KeyboardButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                "🟢 ثبت معامله باز",
                callback_data="menu:open",
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                "🟢 معاملات باز",
                callback_data="menu:opens",
                style=KeyboardButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="menu:stats",
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                "🕘 معاملات اخیر",
                callback_data="menu:recent",
                style=KeyboardButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                "📥 اکسل",
                callback_data="menu:export",
                style=KeyboardButtonStyle.SUCCESS,
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ تنظیمات",
                callback_data="menu:settings",
                style=KeyboardButtonStyle.DANGER,
            )
        ],
    ]
)

_MAIN_MENU_KEY = "main"
_CB_MENU = "menu:"  # callback-data prefix of every main-menu button


async def show_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/settings — the settings screen (alias used by bot.py)."""
    await _send_settings_screen(update, context)

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
_SKIP_RISK_TOKENS = _SKIP_TOKENS | {"بدون درصد", "⏭ بدون درصد"}
# The margin question is skippable (the trader may not want to record it).
_SKIP_MARGIN_BTN = "⏭ رد کردن"
_SKIP_MARGIN_TOKENS = _SKIP_TOKENS | {_SKIP_MARGIN_BTN}
# Leverage (open questionnaire, بخش اول): typed like 10, 10x or ×10 — stored
# as info-only; the bot never uses it in any calculation.
_LEV_RE = re.compile(r"^\s*[×xX]?\s*(\d{1,3})(?:\.\d+)?\s*[xX×]?\s*$")
_SKIP_LEVERAGE_BTN = "⏭ بدون اهرم"
_LEVERAGE_SKIP_TOKENS = _SKIP_TOKENS | {_SKIP_LEVERAGE_BTN}
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
        # The plain inline-button labels (no TP/SL mention — it is just info).
        "✅ win": "win",
        "❌ loss": "loss",
        "➖ be": "be",
    }
)
# Stored value -> detail-card emoji/label (manual uses ➖ in stats roll-ups).
_OPEN_EMOJI = {
    "win": "🟢",
    "loss": "🔴",
    "be": "⚪",
    "manual": "✏️",
}
_OPEN_STATUS_LABELS = {"win": "Win", "loss": "Loss", "be": "Breakeven", "manual": "Manual exit"}
# Maximum number of exit screenshots per close (spec: 4).
_MAX_EXIT_PHOTOS = 4

# Chart screenshots are stored here; override with the SCREENSHOT_DIR env var.
SCREENSHOT_DIR = Path(
    os.getenv(
        "SCREENSHOT_DIR", str(Path(__file__).resolve().parent / "screenshots")
    )
)


# --------------------------------------------------------------------------
# Stale reply-bar removal — the old UI's persistent keyboard keeps living on
# the user's client until a message asks Telegram to remove it.
# --------------------------------------------------------------------------

_MENU_KILLER = ReplyKeyboardRemove()


_CANCEL_ROW = ["✖️ لغو"]
_MARKET_KEYBOARD = _ik([["🪙 Crypto", "💵 فارکس"], _CANCEL_IK_ROW])
_RESULT_KEYBOARD = _ik([["✅ Win", "❌ Loss", "➖ BE"], _CANCEL_IK_ROW])
_RISK_KEYBOARD = _ik([["0.5%", "1%", "2%"], ["3%", "5%", "10%"], ["⏭ بدون درصد"], _CANCEL_IK_ROW])
# The margin questions are info-only: type a USD amount or skip it — the bot
# never calculates, suggests or warns about the margin.
_OPEN_MARGIN_KEYBOARD = _ik(
    [[_SKIP_MARGIN_BTN], _CANCEL_IK_ROW]
)


def _symbol_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Inline keyboard offering the most used and recently traded symbols."""
    recent, top = db.get_symbol_suggestions()
    rows: list[list] = []
    shown: set[str] = set()
    if top:
        rows.append(top)
        shown.update(top)
    extra = [symbol for symbol in recent if symbol not in shown]
    if extra:
        rows.append(extra)
    if not rows:
        return None
    rows.append(list(_CANCEL_IK_ROW))
    return _ik(rows)
_DATE_KEYBOARD = _ik([["📅 امروز"], _CANCEL_IK_ROW])
_OPEN_CONFIRM_KEYBOARD = _ik([["✅ ثبت", "❌ ثبت نشود"], _CANCEL_IK_ROW])
_HOUR_KEYBOARD = _ik([["00", "03", "06", "09"], ["12", "15", "18", "21"], ["🕐 الان", "⏭ رد کردن"], _CANCEL_IK_ROW])

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
_MOOD_KEYBOARD = _ik(
    [
        list(_MOODS)[i : i + 2] for i in range(0, len(_MOODS), 2)
    ]
    + [["⏭ رد کردن"], _CANCEL_IK_ROW]
)
_NOTES_KEYBOARD = _ik([["⏭ بدون دلیل"], _CANCEL_IK_ROW])


async def show_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send the welcome text and the inline main menu (also /start).

    Tracked as the "main" screen so a re-send EDITS the same menu message
    instead of stacking duplicate menus.
    """
    user = getattr(update, "effective_user", None)
    name = (getattr(user, "first_name", "") or "").strip()
    hello = (
        f"سلام {html.escape(name)}! 👋\n\n" if name else "سلام! 👋\n\n"
    )
    # The old UI's persistent reply-keyboard bar keeps living on the user's
    # client until a message asks Telegram to remove it. One silent message
    # per session does that — sent and instantly deleted, so nothing shows.
    if not context.user_data.get("_reply_bar_cleared"):
        context.user_data["_reply_bar_cleared"] = True
        chat_id = update.effective_chat.id
        try:
            killer = await update.effective_chat.send_message(
                "…", reply_markup=_MENU_KILLER
            )
            await context.bot.delete_message(chat_id, killer.message_id)
        except Exception:
            logger.info("Stale reply bar could not be removed.")
    await _show_screen(
        context,
        update.effective_chat.id,
        _MAIN_MENU_KEY,
        hello + MENU_TEXT,
        _MENU_IK,
    )


_MAIN_MENU_KEY = "main"
_CB_MENU = "menu:"  # callback-data prefix of every main-menu button


async def on_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dispatch the inline main-menu taps (the menu message itself stays)."""
    query = update.callback_query
    action = (query.data or "")[len(_CB_MENU):]
    await query.answer()
    synth = _cb_text_update(update, f"{_CB_MENU}{action}")
    if action == "trade":
        await trade_start(synth, context)
    elif action == "open":
        await open_trade_start(synth, context)
    elif action == "opens":
        await open_trades(synth, context)
    elif action == "stats":
        await stats(synth, context)
    elif action == "recent":
        await recent(synth, context)
    elif action == "export":
        await export_trades(synth, context)
    elif action == "settings":
        await _send_settings_screen(update, context)


async def _ensure_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the main-menu message again (back navigation needs it)."""
    await show_menu(update, context)


def build_menu_callbacks() -> CallbackQueryHandler:
    """Handler for the inline main-menu buttons."""
    return CallbackQueryHandler(on_menu_callback, pattern="^" + re.escape(_CB_MENU))


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
        )
    path.unlink(missing_ok=True)  # sent; don't leave copies on disk


# --------------------------------------------------------------------------
# Settings (⚙️ تنظیمات / /settings) — currently the account budget in USD.
# The budget still moves by every closed trade's typed P&L (budget feature);
# the margin auto-calculation is gone — margin is typed or skipped.
# --------------------------------------------------------------------------
_BUDGET_RE = re.compile(r"^\s*💰\s*(?:budget|بودجه)\s*$", re.IGNORECASE)
_BUDGET_VALUE_RE = re.compile(
    r"^\s*(?:"
    r"(?:⚙️\s*)?budget\s*(?::|=\s*|\s)\s*(?P<explicit>\d+(?:\.\d+)?)"
    r"\s*(?:usd|دلار)?"
    r"|(?P<bare>\d+(?:\.\d+)?)"
    r"|(?P<clear>حذف|remove|clear|هیچ|-)"
    r")\s*$",
    re.IGNORECASE,
)


async def _send_settings_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """⚙️ تنظیمات screen — a first-level screen (the main menu stays)."""
    budget = db.get_budget()
    if budget:
        body = (
            f"• 💰 بودجهٔ حساب: <b>{_fmt_num(budget)} $</b>\n\n"
            "با هر معاملهٔ بسته‌شده، بودجه به اندازهٔ سود یا ضرر ثبت‌شده "
            "کم یا زیاد می‌شود."
        )
    else:
        body = (
            "• 💰 بودجهٔ حساب: <b>—</b> (هنوز تنظیم نشده)\n\n"
            "اگر بودجه را وارد کنید، با هر معاملهٔ بسته‌شده به اندازهٔ "
            "سود یا ضرر جابه‌جا می‌شود."
        )
    await _show_screen(
        context,
        update.effective_chat.id,
        "settings",
        "⚙️ <b>تنظیمات</b>\n\n" + body,
        _ik([["💰 بودجه"], _BACK_NAV_ROW]),
    )


async def _send_budget_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """💰 بودجه screen — drills into settings (morphs the same message)."""
    budget = db.get_budget()
    current = f"{_fmt_num(budget)} $" if budget else "—"
    # Arm the free-number listener (expires, see _budget_armed below): the
    # next bare number sent soon after this prompt becomes the budget.
    context.user_data["_budget_prompt"] = datetime.now().timestamp()
    await _show_screen(
        context,
        update.effective_chat.id,
        "budget",
        f"💰 <b>بودجهٔ فعلی: {current}</b>\n\n"
        "عدد بودجه را به دلار بفرستید (مثلاً 500 یا 1250.50) "
        "یا «حذف» برای پاک کردن:",
        _ik([_BACK_NAV_ROW]),
    )


async def on_settings_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Inline taps of the settings screens (💰 بودجه)."""
    query = update.callback_query
    await query.answer()
    if (query.data or "") == "q:💰 بودجه":
        await _send_budget_screen(update, context)


def build_settings_handlers() -> list:
    """Handlers for the settings screens (inline taps + the typed value)."""
    return [
        CallbackQueryHandler(on_settings_callback, pattern=r"^q:💰 بودجه$"),
        MessageHandler(filters.Regex(_BUDGET_VALUE_RE), settings_budget_value),
    ]


def _budget_armed(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True while the 💰 prompt is fresh (10 minutes)."""
    armed_ts = context.user_data.get("_budget_prompt")
    return isinstance(armed_ts, (int, float)) and (
        datetime.now().timestamp() - armed_ts
    ) < 600


async def settings_budget_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Store the budget typed after the 💰 prompt (or via /settings budget N)."""
    raw = (update.message.text or "").strip()
    # Persian keyboards send ۰-۹ (and some Androids ٠-٩) — normalize first,
    # otherwise "۵۰۰" would silently match nothing.
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    )
    m = _BUDGET_VALUE_RE.match(raw)
    if m is None:  # not a budget answer (the filter should prevent this)
        return
    armed = _budget_armed(context)
    if m.group("clear") is not None:
        if not armed:
            return  # "حذف" outside the budget prompt means nothing
        context.user_data.pop("_budget_prompt", None)
        db.set_budget(None)
        await _back_to_settings(update, context)
        return
    if m.group("explicit"):
        number = _parse_positive(m.group("explicit"))
    elif armed:
        number = _parse_positive(m.group("bare"))
    else:
        # A bare number outside the budget prompt is ignored — storing a
        # budget from it would be a surprising side effect.
        return
    if number is None:  # unreachable with the current regex; keep the guard
        await update.message.reply_text(
            "عدد نامعتبر — مثلاً 500 یا 1250.50 بفرستید (دلار):"
        )
        return
    context.user_data.pop("_budget_prompt", None)
    db.set_budget(number)
    logger.info("Budget set to %.2f USD", number)
    await _back_to_settings(update, context)


async def _back_to_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """After a 💰 answer: morph the screen back into the ⚙️ settings view."""
    nav = _nav_stack(context)
    nav[:] = [e for e in nav if e.get("key") not in ("budget", "settings")]
    await _send_settings_screen(update, context)


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


def _fmt_pnl(value: Optional[float]) -> str:
    """Signed dollar amount, e.g. +$12.50 or -$3.00; '—' when unknown (NULL).

    Two-phase (open → close) trades have no margin question, so their P&L is
    NULL by design — stats sums/maxima then come back as NULL too.
    """
    if value is None:
        return "—"
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


def _signed_pnl(data: dict) -> float:
    """The signed P&L of a draft: ➖ for a loss, 0 for BE, ➕ for a win.

    The amount is the trader's own input (data["pnl_amount"]); the result
    question only decides its sign. No math anywhere — what the trader types
    is exactly what gets stored.
    """
    amount = data.get("pnl_amount") or 0.0
    hit = data.get("hit")
    if hit == "lose":
        return -abs(amount)
    if hit == "be":
        return 0.0
    return abs(amount)


def _signed_roi(value: Optional[float], hit: Optional[str]) -> Optional[float]:
    """The trader's typed ROI percent, signed by the result (None if skipped).

    Shared by /trade (data["pnl_roi"]) and the close flow (data["close_roi"]):
    the percent is only signed, never computed from anything.
    """
    if value is None:
        return None
    if hit == "be":
        return 0.0
    if hit in ("lose", "loss"):
        return -abs(value)
    return abs(value)


def _close_roi_signed(data: dict) -> Optional[float]:
    """The signed typed ROI of a close draft (0 for BE, None when skipped)."""
    return _signed_roi(data.get("close_roi"), data.get("hit"))


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

async def _prompt_direction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "جهت معامله؟", reply_markup=_DIR_KEYBOARD
    )
    return DIRECTION


async def _prompt_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "تایم‌فریم (Timeframe):",
        reply_markup=_ik(_TF_BUTTONS + [_CANCEL_IK_ROW]),
    )
    return TIMEFRAME


async def _prompt_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "Entry price:"
    )
    return ENTRY


async def _prompt_take_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "🎯 Take Profit (TP):"
    )
    return TAKE_PROFIT


async def _prompt_stop_loss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "🛑 Stop Loss (SL):"
    )
    return STOP_LOSS


async def _prompt_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "نتیجه معامله؟",
        reply_markup=_ik([["✅ Win", "❌ Loss", "➖ BE"], _CANCEL_IK_ROW]),
    )
    return RESULT


async def _prompt_pnl_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The dollar amount the trader actually gained or lost — saved as-is."""
    hit = context.user_data.get("hit")
    if hit == "win":
        hint = "چند دلار سود کردی؟ (فقط عدد، مثلاً 120.50)"
    elif hit == "lose":
        hint = "چند دلار ضرر کردی؟ (فقط عدد، مثلاً 80)"
    else:
        hint = "چند دلار سود یا ضرر کردی؟ (BE معمولاً 0 است)"
    await _q_send(update, context, f"💵 {hint}")
    return PNL_AMOUNT


async def _prompt_margin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if (context.user_data.get("market") or "crypto") == "forex":
        detail = "USD (حساب فارکس)"
    else:
        detail = "USDT"
    await _q_send(update, context,
        f"💰 Margin ({detail}) — دکمهٔ ⏭ رد کردن را بزنید تا خالی بماند:",
        reply_markup=_ik([[_SKIP_MARGIN_BTN], _CANCEL_IK_ROW]),
    )
    return MARGIN


async def _prompt_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "⚠️ Risk — چند درصد از حساب؟ (مثلاً 1 یا 1%)",
        reply_markup=_ik([["0.5%", "1%", "2%"], ["3%", "5%", "10%"], ["⏭ بدون درصد"], _CANCEL_IK_ROW]),
    )
    return RISK


async def _prompt_trade_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "تاریخ بستن معامله:\n"
        "YYYY-MM-DD  (e.g. 2026-02-09)",
        reply_markup=_ik([["📅 امروز"], _CANCEL_IK_ROW]),
    )
    return TRADE_DATE


async def _prompt_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "📝 دلیل ورود (دقیقاً چرا وارد شدی؟):",
        reply_markup=_ik([["⏭ بدون دلیل"], _CANCEL_IK_ROW]),
    )
    return NOTES


async def _prompt_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "📸 اسکرین‌شات چارت — قبل از ورود (اختیاری):",
        reply_markup=_ik([["⏭ بدون اسکرین‌شات"], _CANCEL_IK_ROW]),
    )
    return SCREENSHOT


async def _prompt_screenshot_after(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await _q_send(update, context,
        "📸 اسکرین‌شات چارت — بعد از معامله (اختیاری):",
        reply_markup=_ik([["⏭ بدون اسکرین‌شات"], _CANCEL_IK_ROW]),
    )
    return SCREENSHOT_AFTER


_RESULT_EMOJI = {"win": "🟢", "lose": "🔴", "be": "⚪"}


def _result_emoji(hit: Optional[str]) -> str:
    """🟢 win · 🔴 loss · ⚪ breakeven · ➖ unknown/legacy."""
    return _RESULT_EMOJI.get(hit or "", "➖")


def _summary(data: dict) -> str:
    """Render the airy confirmation summary for the current draft (HTML)."""
    pnl = _signed_pnl(data)
    hit = data.get("hit") or ""
    roi = _signed_roi(data.get("pnl_roi"), hit)
    emoji = _result_emoji(hit)
    market = data.get("market") or "crypto"
    market_fa = "🪙 Crypto" if market == "crypto" else "💵 فارکس"
    result = _RESULT_LABELS.get(hit, "-")
    risk = data.get("risk_percent")
    margin = data.get("size")
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
        f"• TF        {data.get('timeframe') or '-'}\n"
        f"• Entry     {_fmt_num(data['entry_price'])}   →   "
        f"{_fmt_num(data['exit_price']) if data['exit_price'] else '-'}\n"
        f"• TP / SL   {_fmt_num(data['take_profit']) if data.get('take_profit') else '-'}"
        f" / {_fmt_num(data['stop_loss']) if data.get('stop_loss') else '-'}\n"
        "\n"
        "◾ <i>نتیجه</i>\n"
        f"• Result    {emoji} {result}\n"
        f"• P&L       <b>{_fmt_pnl(pnl)}</b>"
        + (f" · ROI {_fmt_roi(roi)}" if roi is not None else "")
        + "\n"
        + (
            (
                "• Margin    " + _fmt_size(margin)
                + (f" · Risk {_fmt_num(risk)}%" if risk else "")
                + "\n"
            )
            if margin
            else (f"• Risk    {_fmt_num(risk)}%\n" if risk else "")
        )
        + f"• Date      {data['trade_date']}\n"
        + (f"• Mood      {_MOOD_LABELS.get(mood, mood)}\n" if mood else "")
        + (f"• Reason    {notes}\n" if data["notes"] else "")
        + (f"• Shots     {' و '.join(shots)}\n" if shots else "")
        + "\n"
        "ذخیره شود؟"
    )


async def _prompt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    summary = _summary(context.user_data)
    try:
        await _q_send(
            update, context, summary, reply_markup=_CONFIRM_KEYBOARD
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
    _reset_flow(context)
    await _drop_screen_message(context, update.effective_chat.id, "flow")
    pnl = _signed_pnl(data)
    roi = _signed_roi(data.get("pnl_roi"), data.get("hit"))
    trade_id = db.add_trade(
        symbol=data["symbol"],
        direction=data["direction"],
        timeframe=data.get("timeframe") or "",
        entry_price=data["entry_price"],
        exit_price=data["exit_price"],
        size=data.get("size"),
        pnl=pnl,
        trade_date=data["trade_date"],
        notes=data["notes"],
        mood=data.get("mood") or "",
        roi=roi,
        screenshot=data.get("screenshot"),
        market=data.get("market") or "crypto",
        risk_percent=data.get("risk_percent"),
        take_profit=data.get("take_profit"),
        stop_loss=data.get("stop_loss"),
        hit=data.get("hit") or "",
        screenshot_after=data.get("screenshot_after"),
    )
    logger.info("Saved trade #%s %s", trade_id, data["symbol"])
    tf = data.get("timeframe") or ""
    hit = data.get("hit") or ""
    result = _RESULT_LABELS.get(hit, "-")
    symbol = _ESC(data["symbol"])
    text = (
        f"{_result_emoji(hit)} <b>معامله #{trade_id} ذخیره شد</b>\n"
        "\n"
        f"• <b>{symbol}</b> · "
        f"{_DIR_LABEL.get(data['direction'], data['direction'])}"
        + (f" · {tf}" if tf else "")
        + "\n"
        f"• Entry: {_fmt_num(data['entry_price'])}"
        f" → {_fmt_num(data['exit_price'])}\n"
        f"• {result}\n"
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
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        # The trade IS saved — never skip the confirmation. Retry as plain
        # text (HTML tags stripped) and log the real cause for debugging.
        logger.error(
            "HTML save confirmation failed:\n%s", traceback.format_exc()
        )
        await update.message.reply_text(re.sub(r"</?[bi]>", "", text))
    return ConversationHandler.END


async def _discard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Drop the current draft and reply where the user interacted."""
    _drop_screenshot(context)
    _reset_flow(context)
    await update.message.reply_text("❌ ثبت نشد — چیزی ذخیره نشد.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# /trade conversation steps
# --------------------------------------------------------------------------

async def trade_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the guided entry with the market question (crypto vs forex).

    Any half-finished previous draft (and its dangling question message) is
    wiped first — flows always start from scratch, never resume.
    """
    await _q_drop(update, context)  # remove a dangling question, if any
    _drop_screenshot(context)
    _reset_flow(context)
    await _q_send(
        update,
        context,
        "ثبت معاملهٔ بسته‌شده — در کدام بازار معامله کردی؟ (🪙 Crypto / 💵 فارکس)",
        _ik([["🪙 Crypto", "💵 فارکس"], _CANCEL_IK_ROW]),
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
        await _q_send(
            update,
            context,
            "یکی از دو دکمه را بزنید: 🪙 Crypto یا 💵 فارکس",
            reply_markup=_MARKET_KEYBOARD,
        )
        return MARKET
    return await _prompt_symbol(update, context)


async def _prompt_symbol(update: Update, context) -> int:
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
    await _q_send(update, context, text, reply_markup=symbol_kb)
    return SYMBOL


async def ask_symbol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    symbol = (update.message.text or "").strip().upper()
    if not symbol or len(symbol) > 24 or any(ch.isspace() for ch in symbol):
        await update.message.reply_text(
            "این شبیه نماد نیست — یکی از دکمه‌های زیر را بزنید یا دوباره "
            "تایپ کنید (مثلاً EURUSD):",
            reply_markup=_symbol_keyboard(),
        )
        return SYMBOL
    context.user_data["symbol"] = symbol
    return await _prompt_direction(update, context)


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
    return await _prompt_timeframe(update, context)


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
    return await _prompt_entry(update, context)


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
    return await _prompt_take_profit(update, context)


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
    return await _prompt_stop_loss(update, context)


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
    return await _prompt_result(update, context)


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
    return await _prompt_pnl_amount(update, context)


async def ask_pnl_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the dollar amount the trader actually gained or lost."""
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "مبلغ نامعتبر — فقط عدد بفرستید (مثلاً 120.50):"
        )
        return PNL_AMOUNT
    context.user_data["pnl_amount"] = number
    return await _prompt_pnl_roi(update, context)


async def _prompt_pnl_roi(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The typed ROI percent (بخش دوم) — stored as-is, never computed."""
    await _q_send(update, context,
        "📊 مقدار سود یا ضرر به درصد چقدر بود؟ (مثلاً 2.5)\n"
        f"{_SKIP_MARGIN_BTN} یعنی بدون درصد:",
        reply_markup=_ik([[_SKIP_MARGIN_BTN], _CANCEL_IK_ROW]),
    )
    return PNL_ROI


async def ask_pnl_roi(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the typed ROI percent; the result question only signs it."""
    raw = (update.message.text or "").strip()
    if raw in _SKIP_MARGIN_TOKENS:
        context.user_data.pop("pnl_roi", None)
        return await _prompt_margin(update, context)
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2.5):"
        )
        return PNL_ROI
    context.user_data["pnl_roi"] = number
    return await _prompt_margin(update, context)


async def ask_margin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_MARGIN_TOKENS:
        # Skipped margin: the draft keeps no size (nothing to compute).
        context.user_data.pop("size", None)
        return await _prompt_risk(update, context)
    number = _parse_positive(raw)
    if number is None:
        await update.message.reply_text(
            "Margin نامعتبر — یک عدد مثبت بفرستید (اعشار با نقطه) "
            f"یا {_SKIP_MARGIN_BTN}:"
        )
        return MARGIN
    context.user_data["size"] = number  # 'size' column stores the margin (info-only)
    return await _prompt_risk(update, context)


async def ask_risk(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _SKIP_RISK_TOKENS:
        context.user_data.pop("risk_percent", None)
        return await _prompt_trade_date(update, context)
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد ریسک نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2 یا 2%):"
        )
        return RISK
    context.user_data["risk_percent"] = number
    return await _prompt_trade_date(update, context)


async def ask_trade_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS or raw.lower() in _TODAY_TOKENS:
        context.user_data["trade_date"] = date.today().isoformat()
        return await _prompt_trade_hour(update, context)
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
        return await _prompt_notes(update, context)
    context.user_data["trade_date"] = parsed.date().isoformat()
    return await _prompt_trade_hour(update, context)


async def _prompt_trade_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "ساعت بسته‌شدن (اختیاری):\n"
        "HH:MM  (e.g. 14:30) — یا «🕐 الان» برای همین حالا",
        reply_markup=_ik([["00", "03", "06", "09"], ["12", "15", "18", "21"], ["🕐 الان", "⏭ رد کردن"], _CANCEL_IK_ROW]),
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
        return await _prompt_notes(update, context)
    if raw.lower() in _SKIP_HOUR_TOKENS:
        return await _prompt_notes(update, context)
    match = _HOUR_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        await update.message.reply_text(
            "ساعت نامعتبر.\nHH یا HH:MM  (e.g. 14:30)"
        )
        return TRADE_HOUR
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    context.user_data["trade_date"] += f" {hour:02d}:{minute:02d}"
    return await _prompt_notes(update, context)


async def ask_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["notes"] = "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    return await _prompt_mood(update, context)


async def _prompt_mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(
        update,
        context,
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
    return await _prompt_screenshot(update, context)


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


async def on_flow_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """✖️ لغو on any flow question — end the conversation, wipe the draft."""
    query = update.callback_query
    await query.answer()
    had_draft = bool(context.user_data)
    _drop_screenshot(context)
    _reset_flow(context)
    chat_id = update.effective_chat.id
    if query.message is not None:
        try:
            await query.message.delete()
        except Exception:
            logger.info("Flow question could not be deleted on cancel.")
    await context.bot.send_message(
        chat_id,
        "ثبت لغو شد." if had_draft else "چیزی برای لغو نبود.",
    )
    return ConversationHandler.END


async def cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    had_draft = bool(context.user_data)
    _drop_screenshot(context)
    _reset_flow(context)
    text = "ثبت لغو شد." if had_draft else "چیزی برای لغو نبود."
    if update.message is not None:
        await update.message.reply_text(text)
    else:
        await update.effective_chat.send_message(text)
    return ConversationHandler.END


def build_conversation() -> ConversationHandler:
    """Build the guided /trade conversation handler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("trade", trade_start),
            # The 📈 New trade menu button must start a REAL conversation
            # (registering the SYMBOL state), otherwise the symbol keyboard
            # taps would be dropped — a plain message would never do that.
            CallbackQueryHandler(_tap_step(trade_start), pattern="^menu:trade$"),
        ],
        states={
            MARKET: [
                CallbackQueryHandler(_tap_step(ask_market), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_market)),
            ],
            SYMBOL: [
                CallbackQueryHandler(_tap_step(ask_symbol), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_symbol)),
            ],
            DIRECTION: [
                CallbackQueryHandler(_tap_step(ask_direction), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_direction)),
            ],
            TIMEFRAME: [
                CallbackQueryHandler(_tap_step(ask_timeframe), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_timeframe)),
            ],
            ENTRY: [
                CallbackQueryHandler(_tap_step(ask_entry), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_entry)),
            ],
            TAKE_PROFIT: [
                CallbackQueryHandler(_tap_step(ask_take_profit), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_take_profit)),
            ],
            STOP_LOSS: [
                CallbackQueryHandler(_tap_step(ask_stop_loss), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_stop_loss)),
            ],
            RESULT: [
                CallbackQueryHandler(_tap_step(ask_result), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_result)),
            ],
            PNL_AMOUNT: [
                CallbackQueryHandler(_tap_step(ask_pnl_amount), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_pnl_amount)),
            ],
            PNL_ROI: [
                CallbackQueryHandler(_tap_step(ask_pnl_roi), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_pnl_roi)),
            ],
            MARGIN: [
                CallbackQueryHandler(_tap_step(ask_margin), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_margin)),
            ],
            RISK: [
                CallbackQueryHandler(_tap_step(ask_risk), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_risk)),
            ],
            TRADE_DATE: [
                CallbackQueryHandler(_tap_step(ask_trade_date), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_trade_date)),
            ],
            TRADE_HOUR: [
                CallbackQueryHandler(_tap_step(ask_trade_hour), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_trade_hour)),
            ],
            NOTES: [
                CallbackQueryHandler(_tap_step(ask_notes), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_notes)),
            ],
            MOOD: [
                CallbackQueryHandler(_tap_step(ask_mood), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_mood)),
            ],
            SCREENSHOT: [
                CallbackQueryHandler(
                    _tap_step(ask_screenshot_text), pattern=_Q_CB_RE
                ),
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, _msg_step(ask_screenshot)
                ),
                MessageHandler(_ANSWER, ask_screenshot_text),
            ],
            SCREENSHOT_AFTER: [
                CallbackQueryHandler(
                    _tap_step(ask_screenshot_after_text), pattern=_Q_CB_RE
                ),
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, _msg_step(ask_screenshot_after)
                ),
                MessageHandler(_ANSWER, ask_screenshot_after_text),
            ],
            CONFIRM: [
                CallbackQueryHandler(_tap_step(save_trade), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(save_trade)),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(on_flow_cancel, pattern=_Q_CANCEL_CB_RE),
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
_STATS_CLOSE = "🔙 بازگشت"
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
            _BACK_NAV_ROW,
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
        [InlineKeyboardButton(_STATS_ALL_SYMBOLS, callback_data=_CB_SALL)]
    )
    rows.append(_BACK_NAV_ROW)
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
    await _show_screen(
        context,
        update.effective_chat.id,
        "stats",
        _render_stats(flt.get("symbol"), flt.get("period")),
        _stats_panel_kb(flt),
    )
    context.user_data.pop("stats_msg_id", None)


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
    """Re-render the stats panel on the morphing screen (edit in place)."""
    await _show_screen(
        context,
        chat_id,
        "stats",
        _render_stats(flt.get("symbol"), flt.get("period")),
        _stats_panel_kb(flt),
    )


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
        text = _render_stats(flt.get("symbol"), flt.get("period"))
        kb = _stats_panel_kb(flt)
        await query.answer()
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        _nav_update_top(context, "stats", text, kb)
        return
    if match.group("page"):
        symbols = db.get_all_symbols()
        pages = max(1, math.ceil(len(symbols) / _SYMBOLS_PER_PAGE))
        page = min(max(int(match.group("page")), 1), pages)
        text = _picker_text(page, pages)
        kb = _symbol_picker_kb(page, pages, symbols)
        await query.answer()
        await query.edit_message_text(text, reply_markup=kb)
        _nav_update_top(context, "stats_symbols", text, kb)
        return
    await query.answer()

    action = query.data
    if match.group("sym") is not None:
        symbol = match.group("sym")
        # Tapping the active symbol again clears the symbol filter.
        flt["symbol"] = None if flt.get("symbol") == symbol else symbol
        _nav_prune(context, "stats_symbols")
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_OPEN:
        # The symbol list morphs the same screen message (🔙 brings the
        # stats panel straight back).
        symbols = db.get_all_symbols()
        if not symbols:
            await context.bot.send_message(
                chat_id, "هنوز معامله‌ای ثبت نشده — نمادی برای فیلتر نیست."
            )
            return
        pages = max(1, math.ceil(len(symbols) / _SYMBOLS_PER_PAGE))
        await _show_screen(
            context,
            chat_id,
            "stats_symbols",
            _picker_text(1, pages),
            _symbol_picker_kb(1, pages, symbols),
        )
    elif action == _CB_SALL:
        flt["symbol"] = None
        _nav_prune(context, "stats_symbols")
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_RESET:
        flt["symbol"] = None
        flt["period"] = None
        _nav_prune(context, "stats_symbols")
        await _refresh_panel(context, chat_id, flt)
    elif action == _CB_EXPORT:
        await _send_export(context, chat_id)
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
        await _show_screen(
            context,
            update.effective_chat.id,
            "recent",
            "🕘 <b>معاملات اخیر</b>\n\n"
            "هنوز معامله‌ای ثبت نشده — با 📈 ثبت معامله بسته شروع کنید.",
            _ik([_HOME_NAV_ROW]),
        )
        return
    pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
    rows = db.get_recent(
        _RECENT_PER_PAGE, offset=0, since=since
    )
    await _show_screen(
        context,
        update.effective_chat.id,
        "recent",
        _recent_panel_text(1, pages),
        _recent_panel_kb(rows, 1, pages),
    )


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
        text = _recent_panel_text(page, pages)
        kb = _recent_panel_kb(rows, page, pages)
        await query.answer()
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        _nav_update_top(context, "recent", text, kb)
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
            text = "در این بازه معامله‌ای نیست."
            kb = _recent_panel_kb([], 1, 1)
            await query.edit_message_text(text, reply_markup=kb)
            _nav_update_top(context, "recent", text, kb)
            return
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        rows = db.get_recent(_RECENT_PER_PAGE, offset=0, since=since)
        text = _recent_panel_text(1, pages)
        kb = _recent_panel_kb(rows, 1, pages)
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        _nav_update_top(context, "recent", text, kb)
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
        has_shots = bool(
            row["screenshot"]
            or row["screenshot_after"]
            or _row_get(row, "exit_photos")
        )
        await update.effective_chat.send_message(
            _recent_detail_text(row),
            reply_markup=_recent_detail_kb(
                row["id"],
                has_shots,
                bool(_row_get(row, "exit_photos")),
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    if match.group("photo") is not None:
        await query.answer()
        await _send_trade_photos(update, int(match.group("photo")))
        return
    if match.group("xphoto") is not None:
        await query.answer()
        await _send_exit_photos(update, int(match.group("xphoto")))
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
        panel_text = _recent_panel_text(page, pages)
        panel_kb = _recent_panel_kb(rows, page, pages)
        panel_msg = _screen_msg_id(context)
        query_msg = getattr(query, "message", None)
        query_msg_id = getattr(query_msg, "message_id", None)
        await query.answer("🗑 حذف شد.")
        _recent_page = page
        if query_msg_id is not None and panel_msg == query_msg_id:
            # Delete button on the panel itself: refresh it in place.
            await query.edit_message_text(
                panel_text,
                reply_markup=panel_kb,
                parse_mode=ParseMode.HTML,
            )
            _nav_update_top(context, "recent", panel_text, panel_kb)
        else:
            # Delete inside a sent detail message: morph it into the
            # refreshed panel (the panel message was edited away? then this
            # edit fails and the panel is simply re-rendered on its slot).
            try:
                await query.edit_message_text(
                    panel_text,
                    reply_markup=panel_kb,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.info("Could not refresh the recent panel.")
            _nav_update_top(context, "recent", panel_text, panel_kb)
        return
    # close / noop (home moved to the shared 🔙/🏠 handler)
    await query.answer()
    if match.group(0) == _RCB_CLOSE:
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
            _BACK_NAV_ROW,
        ]
    )


def _open_detail_text(row) -> str:
    """Big, airy detail card for one OPEN trade (HTML)."""
    side = _DIR_LABEL.get(row["direction"], row["direction"].upper())
    side_icon = "📈" if row["direction"] == "long" else "📉"
    market_fa = (
        "🪙 Crypto" if (row["market"] or "crypto") == "crypto" else "💵 فارکس"
    )
    tf = row["timeframe"] or "—"
    risk = f"{_fmt_num(row['risk_percent'])}%" if row["risk_percent"] else "—"
    margin = (
        f"{_fmt_num(row['margin'])} $" if row["margin"] else "—"
    )
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
        f"• 💰 Margin: {margin}\n"
        + (f"• ⚡ اهرم: {_fmt_num(row['leverage'])}\n" if row["leverage"] else "")
        + f"• 📅 Date: {_ESC(when)}\n"
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
        # Nothing to show yet — still show the panel (➕ starts the flow)
        # so the button always behaves the same way.
        rows: list = []
        await _show_screen(
            context,
            update.effective_chat.id,
            "open",
            "🟢 <b>معاملات باز</b>\n\n"
            "معامله‌ای باز نیست. با دکمه زیر یک معاملهٔ باز ثبت کنید:",
            _open_panel_kb(rows, 1, 1),
        )
        return
    pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
    rows = db.get_open_trades(_RECENT_PER_PAGE, offset=0)
    await _show_screen(
        context,
        update.effective_chat.id,
        "open",
        _open_panel_text(1, pages),
        _open_panel_kb(rows, 1, pages),
    )


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
    if match.group("page") is not None:
        total = db.count_open_trades()
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        page = min(max(int(match.group("page")), 1), pages)
        _open_page = page
        rows = db.get_open_trades(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE
        )
        text = _open_panel_text(page, pages)
        kb = _open_panel_kb(rows, page, pages)
        await query.answer()
        await query.edit_message_text(
            text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        _nav_update_top(context, "open", text, kb)
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
        panel_msg = _screen_msg_id(context)
        query_msg = getattr(query, "message", None)
        query_msg_id = getattr(query_msg, "message_id", None)
        if not total:
            text = "🗑 معامله باز حذف شد — معامله بازی نمانده است."
            await query.answer("حذف شد.")
            await query.edit_message_text(text, reply_markup=None)
            _nav_prune(context, "open")
            return
        pages = max(1, math.ceil(total / _RECENT_PER_PAGE))
        page = min(max(_open_page, 1), pages)
        _open_page = page
        rows = db.get_open_trades(
            _RECENT_PER_PAGE, offset=(page - 1) * _RECENT_PER_PAGE
        )
        panel_text = _open_panel_text(page, pages)
        panel_kb = _open_panel_kb(rows, page, pages)
        await query.answer("🗑 حذف شد.")
        if query_msg_id is not None and panel_msg == query_msg_id:
            # Delete button on the panel itself: refresh it in place.
            await query.edit_message_text(
                panel_text,
                reply_markup=panel_kb,
                parse_mode=ParseMode.HTML,
            )
        else:
            # Delete inside a sent detail message: morph it into the
            # refreshed panel (falls back silently when already gone).
            try:
                await query.edit_message_text(
                    panel_text,
                    reply_markup=panel_kb,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.info("Could not refresh the open-trades panel.")
        _nav_update_top(context, "open", panel_text, panel_kb)
        return
    if match.group("close") is not None:
        open_id = int(match.group("close"))
        row = db.get_open_trade(open_id)
        if row is None:
            await query.answer("این معامله دیگر باز نیست.")
            return
        _reset_flow(context)
        context.user_data["open_id"] = open_id
        context.user_data["open_symbol"] = row["symbol"]
        # Margin snapshot (budget feature) — gives ROI on close (optional).
        context.user_data["open_margin"] = row["margin"]
        context.user_data["open_entry_price"] = row["entry_price"]
        context.user_data["open_direction"] = row["direction"]
        await query.answer()
        await update.effective_chat.send_message(
            f"بستن معامله #{open_id} {_ESC(row['symbol'])} — نتیجه؟",
            reply_markup=_STATUS_KEYBOARD,
        )
        return
    # close-msg / noop  (➕ and 🏁 are conversation entry points)
    await query.answer()
    if match.group(0) == _OCB_CLOSE_MSG:
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
    """Start the open-trades questionnaire with the market question.

    Any half-finished previous draft (and its dangling question message) is
    wiped first — flows always start from scratch, never resume (the trader
    found picking up where they "left off" confusing).
    """
    await _q_drop(update, context)  # remove a dangling question, if any
    _drop_screenshot(context)
    _reset_flow(context)
    text = (
        "معامله باز جدید — در کدام بازار معامله کردی؟\n"
        "(برای انصراف /cancel را بفرستید)"
    )
    if update.message is not None:
        await _q_send(update, context,text, reply_markup=_MARKET_KEYBOARD)
    else:
        # Entry via the ➕ button: there is no message to reply to.
        await _q_send(update, context,
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
        await _q_send(
            update,
            context,
            "یکی از دو دکمه را بزنید: 🪙 Crypto یا 💵 فارکس",
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
    await _q_send(update, context, text, reply_markup=symbol_kb)
    return OPEN_SYMBOL


async def ask_open_symbol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    symbol = (update.message.text or "").strip().upper()
    if not symbol or len(symbol) > 24 or any(ch.isspace() for ch in symbol):
        await update.message.reply_text(
            "این شبیه نماد نیست — دوباره تایپ کنید (مثلاً EURUSD):",
            reply_markup=_symbol_keyboard(),
        )
        return OPEN_SYMBOL
    context.user_data["symbol"] = symbol
    await _q_send(update, context,
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
    return await _prompt_open_timeframe(update, context)


async def _prompt_open_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
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
    return await _prompt_open_reason(update, context)


async def _prompt_open_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "📝 دلیل ورود — چرا وارد این معامله شدی؟",
        reply_markup=_ik([["⏭ بدون دلیل"], _CANCEL_IK_ROW]),
    )
    return OPEN_REASON


async def ask_open_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["reason"] = (
        "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    )
    await _q_send(update, context,
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
    return await _prompt_open_date(update, context)


async def ask_open_screenshot_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() not in _SKIP_SHOT_TOKENS:
        await update.message.reply_text(
            "لطفاً یک تصویر بفرستید، دکمه ⏭ بدون اسکرین‌شات را بزنید، یا '-' را بنویسید."
        )
        return OPEN_SCREENSHOT
    return await _prompt_open_date(update, context)


async def _prompt_open_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
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
        return await _prompt_open_hour(update, context)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt.endswith("%H:%M"):
            context.user_data["trade_date"] = parsed.strftime("%Y-%m-%d")
            context.user_data["entry_time"] = parsed.strftime("%H:%M")
            return await _prompt_open_risk(update, context)
        context.user_data["trade_date"] = parsed.date().isoformat()
        return await _prompt_open_hour(update, context)
    await update.message.reply_text(
        "تاریخ نامعتبر.\n"
        "YYYY-MM-DD یا YYYY-MM-DD HH:MM  (e.g. 2026-02-09 14:30)\n"
        "یا دکمه «📅 امروز» را بزنید."
    )
    return OPEN_TRADE_DATE


async def _prompt_open_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "ساعت ورود:\nHH:MM  (e.g. 14:30) — یا «🕐 الان» برای همین حالا",
        reply_markup=_ik([["00", "03", "06", "09"], ["12", "15", "18", "21"], ["🕐 الان", "⏭ رد کردن"], _CANCEL_IK_ROW]),
    )
    return OPEN_TRADE_HOUR


async def ask_open_trade_hour(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _NOW_TOKENS:
        context.user_data["entry_time"] = datetime.now().strftime("%H:%M")
        return await _prompt_open_risk(update, context)
    if raw.lower() in _SKIP_HOUR_TOKENS:
        context.user_data.setdefault("entry_time", "")
        return await _prompt_open_risk(update, context)
    match = _HOUR_RE.match(raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        await update.message.reply_text(
            "ساعت نامعتبر.\nHH یا HH:MM  (e.g. 14:30)"
        )
        return OPEN_TRADE_HOUR
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    context.user_data["entry_time"] = f"{hour:02d}:{minute:02d}"
    return await _prompt_open_risk(update, context)


async def _prompt_open_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "⚠️ Risk — درصد ریسک این معامله (فقط اطلاعات ثبت می‌شود؛ مثل 1 یا 1%)",
        reply_markup=_ik([["0.5%", "1%", "2%"], ["3%", "5%", "10%"], ["⏭ بدون درصد"], _CANCEL_IK_ROW]),
    )
    return OPEN_RISK


async def ask_open_risk(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw.lower() in _SKIP_RISK_TOKENS:
        context.user_data.pop("risk_percent", None)
        return await _prompt_open_leverage(update, context)
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد ریسک نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2 یا 2%):"
        )
        return OPEN_RISK
    context.user_data["risk_percent"] = number
    return await _prompt_open_leverage(update, context)


async def _prompt_open_margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """💰 Margin — info-only; typed or skipped, never calculated."""
    await _q_send(update, context,
        "💰 Margin — چند دلار به این معامله اختصاص می‌دی؟ (فقط USD، مثل 250)\n"
        f"این عدد فقط ثبت می‌شود و هیچ محاسبه‌ای انجام نمی‌دهد؛ "
        f"{_SKIP_MARGIN_BTN} یعنی بدون مارجین:",
        reply_markup=_OPEN_MARGIN_KEYBOARD,
    )
    return OPEN_MARGIN


async def ask_open_margin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the margin USD exactly as typed — no warnings, no suggestions.

    Nothing is compared against the budget or the risk %; the number is
    info-only. ⏭ رد کردن leaves the margin empty.
    """
    raw = (update.message.text or "").strip()

    # --- skip: no margin at all ------------------------------------------
    if raw in _SKIP_MARGIN_TOKENS:
        context.user_data.pop("margin", None)
        return await _prompt_open_leverage(update, context)

    # --- fresh answer: a typed USD number, or an invalid one --------------
    number = _parse_positive(raw)
    if number is None:
        await update.message.reply_text(
            "مارجین نامعتبر — یک عدد مثبت به دلار بفرستید (مثل 250) "
            f"یا {_SKIP_MARGIN_BTN}:"
        )
        return OPEN_MARGIN
    context.user_data["margin"] = number
    return await _prompt_open_leverage(update, context)


async def _prompt_open_leverage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """اهرم — info-only (the layout's «اهرم»); never used in any math."""
    await _q_send(update, context,
        "⚡ اهرم (فقط اطلاعات — هیچ محاسبه‌ای انجام نمی‌شود):\n"
        f"مثلاً 10 یا 10x؛ یا {_SKIP_LEVERAGE_BTN}:",
        reply_markup=_ik([[_SKIP_LEVERAGE_BTN], _CANCEL_IK_ROW]),
    )
    return OPEN_LEVERAGE


async def ask_open_leverage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the leverage exactly as typed (info-only); skip leaves it NULL."""
    raw = (update.message.text or "").strip()
    if raw.lower() in _LEVERAGE_SKIP_TOKENS:
        context.user_data.pop("leverage", None)
        return await _prompt_open_entry(update, context)
    match = _LEV_RE.match(raw)
    if not match:
        await update.message.reply_text(
            "اهرم نامعتبر — عددی مثل 10 یا 10x بفرستید "
            f"یا {_SKIP_LEVERAGE_BTN}:"
        )
        return OPEN_LEVERAGE
    context.user_data["leverage"] = float(match.group(1))
    return await _prompt_open_entry(update, context)


async def _prompt_open_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "Entry price:"
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
    await _q_send(update, context,
        "🎯 Take Profit (TP):"
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
    await _q_send(update, context,
        "🛑 Stop Loss (SL):"
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
        "🪙 Crypto" if (data.get("market") or "crypto") == "crypto" else "💵 فارکس"
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
        + (
            f"• Margin    {_fmt_num(data['margin'])} $\n"
            if data.get("margin")
            else ""
        )
        + (
            f"• Leverage  ×{_fmt_num(data['leverage'])}\n"
            if data.get("leverage")
            else ""
        )
        + f"• Entry     {_fmt_num(data['entry_price'])}\n"
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
        await _q_send(
            update, context, summary, reply_markup=_OPEN_CONFIRM_KEYBOARD
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
        _reset_flow(context)
        await _drop_screen_message(
            context, update.effective_chat.id, "flow"
        )
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
            margin=data.get("margin"),
            leverage=data.get("leverage"),
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
            + (
                f"• 💰 Margin: {_fmt_num(data['margin'])} $\n"
                if data.get("margin")
                else ""
            )
            + (
                f"• ⚡ اهرم: ×{_fmt_num(data['leverage'])}\n"
                if data.get("leverage")
                else ""
            )
            + f"• Entry: {_fmt_num(data['entry_price'])}\n"
            f"• TP / SL: {_fmt_num(data['take_profit'])}"
            f" / {_fmt_num(data['stop_loss'])}\n"
            f"• 📅 {when}\n"
            "\n"
            "وقتی بستی، از 🟢 معاملات باز با دکمه 🏁 ببندش."
        )
        try:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.error(
                "HTML open confirmation failed:\n%s", traceback.format_exc()
            )
            await update.message.reply_text(
                re.sub(r"</?[bi]>", "", text)
            )
        return ConversationHandler.END
    if answer in ("n", "no", "❌ ثبت نشود", "❌ discard", "خیر", "ثبت نشود"):
        _drop_screenshot(context)
        _reset_flow(context)
        await update.message.reply_text(
            "❌ ثبت نشد — چیزی ذخیره نشد."
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
            await _q_send(update, context,text)
        else:
            await _q_send(update, context,text)
        return ConversationHandler.END
    _reset_flow(context)
    context.user_data["open_id"] = open_id
    context.user_data["open_symbol"] = row["symbol"]
    # Margin snapshot (budget feature) — gives ROI on close (optional).
    context.user_data["open_margin"] = row["margin"]
    context.user_data["open_entry_price"] = row["entry_price"]
    context.user_data["open_direction"] = row["direction"]
    await _q_send(update, context,
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
            "نتیجه را انتخاب کنید: ✅ Win / ❌ Loss / ➖ BE / ✏️ دستی"
        )
        return CLOSE_STATUS
    context.user_data["hit"] = status
    if status == "be":
        # Breakeven: nothing gained, nothing lost — skip the amount.
        context.user_data["close_pnl"] = 0.0
        context.user_data["close_roi"] = 0.0
        await _q_send(update, context,
            "تاریخ بستن معامله:\nYYYY-MM-DD  (e.g. 2026-02-09)",
            reply_markup=_DATE_KEYBOARD,
        )
        return CLOSE_DATE
    return await _prompt_close_amount(update, context)


async def _prompt_close_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    hit = context.user_data.get("hit")
    if hit == "win":
        hint = "چند دلار سود کردی؟ (فقط عدد، مثلاً 120.50)"
    elif hit == "lose":
        hint = "چند دلار ضرر کردی؟ (فقط عدد، مثلاً 80)"
    else:
        hint = "چند دلار سود یا ضرر کردی؟ (فقط عدد)"
    await _q_send(update, context, f"💵 {hint}")
    return CLOSE_AMOUNT


async def ask_close_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the dollar amount the trader actually gained or lost."""
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "مبلغ نامعتبر — فقط عدد بفرستید (مثلاً 120.50):"
        )
        return CLOSE_AMOUNT
    context.user_data["close_pnl"] = number
    return await _prompt_close_roi(update, context)


async def _prompt_close_roi(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """The typed ROI percent of the close (بخش دوم) — stored as-is."""
    await _q_send(update, context,
        "📊 مقدار سود یا ضرر به درصد چقدر بود؟ (مثلاً 2.5)\n"
        f"{_SKIP_MARGIN_BTN} یعنی بدون درصد:",
        reply_markup=_ik([[_SKIP_MARGIN_BTN], _CANCEL_IK_ROW]),
    )
    return CLOSE_ROI


async def ask_close_roi(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Store the typed ROI percent; the result question only signs it."""
    raw = (update.message.text or "").strip()
    if raw in _SKIP_MARGIN_TOKENS:
        context.user_data.pop("close_roi", None)
        await _q_send(update, context,
            "تاریخ بستن معامله:\nYYYY-MM-DD  (e.g. 2026-02-09)",
            reply_markup=_DATE_KEYBOARD,
        )
        return CLOSE_DATE
    number = _parse_percent(raw)
    if number is None:
        await update.message.reply_text(
            "درصد نامعتبر — عددی بین 0 تا 100 بفرستید (مثلاً 2.5):"
        )
        return CLOSE_ROI
    context.user_data["close_roi"] = number
    await _q_send(update, context,
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
        return await _prompt_close_hour(update, context)
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
        return await _prompt_close_hour(update, context)
    await update.message.reply_text(
        "تاریخ نامعتبر.\n"
        "YYYY-MM-DD یا YYYY-MM-DD HH:MM  (e.g. 2026-02-09 14:30)\n"
        "یا دکمه «📅 امروز» را بزنید."
    )
    return CLOSE_DATE


async def _prompt_close_hour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "ساعت بستن:\nHH:MM  (e.g. 14:30) — یا «🕐 الان» برای همین حالا",
        reply_markup=_ik([["00", "03", "06", "09"], ["12", "15", "18", "21"], ["🕐 الان", "⏭ رد کردن"], _CANCEL_IK_ROW]),
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
        await _q_send(update, context,
            f"🎯 Exit price: {_fmt_num(row['take_profit'])} (TP hit)",
            reply_markup=None,
        )
        return await _prompt_close_photos(update, context)
    if hit == "loss" and row is not None and row["stop_loss"]:
        context.user_data["exit_price"] = row["stop_loss"]
        await _q_send(update, context,
            f"🛑 Exit price: {_fmt_num(row['stop_loss'])} (SL hit)",
            reply_markup=None,
        )
        return await _prompt_close_photos(update, context)
    if hit == "be" and row is not None:
        context.user_data["exit_price"] = row["entry_price"]
        await _q_send(update, context,
            f"➖ Exit price: {_fmt_num(row['entry_price'])} (breakeven)",
            reply_markup=None,
        )
        return await _prompt_close_photos(update, context)
    await _q_send(update, context,
        "Exit price:"
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
    return await _prompt_close_photos(update, context)


async def _prompt_close_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "📸 اسکرین‌شات خروج — تا ۴ تصویر، یکی‌یکی بفرستید (اختیاری):",
        reply_markup=_ik([["⏭ بدون اسکرین‌شات"], _CANCEL_IK_ROW]),
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
        return await _prompt_close_reason(update, context)
    await _q_send(update, context,
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
    return await _prompt_close_reason(update, context)


async def _prompt_close_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(update, context,
        "📝 دلیل خروج — چرا از معامله خارج شدی؟",
        reply_markup=_ik([["⏭ بدون دلیل"], _CANCEL_IK_ROW]),
    )
    return CLOSE_REASON


async def _prompt_close_mood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _q_send(
        update,
        context,
        "Mood — حال‌وهوای حین معامله (اختیاری):",
        reply_markup=_MOOD_KEYBOARD,
    )
    return CLOSE_MOOD


async def ask_close_reason(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["notes"] = (
        "" if raw.lower() in _SKIP_NOTES_TOKENS else raw
    )
    return await _prompt_close_mood(update, context)


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
        return CLOSE_MOOD
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
    # The P&L is the trader's typed amount (signed by the status); ROI only
    # appears when the open questionnaire recorded a margin (optional).
    pnl = _close_pnl_signed(data)
    roi = _close_roi_signed(data)
    margin = data.get("open_margin")
    pnl_line = (
        f"• P&L       {_fmt_pnl(pnl)}\n"
        + (f"           ROI {_fmt_roi(roi)}\n" if roi is not None else "")
        + (
            f"• Margin    {_fmt_num(margin)} $ (اطلاعات)\n"
            if margin
            else ""
        )
    )
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
        + pnl_line
        + "\n"
        "معامله به تاریخچه معاملات بسته‌شده منتقل می‌شود.\n"
        "ثبت شود؟"
    )


def _close_pnl_signed(data: dict) -> float:
    """Signed P&L of a close draft (trader's typed amount, status = sign)."""
    amount = data.get("close_pnl") or 0.0
    hit = data.get("hit")
    if hit == "lose":
        return -abs(amount)
    if hit == "be":
        return 0.0
    return abs(amount)


async def _prompt_close_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    summary = _close_summary(context.user_data)
    try:
        await _q_send(
            update, context, summary, reply_markup=_OPEN_CONFIRM_KEYBOARD
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
        _reset_flow(context)
        open_id = data.pop("open_id")
        # The trader types the dollar result and the ROI percent; the status
        # only signs them. Nothing is computed from the margin.
        _close_pnl = _close_pnl_signed(data)
        _close_roi = _close_roi_signed(data)
        data["_close_pnl"], data["_close_roi"] = _close_pnl, _close_roi
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
            pnl=_close_pnl,
            roi=_close_roi,
        )
        if new_id is None:
            await update.message.reply_text(
                "این معامله دیگر باز نیست — شاید قبلاً بسته شده باشد.",
            )
            return ConversationHandler.END
        logger.info(
            "Closed open trade #%s -> trade #%s (%s)",
            open_id, new_id, data.get("hit"),
        )
        _reset_flow(context)
        chat_id = update.effective_chat.id
        await _drop_screen_message(context, chat_id, "flow")
        # Budget feature: a closed trade moves the account budget by its P&L.
        budget_line = ""
        new_budget = db.adjust_budget(_close_pnl)
        if new_budget is not None:
            budget_line = f"\n• 💰 بودجهٔ جدید: {_fmt_num(new_budget)} $"
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
            + (
                f"• 💵 P&L: {_fmt_num(_close_pnl)} $"
                + (
                    f" · {_fmt_num(_close_roi)}%"
                    if _close_roi is not None
                    else ""
                )
                + "\n"
            )
            + budget_line
            + "\n"
            "در 🕘 معاملات اخیر و 📊 آمار قابل مشاهده است."
        )
        try:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.error(
                "HTML close confirmation failed:\\n%s", traceback.format_exc()
            )
            await update.message.reply_text(re.sub(r"</?[bi]>", "", text))
        return ConversationHandler.END
        try:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.error(
                "HTML close confirmation failed:\n%s", traceback.format_exc()
            )
            await update.message.reply_text(re.sub(r"</?[bi]>", "", text))
        return ConversationHandler.END
    if answer in ("n", "no", "❌ ثبت نشود", "❌ discard", "خیر", "ثبت نشود"):
        _drop_screenshot(context)
        _reset_flow(context)
        await update.message.reply_text(
            "❌ ثبت نشد — معامله هنوز باز است."
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
            # 🟢 ثبت معامله باز on the main menu — straightforward start.
            CallbackQueryHandler(_tap_step(open_trade_start), pattern="^menu:open$"),
            # /open — same straightforward start (see /opens for the panel).
            CommandHandler("open", open_trade_start),
        ],
        states={
            OPEN_MARKET: [
                CallbackQueryHandler(_tap_step(ask_open_market), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_market)),
            ],
            OPEN_SYMBOL: [
                CallbackQueryHandler(_tap_step(ask_open_symbol), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_symbol)),
            ],
            OPEN_DIRECTION: [
                CallbackQueryHandler(_tap_step(ask_open_direction), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_direction)),
            ],
            OPEN_TIMEFRAME: [
                CallbackQueryHandler(_tap_step(ask_open_timeframe), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_timeframe)),
            ],
            OPEN_REASON: [
                CallbackQueryHandler(_tap_step(ask_open_reason), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_reason)),
            ],
            OPEN_SCREENSHOT: [
                CallbackQueryHandler(
                    _tap_step(ask_open_screenshot_text), pattern=_Q_CB_RE
                ),
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, _msg_step(ask_open_screenshot)
                ),
                MessageHandler(_ANSWER, ask_open_screenshot_text),
            ],
            OPEN_TRADE_DATE: [
                CallbackQueryHandler(_tap_step(ask_open_trade_date), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_trade_date)),
            ],
            OPEN_TRADE_HOUR: [
                CallbackQueryHandler(_tap_step(ask_open_trade_hour), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_trade_hour)),
            ],
            OPEN_RISK: [
                CallbackQueryHandler(_tap_step(ask_open_risk), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_risk)),
            ],
            OPEN_MARGIN: [
                CallbackQueryHandler(_tap_step(ask_open_margin), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_margin)),
            ],
            OPEN_LEVERAGE: [
                CallbackQueryHandler(_tap_step(ask_open_leverage), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_leverage)),
            ],
            OPEN_ENTRY: [
                CallbackQueryHandler(_tap_step(ask_open_entry), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_entry)),
            ],
            OPEN_TAKE_PROFIT: [
                CallbackQueryHandler(_tap_step(ask_open_take_profit), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_take_profit)),
            ],
            OPEN_STOP_LOSS: [
                CallbackQueryHandler(_tap_step(ask_open_stop_loss), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_open_stop_loss)),
            ],
            OPEN_CONFIRM: [
                CallbackQueryHandler(_tap_step(save_open_trade), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(save_open_trade)),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(on_flow_cancel, pattern=_Q_CANCEL_CB_RE),
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
            CLOSE_STATUS: [
                CallbackQueryHandler(_tap_step(ask_close_status), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_status)),
            ],
            CLOSE_AMOUNT: [
                CallbackQueryHandler(_tap_step(ask_close_amount), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_amount)),
            ],
            CLOSE_ROI: [
                CallbackQueryHandler(_tap_step(ask_close_roi), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_roi)),
            ],
            CLOSE_DATE: [
                CallbackQueryHandler(_tap_step(ask_close_date), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_date)),
            ],
            CLOSE_HOUR: [
                CallbackQueryHandler(_tap_step(ask_close_hour), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_hour)),
            ],
            CLOSE_PRICE: [
                CallbackQueryHandler(_tap_step(ask_close_price), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_price)),
            ],
            CLOSE_PHOTOS: [
                CallbackQueryHandler(
                    _tap_step(ask_close_photos_text), pattern=_Q_CB_RE
                ),
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE, _msg_step(ask_close_photos)
                ),
                MessageHandler(_ANSWER, ask_close_photos_text),
            ],
            CLOSE_REASON: [
                CallbackQueryHandler(_tap_step(ask_close_reason), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_reason)),
            ],
            CLOSE_MOOD: [
                CallbackQueryHandler(_tap_step(ask_close_mood), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(ask_close_mood)),
            ],
            CLOSE_CONFIRM: [
                CallbackQueryHandler(_tap_step(save_close_trade), pattern=_Q_CB_RE),
                MessageHandler(_ANSWER, _msg_step(save_close_trade)),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(on_flow_cancel, pattern=_Q_CANCEL_CB_RE),
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
_RCB_XPHOTO = f"{_RCB}:px:"
_RCB_DEL = f"{_RCB}:d:"
_RCB_HOME = f"{_RCB}:home"
_RCB_CLOSE = f"{_RCB}:close"
_RCB_NOOP = f"{_RCB}:noop"
_RECENT_CB_RE = re.compile(
    r"^" + re.escape(_RCB)
    + r":(?:p:(?P<page>\d+)|r:(?P<range>all|1w|1m)"
    r"|v:(?P<view>\d+)|ph:(?P<photo>\d+)|px:(?P<xphoto>\d+)"
    r"|d:(?P<del>\d+)|home|close|noop)$"
)


def _row_get(row, key):
    """Column value that tolerates rows fetched before a migration step."""
    return row[key] if key in row.keys() else None


def _recent_button(row) -> str:
    """Label of a trade's list button: emoji, id, symbol, P&L, marks."""
    emoji = _result_emoji(row["hit"])
    shots = " 📷" if row["screenshot"] or row["screenshot_after"] else ""
    two_phase = " 🔁" if (_row_get(row, "source") or "") == "open" else ""
    # Open-flow closes have no margin question, so P&L can be NULL.
    pnl_txt = _fmt_pnl(row["pnl"]) if row["pnl"] is not None else "—"
    return (
        f"{emoji} #{row['id']} — {_ESC(row['symbol'])}"
        f" · {pnl_txt}{shots}{two_phase}"
    )


def _recent_panel_text(page: int, pages: int) -> str:
    """Heading above the /recent button list (the trades ARE the buttons)."""
    return (
        "🕘 <b>معاملات اخیر</b>\n"
        f"📄 صفحه {_fa_num(page)} از {_fa_num(pages)} — "
        "برای دیدن جزئیات کامل، روی معامله بزنید 👇"
    )


def _recent_detail_text(row) -> str:
    """Big, airy detail card for one trade (HTML) — sent as its own message.

    Renders /trade entries and two-phase (open → close) trades alike; every
    field a flow does not provide shows as “—” instead of crashing.
    """
    roi = row["roi"]
    emoji = _result_emoji(row["hit"])
    side = _DIR_LABEL.get(row["direction"], (row["direction"] or "?").upper())
    side_icon = "📈" if row["direction"] == "long" else "📉"
    market_fa = (
        "🪙 Crypto" if (row["market"] or "crypto") == "crypto" else "💵 فارکس"
    )
    tf = row["timeframe"] or "—"
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

    is_two_phase = (_row_get(row, "source") or "") == "open"
    entry_time = _row_get(row, "entry_time")
    exit_time = _row_get(row, "exit_time")
    entry_reason = _row_get(row, "entry_reason")
    exit_reason = _row_get(row, "exit_reason")
    exit_photos = _row_get(row, "exit_photos")

    # Date & times: two-phase trades know the exit date plus entry/exit hours;
    # /trade rows keep their single (optionally time-stamped) date.
    date_line = f"• 📅 تاریخ: {_ESC(row['trade_date'])}"
    time_bits = []
    if entry_time:
        time_bits.append(f"ورود {entry_time}")
    if exit_time:
        time_bits.append(f"خروج {exit_time}")
    time_line = f"• 🕐 ساعت: {' · '.join(time_bits)}" if time_bits else None

    # Reasons: /trade stores the entry reason in `notes`; two-phase trades
    # carry separate entry_reason / exit_reason columns.
    entry_reason_txt = _ESC(entry_reason or row["notes"]) if (
        entry_reason or row["notes"]
    ) else "—"
    reason_lines = [f"• 💭 دلیل ورود: {entry_reason_txt}"]
    if exit_reason:
        reason_lines.append(f"• 🧯 دلیل خروج: {_ESC(exit_reason)}")

    shots = []
    if row["screenshot"]:
        shots.append("قبل")
    if row["screenshot_after"]:
        shots.append("بعد")
    exit_shot_count = len([n for n in (exit_photos or "").splitlines() if n])
    if exit_shot_count:
        shots.append(f"خروج ×{_fa_num(exit_shot_count)}")
    shots_txt = "  •  ".join(shots) if shots else "—"

    badge = (
        "\n• 🔁 ثبت دو مرحله‌ای — باز شد، بعداً بسته شد" if is_two_phase else ""
    )
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
        f"• 💰 مارجین: {_fmt_size(row['size']) if row['size'] else '—'}\n"
        f"• ⚠️ ریسک: {risk}\n"
        + (
            f"• ⚡ اهرم: ×{_fmt_num(row['leverage'])}\n"
            if row["leverage"]
            else ""
        )
        + "\n"
        f"{date_line}\n"
        + (f"{time_line}\n" if time_line else "")
        + f"• 🧠 حالت: {_ESC(mood)}\n"
        + "\n".join(reason_lines) + "\n"
        f"• 📸 عکس: {shots_txt}\n"
        "\n"
        f"💵 سود و زیان: <b>{_fmt_pnl(row['pnl'])}</b>\n"
        f"📊 بازدهی (ROI): <b>{_fmt_roi(roi)}</b>"
        + badge
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
            _BACK_NAV_ROW,
        ]
    )


def _recent_detail_kb(
    trade_id: int,
    has_shots: bool = False,
    has_exit_shots: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons on a sent detail: 📷 entry shots, 📸 exit shots, 🗑, ❌."""
    rows = []
    if has_shots:
        rows.append(
            [InlineKeyboardButton("📷 عکس چارت", callback_data=_RCB_PHOTO + str(trade_id))]
        )
    if has_exit_shots:
        rows.append(
            [
                InlineKeyboardButton(
                    "📸 عکس‌های خروج", callback_data=_RCB_XPHOTO + str(trade_id)
                )
            ]
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


async def _send_exit_photos(update: Update, trade_id: int) -> None:
    """Send the exit screenshots of a two-phase trade (📸 button on the detail)."""
    row = db.get_trade(trade_id)
    if row is None:
        await update.effective_chat.send_message(
            f"معامله‌ای با شماره #{trade_id} پیدا نشد."
        )
        return
    names = [
        n
        for n in (_row_get(row, "exit_photos") or "").splitlines()
        if n
    ]
    sent_any = False
    for i, name in enumerate(names, start=1):
        path = _screenshot_path(name)
        if not path.is_file():
            continue
        with path.open("rb") as photo:
            await update.effective_chat.send_photo(
                photo,
                caption=(
                    f"#{trade_id} {row['symbol']} {row['trade_date']}"
                    f" — خروج {_fa_num(i)}/{_fa_num(len(names))}"
                ),
            )
        sent_any = True
    if not sent_any:
        await update.effective_chat.send_message(
            f"معامله #{trade_id} اسکرین‌شات خروج ندارد."
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
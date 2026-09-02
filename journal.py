"""Guided /trade conversation (text prompts with button choices) plus the
/recent, /stats and /delete commands."""

from __future__ import annotations

import logging
import math
import warnings
from datetime import date, datetime
from typing import Optional

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from telegram.warnings import PTBUserWarning

# The /trade conversation intentionally mixes MessageHandler and
# CallbackQueryHandler in the same states, so per_message=False (the default)
# is the correct setting here; silence PTB's reminder about it.
warnings.filterwarnings(
    "ignore", category=PTBUserWarning, message="If 'per_message=False'"
)

logger = logging.getLogger(__name__)

# Conversation states, in the order the questions are asked.
(
    SYMBOL,
    DIRECTION,
    ENTRY,
    EXIT,
    SIZE,
    PNL,
    TRADE_DATE,
    NOTES,
    CONFIRM,
) = range(9)

_TEXT = filters.TEXT & ~filters.COMMAND
_LONG_ALIASES = {"long", "l", "buy", "b"}
_SHORT_ALIASES = {"short", "s", "sell"}
_SKIP_TOKENS = {"", "-", "skip"}


# --------------------------------------------------------------------------
# Inline keyboards for the button prompts
# --------------------------------------------------------------------------

def _kb(row: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Build a single-row inline keyboard from (label, callback_data) pairs."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row]]
    )


_DIR_KEYBOARD = _kb([("📈 Long", "dir:long"), ("📉 Short", "dir:short")])
_PNL_KEYBOARD = _kb([("🤖 Auto-calculate", "pnl:auto")])
_DATE_KEYBOARD = _kb([("📅 Today", "date:today"), ("✖️ Cancel", "cancel")])
_NOTES_KEYBOARD = _kb([("⏭ Skip notes", "notes:skip"), ("✖️ Cancel", "cancel")])
_CONFIRM_KEYBOARD = _kb([("✅ Save", "confirm:yes"), ("❌ Discard", "confirm:no")])


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


def _parse_direction(raw: str) -> Optional[str]:
    word = raw.strip().lower()
    if word in _LONG_ALIASES:
        return "long"
    if word in _SHORT_ALIASES:
        return "short"
    return None


def _fmt_num(value: float) -> str:
    """Compact price formatting without trailing zeros."""
    return f"{value:.10g}"


def _fmt_size(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _fmt_pnl(value: float) -> str:
    return f"{value:+,.2f}"


def _auto_pnl(data: dict) -> float:
    """Signed P&L from entry/exit/size (units-based instruments)."""
    if data["direction"] == "long":
        return (data["exit_price"] - data["entry_price"]) * data["size"]
    return (data["entry_price"] - data["exit_price"]) * data["size"]


# --------------------------------------------------------------------------
# Prompt helpers (prompts that carry inline button choices)
# --------------------------------------------------------------------------

async def _mark_choice(query: CallbackQuery, chosen: str) -> None:
    """Acknowledge a button press and echo the choice under the prompt."""
    await query.answer()
    text = getattr(query.message, "text", None)
    if text:
        await query.edit_message_text(f"{text}\n\n→ {chosen}")


async def _prompt_direction(update: Update) -> int:
    await update.effective_chat.send_message(
        "Long or short?", reply_markup=_DIR_KEYBOARD
    )
    return DIRECTION


async def _prompt_entry(update: Update) -> int:
    await update.effective_chat.send_message("Entry price:")
    return ENTRY


async def _prompt_exit(update: Update) -> int:
    await update.effective_chat.send_message("Exit price:")
    return EXIT


async def _prompt_size(update: Update) -> int:
    await update.effective_chat.send_message("Size (units / shares / lots):")
    return SIZE


async def _prompt_pnl(update: Update) -> int:
    await update.effective_chat.send_message(
        "P&L — tap to auto-calculate from entry/exit/size, or type the "
        "value yourself (e.g. -45.5):",
        reply_markup=_PNL_KEYBOARD,
    )
    return PNL


async def _prompt_trade_date(update: Update) -> int:
    await update.effective_chat.send_message(
        "Close date (YYYY-MM-DD):", reply_markup=_DATE_KEYBOARD
    )
    return TRADE_DATE


async def _prompt_notes(update: Update) -> int:
    await update.effective_chat.send_message(
        "Notes (optional):", reply_markup=_NOTES_KEYBOARD
    )
    return NOTES


def _summary(data: dict) -> str:
    """Render the confirmation summary for the current draft."""
    pnl = _auto_pnl(data) if data["pnl"] is None else data["pnl"]
    auto = " (auto)" if data["pnl"] is None else ""
    return (
        "Please confirm:\n\n"
        f"Symbol:    {data['symbol']}\n"
        f"Direction: {data['direction'].upper()}\n"
        f"Entry:     {_fmt_num(data['entry_price'])}\n"
        f"Exit:      {_fmt_num(data['exit_price'])}\n"
        f"Size:      {_fmt_size(data['size'])}\n"
        f"Date:      {data['trade_date']}\n"
        f"P&L:       {_fmt_pnl(pnl)}{auto}\n"
        f"Notes:     {data['notes'] or '-'}\n\n"
        "Save this trade?"
    )


async def _prompt_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.effective_chat.send_message(
        _summary(context.user_data), reply_markup=_CONFIRM_KEYBOARD
    )
    return CONFIRM


async def _save_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: Optional[CallbackQuery] = None,
) -> int:
    """Persist the confirmed draft and reply where the user interacted."""
    data = dict(context.user_data)
    context.user_data.clear()
    pnl = _auto_pnl(data) if data["pnl"] is None else data["pnl"]
    trade_id = db.add_trade(
        symbol=data["symbol"],
        direction=data["direction"],
        entry_price=data["entry_price"],
        exit_price=data["exit_price"],
        size=data["size"],
        pnl=pnl,
        trade_date=data["trade_date"],
        notes=data["notes"],
    )
    logger.info("Saved trade #%s %s", trade_id, data["symbol"])
    text = (
        f"✅ Saved trade #{trade_id} — {data['symbol']} {data['direction'].upper()} "
        f"{_fmt_num(data['entry_price'])} -> {_fmt_num(data['exit_price'])} "
        f"x{_fmt_size(data['size'])}, P&L {_fmt_pnl(pnl)}"
    )
    if query is not None:
        await query.answer()
        await query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    return ConversationHandler.END


async def _discard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: Optional[CallbackQuery] = None,
) -> int:
    """Drop the current draft and reply where the user interacted."""
    context.user_data.clear()
    text = "❌ Discarded — nothing was saved."
    if query is not None:
        await query.answer()
        await query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    return ConversationHandler.END


# --------------------------------------------------------------------------
# /trade conversation steps
# --------------------------------------------------------------------------

async def trade_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "New trade — tap the buttons or type your answers;\n"
        "send /cancel to abort.\n\n"
        "Symbol (e.g. EURUSD, BTCUSD, AAPL):"
    )
    return SYMBOL


async def ask_symbol(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    symbol = (update.message.text or "").strip().upper()
    if not symbol or len(symbol) > 24 or any(ch.isspace() for ch in symbol):
        await update.message.reply_text(
            "That doesn't look like a symbol — try again (e.g. EURUSD):"
        )
        return SYMBOL
    context.user_data["symbol"] = symbol
    return await _prompt_direction(update)


async def ask_direction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    direction = _parse_direction(update.message.text or "")
    if direction is None:
        await update.message.reply_text("Please answer long or short (l/s):")
        return DIRECTION
    context.user_data["direction"] = direction
    return await _prompt_entry(update)


async def direction_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    choice = (query.data or "").rsplit(":", 1)[-1]
    if choice not in ("long", "short"):
        await query.answer()
        return DIRECTION
    context.user_data["direction"] = choice
    await _mark_choice(query, "📈 Long" if choice == "long" else "📉 Short")
    return await _prompt_entry(update)


async def ask_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "Enter a positive number, using . as the decimal separator:"
        )
        return ENTRY
    context.user_data["entry_price"] = number
    return await _prompt_exit(update)


async def ask_exit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text(
            "Enter a positive number, using . as the decimal separator:"
        )
        return EXIT
    context.user_data["exit_price"] = number
    return await _prompt_size(update)


async def ask_size(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    number = _parse_positive(update.message.text or "")
    if number is None:
        await update.message.reply_text("Enter a positive size:")
        return SIZE
    context.user_data["size"] = number
    return await _prompt_pnl(update)


async def ask_pnl(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS:
        context.user_data["pnl"] = None  # auto-calculate on save
    else:
        pnl = _parse_number(raw)
        if pnl is None:
            await update.message.reply_text(
                "Enter a number (negative allowed) or '-' to auto-calculate:"
            )
            return PNL
        context.user_data["pnl"] = pnl
    return await _prompt_trade_date(update)


async def pnl_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if (query.data or "").rsplit(":", 1)[-1] != "auto":
        await query.answer()
        return PNL
    context.user_data["pnl"] = None  # auto-calculate on save
    await _mark_choice(query, "🤖 Auto-calculated")
    return await _prompt_trade_date(update)


async def ask_trade_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    if raw in _SKIP_TOKENS:
        chosen = date.today()
    else:
        try:
            chosen = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            await update.message.reply_text(
                "Use YYYY-MM-DD (e.g. 2026-02-09) or '-' for today:"
            )
            return TRADE_DATE
    context.user_data["trade_date"] = chosen.isoformat()
    return await _prompt_notes(update)


async def trade_date_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if (query.data or "").rsplit(":", 1)[-1] != "today":
        await query.answer()
        return TRADE_DATE
    context.user_data["trade_date"] = date.today().isoformat()
    await _mark_choice(query, f"📅 {date.today().isoformat()}")
    return await _prompt_notes(update)


async def ask_notes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    raw = (update.message.text or "").strip()
    context.user_data["notes"] = "" if raw in _SKIP_TOKENS else raw
    return await _prompt_confirm(update, context)


async def notes_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if (query.data or "").rsplit(":", 1)[-1] != "skip":
        await query.answer()
        return NOTES
    context.user_data["notes"] = ""
    await _mark_choice(query, "⏭ Skipped")
    return await _prompt_confirm(update, context)


async def save_trade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    answer = (update.message.text or "").strip().lower()
    if answer in ("y", "yes"):
        return await _save_and_reply(update, context)
    if answer in ("n", "no"):
        return await _discard(update, context)
    await update.message.reply_text("Please answer yes or no:")
    return CONFIRM


async def confirm_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    choice = (query.data or "").rsplit(":", 1)[-1]
    if choice == "yes":
        return await _save_and_reply(update, context, query=query)
    if choice == "no":
        return await _discard(update, context, query=query)
    await query.answer()
    return CONFIRM


async def cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    had_draft = bool(context.user_data)
    context.user_data.clear()
    text = "Entry cancelled." if had_draft else "Nothing to cancel."
    message = update.callback_query and update.callback_query.message
    if message is not None:
        await message.reply_text(text)
    elif update.message is not None:
        await update.message.reply_text(text)
    return ConversationHandler.END


def build_conversation() -> ConversationHandler:
    """Build the guided /trade conversation handler."""
    return ConversationHandler(
        entry_points=[CommandHandler("trade", trade_start)],
        states={
            SYMBOL: [MessageHandler(_TEXT, ask_symbol)],
            DIRECTION: [
                CallbackQueryHandler(direction_button, pattern=r"^dir:(long|short)$"),
                MessageHandler(_TEXT, ask_direction),
            ],
            ENTRY: [MessageHandler(_TEXT, ask_entry)],
            EXIT: [MessageHandler(_TEXT, ask_exit)],
            SIZE: [MessageHandler(_TEXT, ask_size)],
            PNL: [
                CallbackQueryHandler(pnl_button, pattern=r"^pnl:auto$"),
                MessageHandler(_TEXT, ask_pnl),
            ],
            TRADE_DATE: [
                CallbackQueryHandler(trade_date_button, pattern=r"^date:today$"),
                MessageHandler(_TEXT, ask_trade_date),
            ],
            NOTES: [
                CallbackQueryHandler(notes_button, pattern=r"^notes:skip$"),
                MessageHandler(_TEXT, ask_notes),
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_button, pattern=r"^confirm:(yes|no)$"),
                MessageHandler(_TEXT, save_trade),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^cancel$"),
        ],
        allow_reentry=True,
    )


# --------------------------------------------------------------------------
# /recent, /stats, /delete commands
# --------------------------------------------------------------------------

async def recent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    rows = db.get_recent(10)
    if not rows:
        await update.message.reply_text(
            "No trades yet — log your first with /trade."
        )
        return
    lines = [f"Last {len(rows)} trade(s), newest first:"]
    for row in rows:
        lines.append(
            f"#{row['id']} {row['trade_date']} {row['symbol']} "
            f"{row['direction'].upper()} {_fmt_num(row['entry_price'])}"
            f" -> {_fmt_num(row['exit_price'])} x{_fmt_size(row['size'])} "
            f"P&L {_fmt_pnl(row['pnl'])}"
        )
    await update.message.reply_text("\n".join(lines))


async def stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    s = db.get_stats()
    if s.get("trades", 0) == 0:
        await update.message.reply_text(
            "No trades yet — log your first with /trade."
        )
        return
    wins, losses = s["wins"] or 0, s["losses"] or 0
    decided = wins + losses
    profit_factor = (
        s["gross_win"] / -s["gross_loss"] if s["gross_loss"] else None
    )
    lines = [
        f"Trades: {s['trades']}   W/L: {wins}/{losses}",
        f"Win rate: {wins / decided * 100:.1f}%" if decided else "Win rate: n/a",
        f"Total P&L: {_fmt_pnl(s['total'])}",
        "Avg win: "
        + (_fmt_pnl(s["avg_win"]) if s["avg_win"] is not None else "-")
        + "   Avg loss: "
        + (_fmt_pnl(s["avg_loss"]) if s["avg_loss"] is not None else "-"),
        f"Profit factor: {profit_factor:.2f}" if profit_factor is not None
        else "Profit factor: n/a",
        f"Best: {_fmt_pnl(s['best'])}   Worst: {_fmt_pnl(s['worst'])}",
    ]
    await update.message.reply_text("\n".join(lines))


async def delete_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    usage = "Usage: /delete <id> (see /recent for ids)"
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
        await update.message.reply_text(f"No trade with id #{trade_id}.")
        return
    logger.info("Deleted trade #%s", trade_id)
    await update.message.reply_text(
        f"Deleted #{row['id']} {row['trade_date']} {row['symbol']} "
        f"{row['direction'].upper()} — P&L {_fmt_pnl(row['pnl'])}"
    )
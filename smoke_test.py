"""Offline smoke test for the button-driven /trade flow (no Telegram needed).

Run:  .\\.venv\\Scripts\\python.exe smoke_test.py
Uses a throwaway SQLite database in a temp folder; safe to run anytime.
"""

import asyncio
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

os.environ["JOURNAL_DB"] = str(
    Path(tempfile.mkdtemp(prefix="journal_smoke_")) / "smoke.db"
)

import db  # noqa: E402
import journal  # noqa: E402


class FakeChat:
    def __init__(self, log):
        self._log = log

    async def send_message(self, text, reply_markup=None):
        self._log.append(("send", text, reply_markup))


class FakeMessage:
    def __init__(self, text, log):
        self.text = text
        self._log = log

    async def reply_text(self, text, reply_markup=None):
        self._log.append(("reply", text, reply_markup))


class FakeQuery:
    def __init__(self, data, message_text):
        self.data = data
        self.message = type("M", (), {"text": message_text})()
        self.answered = 0
        self.edits = []

    async def answer(self):
        self.answered += 1

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)


class FakeUpdate:
    def __init__(self):
        self.sent = []
        self.effective_chat = FakeChat(self.sent)
        self.message = None
        self.callback_query = None

    def text(self, value):
        self.callback_query = None
        self.message = FakeMessage(value, self.sent)
        return self

    def query(self, data, prompt_text):
        self.message = None
        self.callback_query = FakeQuery(data, prompt_text)
        return self


class FakeContext:
    def __init__(self):
        self.user_data = {}


async def main() -> int:
    failures = []

    def check(cond, label):
        print(("PASS  " if cond else "FAIL  ") + label)
        if not cond:
            failures.append(label)

    db.init_db()
    ctx = FakeContext()
    upd = FakeUpdate()

    # --- button-driven happy path -------------------------------------------
    state = await journal.trade_start(upd.text("/trade"), ctx)
    check(state == journal.SYMBOL, "/trade starts at SYMBOL")

    state = await journal.ask_symbol(upd.text("eurusd"), ctx)
    check(state == journal.DIRECTION and ctx.user_data["symbol"] == "EURUSD",
          "symbol via text -> DIRECTION")
    check("Long or short?" in upd.sent[-1][1], "direction prompt sent")
    check(upd.sent[-1][2] is journal._DIR_KEYBOARD, "direction prompt has buttons")

    upd.query("dir:long", "Long or short?")
    state = await journal.direction_button(upd, ctx)
    check(state == journal.ENTRY, "Long button -> ENTRY")
    check(ctx.user_data["direction"] == "long", "direction stored from button")
    check(upd.callback_query.answered == 1, "callback answered")
    check(any("📈 Long" in e for e in upd.callback_query.edits),
          "choice echoed on old prompt")

    # typed alias still works (checked in its own draft so ctx stays "long")
    ctx_alias = FakeContext()
    await journal.trade_start(upd.text("/trade"), ctx_alias)
    await journal.ask_symbol(upd.text("EURUSD"), ctx_alias)
    state = await journal.ask_direction(upd.text("S"), ctx_alias)
    check(state == journal.ENTRY and ctx_alias.user_data["direction"] == "short",
          "typed alias 'S' -> short -> ENTRY")

    state = await journal.ask_entry(upd.text("1.2345"), ctx)
    check(state == journal.EXIT, "entry -> EXIT")
    state = await journal.ask_exit(upd.text("1.2400"), ctx)
    check(state == journal.SIZE, "exit -> SIZE")
    state = await journal.ask_size(upd.text("2"), ctx)
    check(state == journal.PNL, "size -> PNL")
    check(upd.sent[-1][2] is journal._PNL_KEYBOARD, "P&L prompt shows Auto button")

    state = await journal.ask_pnl(upd.text("abc"), ctx)
    check(state == journal.PNL, "invalid P&L stays at PNL")

    upd.query("pnl:auto", "P&L")
    state = await journal.pnl_button(upd, ctx)
    check(state == journal.TRADE_DATE and ctx.user_data["pnl"] is None,
          "Auto-calculate button -> TRADE_DATE (auto pnl)")

    upd.query("date:today", "Close date (YYYY-MM-DD):")
    state = await journal.trade_date_button(upd, ctx)
    check(state == journal.NOTES
          and ctx.user_data["trade_date"] == date.today().isoformat(),
          "Today button -> NOTES (today stored)")

    upd.query("notes:skip", "Notes (optional):")
    state = await journal.notes_button(upd, ctx)
    check(state == journal.CONFIRM and ctx.user_data["notes"] == "",
          "Skip notes button -> CONFIRM")
    check("Please confirm" in upd.sent[-1][1], "summary shown")
    check(upd.sent[-1][2] is journal._CONFIRM_KEYBOARD,
          "summary shows Save/Discard buttons")
    summary_text = upd.sent[-1][1]
    upd.query("confirm:yes", summary_text)
    state = await journal.confirm_button(upd, ctx)
    check(state == journal.ConversationHandler.END, "Save button -> END")
    check(not ctx.user_data, "draft cleared after save")
    check(any("Saved trade" in e for e in upd.callback_query.edits),
          "save confirmation edited onto summary")

    rows = db.get_recent(1)
    check(len(rows) == 1, "trade written to db")
    if rows:
        row = rows[0]
        expected = (1.24 - 1.2345) * 2  # long
        check(row["symbol"] == "EURUSD" and row["direction"] == "long",
              "db: symbol/direction correct")
        check(abs(row["pnl"] - expected) < 1e-9, "db: auto P&L correct")
        check(row["notes"] == "", "db: empty notes")

    # --- typed fallback path still works -------------------------------------
    ctx2 = FakeContext()
    await journal.trade_start(upd.text("/trade"), ctx2)
    await journal.ask_symbol(upd.text("BTCUSD"), ctx2)
    await journal.ask_direction(upd.text("long"), ctx2)
    await journal.ask_entry(upd.text("50000"), ctx2)
    await journal.ask_exit(upd.text("51000"), ctx2)
    await journal.ask_size(upd.text("0.5"), ctx2)
    state = await journal.ask_pnl(upd.text("123.45"), ctx2)
    check(state == journal.TRADE_DATE and ctx2.user_data["pnl"] == 123.45,
          "typed manual P&L -> TRADE_DATE")
    state = await journal.ask_trade_date(upd.text("2026-02-01"), ctx2)
    check(state == journal.NOTES, "typed date -> NOTES")
    state = await journal.ask_notes(upd.text("breakout"), ctx2)
    check(state == journal.CONFIRM, "typed notes -> CONFIRM")
    state = await journal.save_trade(upd.text("y"), ctx2)
    check(state == journal.ConversationHandler.END, "typed 'y' saves -> END")

    rows = db.get_recent(2)
    check(len(rows) == 2 and rows[0]["symbol"] == "BTCUSD"
          and rows[0]["pnl"] == 123.45,
          "second trade saved with manual P&L")

    # --- discard via button ---------------------------------------------------
    ctx3 = FakeContext()
    await journal.trade_start(upd.text("/trade"), ctx3)
    await journal.ask_symbol(upd.text("AAPL"), ctx3)
    await journal.ask_direction(upd.text("long"), ctx3)
    await journal.ask_entry(upd.text("100"), ctx3)
    await journal.ask_exit(upd.text("101"), ctx3)
    await journal.ask_size(upd.text("1"), ctx3)
    await journal.ask_pnl(upd.text("-"), ctx3)
    await journal.ask_trade_date(upd.text("-"), ctx3)
    state = await journal.ask_notes(upd.text("-"), ctx3)
    check(state == journal.CONFIRM, "typed '-' skip path -> CONFIRM")
    upd.query("confirm:no", "Please confirm:")
    state = await journal.confirm_button(upd, ctx3)
    check(state == journal.ConversationHandler.END and not ctx3.user_data,
          "Discard button -> END, draft cleared")

    # --- conversation wiring (real PTB routing, no network) -------------------
    from datetime import datetime as _dt

    from telegram import CallbackQuery, Chat, Message, Update, User

    conv = journal.build_conversation()
    _user = User(id=1, first_name="T", is_bot=False)
    _chat = Chat(id=1, type=Chat.PRIVATE)

    def _button_update(data):
        msg = Message(message_id=1, date=_dt.now(), chat=_chat, from_user=_user)
        cq = CallbackQuery(id="1", from_user=_user, chat_instance="ci",
                           data=data, message=msg)
        return Update(update_id=1, callback_query=cq)

    def _text_update(text):
        msg = Message(message_id=2, date=_dt.now(), chat=_chat,
                      from_user=_user, text=text)
        return Update(update_id=2, message=msg)

    key = conv._get_key(_button_update("dir:long"))

    def _routed_to(st, update):
        conv._conversations[key] = st
        result = conv.check_update(update)
        conv._conversations.clear()
        return result[2] if result else None

    cases = [
        (journal.DIRECTION, _button_update("dir:long"), journal.direction_button),
        (journal.PNL, _button_update("pnl:auto"), journal.pnl_button),
        (journal.TRADE_DATE, _button_update("date:today"), journal.trade_date_button),
        (journal.NOTES, _button_update("notes:skip"), journal.notes_button),
        (journal.CONFIRM, _button_update("confirm:yes"), journal.confirm_button),
    ]
    for st, update, fn in cases:
        handler = _routed_to(st, update)
        check(getattr(handler, "callback", None) is fn,
              f"PTB routes callback '{update.callback_query.data}' to {fn.__name__}")

    handler = _routed_to(journal.DIRECTION, _text_update("long"))
    check(getattr(handler, "callback", None) is journal.ask_direction,
          "PTB still routes typed text to ask_direction")

    handler = _routed_to(journal.ENTRY, _button_update("cancel"))
    check(getattr(handler, "callback", None) is journal.cancel,
          "PTB routes 'cancel' button to cancel fallback mid-conversation")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


sys.exit(asyncio.run(main()))

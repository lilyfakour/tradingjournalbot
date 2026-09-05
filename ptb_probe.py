"""Real-PTB end-to-end probe: drive the open/close conversations exactly like
Application.process_update does (check_update -> refresh_data -> handle_update)
with a stub Bot, proving no bot-binding errors remain. Throwaway file."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, r"C:\Users\Astrix\Dev\trading")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

tmp = tempfile.mkdtemp(prefix="probe_")
os.environ["JOURNAL_DB"] = os.path.join(tmp, "p.db")

import db  # noqa: E402
import journal  # noqa: E402

db.init_db()

from telegram import (  # noqa: E402
    Bot, CallbackQuery, Chat, Message, Update, User,
)
from telegram.ext import ApplicationBuilder  # noqa: E402


class StubBot(Bot):
    def __init__(self):
        super().__init__(token="123456:TESTTOKEN")
        self._sent = []
        self._deleted = []   # message ids deleted (question messages)
        self._edited = []    # (message_id, new_text) pairs of in-place edits

    @property
    def sent(self):
        return self._sent

    @property
    def username(self):
        return "ProbeBot"  # CommandHandler.check_update parses "@name" mentions

    @property
    def deleted(self):
        return self._deleted

    @property
    def edited(self):
        return self._edited

    async def send_message(self, chat_id=None, text=None, **kw):
        self._n = getattr(self, "_n", 500) + 1
        msg = Message(
            message_id=self._n,
            date=datetime.now(),
            chat=Chat(id=chat_id or 0, type=Chat.PRIVATE),
            text=text,
        )
        msg.set_bot(self)
        self._sent.append(text)
        return msg

    async def edit_message_text(
        self, text=None, chat_id=None, message_id=None, **kw
    ):
        """Morph an existing message in place (returns the Message object)."""
        msg = Message(
            message_id=message_id,
            date=datetime.now(),
            chat=Chat(id=chat_id or 0, type=Chat.PRIVATE),
            text=text,
        )
        msg.set_bot(self)
        self._edited.append((message_id, text))
        return msg

    async def edit_message_reply_markup(
        self, reply_markup=None, chat_id=None, message_id=None, **kw
    ):
        self._edited.append((message_id, None))
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self._deleted.append(message_id)
        return True

    async def answer_callback_query(self, callback_query_id=None, **kw):
        return True

    async def set_my_commands(self, commands=None, **kw):
        self._commands = list(commands or [])
        return True

    async def set_chat_menu_button(self, **kw):
        return True


def tap(stub, user, chat, data, oid=1):
    msg = Message(message_id=oid, date=datetime.now(), chat=chat, from_user=user)
    msg.set_bot(stub)
    cq = CallbackQuery(
        id=f"q{oid}", from_user=user, chat_instance="ci", data=data, message=msg
    )
    cq.set_bot(stub)
    upd = Update(update_id=oid, callback_query=cq)
    upd.set_bot(stub)
    return upd


def tap_and_run(conv, app, user, data, oid):
    """Drive an inline-button tap through real conversation dispatch."""
    upd = tap(app.bot, user, Chat(id=user.id, type=Chat.PRIVATE), data, oid)
    res = conv.check_update(upd)
    assert res, f"tap {data!r} not routed at state {conv._conversations}"
    ctx = app.context_types.context.from_update(upd, app)
    ctx._user_id = user.id
    ctx._chat_id = user.id
    return ctx


def text_up(stub, user, chat, text, oid=99):
    msg = Message(
        message_id=oid, date=datetime.now(), chat=chat, from_user=user, text=text
    )
    msg.set_bot(stub)
    upd = Update(update_id=oid, message=msg)
    upd.set_bot(stub)
    return upd


async def main():
    stub = StubBot()
    app = ApplicationBuilder().bot(stub).build()
    user = User(id=7, first_name="P", is_bot=False)
    chat = Chat(id=7, type=Chat.PRIVATE)
    chat.set_bot(stub)

    conv = journal.build_open_conversation()
    key = (user.id, user.id)

    def stored(c):
        return c._conversations.get(key)

    # 1) ➕ tap -> real conversation entry -> market question
    upd = tap(stub, user, chat, "opn:add", 1)
    res = conv.check_update(upd)
    assert res, "➕ tap did not match the open conversation"
    ctx = app.context_types.context.from_update(upd, app)
    await ctx.refresh_data()
    await conv.handle_update(upd, app, res, ctx)
    assert stored(conv) == journal.OPEN_MARKET, dict(conv._conversations)
    assert any("کدام بازار" in t for t in stub.sent), stub.sent
    print("PASS  tap opn:add -> questionnaire started (state OPEN_MARKET)")

    # 2) market answer flows through the real conversation
    upd2 = text_up(stub, user, chat, "🪙 Crypto", 2)
    res2 = conv.check_update(upd2)
    assert res2, "market answer not routed"
    ctx2 = app.context_types.context.from_update(upd2, app)
    await ctx2.refresh_data()
    await conv.handle_update(upd2, app, res2, ctx2)
    assert stored(conv) == journal.OPEN_SYMBOL, dict(conv._conversations)
    assert any("نماد" in t or "Symbol" in t for t in stub.sent), stub.sent
    print("PASS  market answer -> state OPEN_SYMBOL via real conversation")

    # 3) 🏁 tap -> close conversation entry -> status question
    oid = db.add_open_trade(
        symbol="BTCUSD", direction="long", market="crypto", timeframe="1h",
        reason="probe", screenshot=None, trade_date="2026-09-04",
        entry_time="10:00", risk_percent=1.0, entry_price=50000.0,
        take_profit=51000.0, stop_loss=49000.0,
    )
    cconv = journal.build_close_conversation()
    upd3 = tap(stub, user, chat, f"opn:c:{oid}", 3)
    r3 = cconv.check_update(upd3)
    assert r3, "🏁 tap did not match the close conversation"
    ctx3 = app.context_types.context.from_update(upd3, app)
    await ctx3.refresh_data()
    await cconv.handle_update(upd3, app, r3, ctx3)
    assert stored(cconv) == journal.CLOSE_STATUS, dict(cconv._conversations)
    assert ctx3.user_data.get("open_id") == oid, ctx3.user_data
    assert any("نتیجه" in t for t in stub.sent), stub.sent
    print("PASS  tap opn:c:<id> -> close flow started (state CLOSE_STATUS)")

    # 4) full close chain through the REAL conversation — regression for the
    #    mood bug (ask_close_reason used to return the /trade MOOD state,
    #    which left the close conversation unroutable) AND for the new
    #    "typed dollar result + typed ROI percent" model.
    async def step(text, oid):
        m = Message(
            message_id=oid, date=datetime.now(), chat=chat, from_user=user,
            text=text,
        )
        m.set_bot(stub)
        u = Update(update_id=oid, message=m)
        u.set_bot(stub)
        r = cconv.check_update(u)
        assert r, f"no route for {text!r} at state {cconv._conversations.get((user.id, user.id))}"
        cx = app.context_types.context.from_update(u, app)
        await cx.refresh_data()
        await cconv.handle_update(u, app, r, cx)
        return cconv._conversations.get((user.id, user.id))

    assert await step("✅ Win", 11) == journal.CLOSE_AMOUNT
    assert await step("37.5", 12) == journal.CLOSE_ROI
    assert await step("-", 13) == journal.CLOSE_DATE  # ⏭ skip the ROI percent
    assert await step("2026-09-04", 14) == journal.CLOSE_HOUR
    assert await step("الان", 15) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 16) == journal.CLOSE_REASON
    state_after_reason = await step("TP tapped, momentum gone", 17)
    assert state_after_reason == journal.CLOSE_MOOD, state_after_reason
    print("PASS  reason -> CLOSE_MOOD through the real conversation (mood bug fixed)")
    state_after_mood = await step("آرام", 18)
    assert state_after_mood == journal.CLOSE_CONFIRM, state_after_mood
    print("PASS  mood -> CLOSE_CONFIRM through the real conversation")
    state_after_save = await step("✅ ثبت", 19)
    assert state_after_save is None, state_after_save
    closed = db.get_recent(1)[0]
    assert closed["hit"] == "win" and closed["symbol"] == "BTCUSD", dict(closed)
    # The typed dollar amount (37.5) is stored EXACTLY as given — no margin
    # math anywhere, and the skipped ROI percent stays NULL.
    assert closed["pnl"] == 37.5, dict(closed)
    assert closed["roi"] is None, dict(closed)
    print("PASS  confirm -> saved into history with the typed P&L (37.5 $)")

    # 5) the recent-detail card of the just-closed two-phase trade renders —
    #    regression for the reported "click on a two-phase trade does nothing".
    detail = journal._recent_detail_text(closed)
    assert "سود و زیان: <b>+$37.50</b>" in detail, detail
    assert "دلیل ورود" in detail and "دلیل خروج" in detail, detail
    assert "اهرم" not in detail, detail
    print("PASS  recent detail of the two-phase trade renders (NULL-safe)")

    # 6) close with a margin on the open trade — the margin is info-only;
    #    the budget shifts by the typed P&L; BE skips the amount question.
    oid2 = db.add_open_trade(
        symbol="ETHUSD", direction="short", market="crypto", timeframe="15m",
        reason="probe", screenshot=None, trade_date="2026-09-04",
        entry_time="11:00", risk_percent=2.0, entry_price=3000.0,
        take_profit=2900.0, stop_loss=3100.0, margin=50.0,
    )
    db.set_budget(500.0)
    upd6 = tap(stub, user, chat, f"opn:c:{oid2}", 20)
    r6 = cconv.check_update(upd6)
    assert r6, "🏁 tap for the margin trade did not match"
    ctx6 = app.context_types.context.from_update(upd6, app)
    await ctx6.refresh_data()
    await cconv.handle_update(upd6, app, r6, ctx6)
    assert ctx6.user_data.get("open_margin") == 50.0, ctx6.user_data
    assert await step("✅ Win", 21) == journal.CLOSE_AMOUNT
    assert await step("16.67", 22) == journal.CLOSE_ROI
    assert await step("2.5", 23) == journal.CLOSE_DATE  # typed ROI percent
    assert await step("2026-09-04", 24) == journal.CLOSE_HOUR
    assert await step("الان", 25) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 26) == journal.CLOSE_REASON
    assert await step("TP tapped", 27) == journal.CLOSE_MOOD
    assert await step("آرام", 28) == journal.CLOSE_CONFIRM
    assert await step("✅ ثبت", 29) is None
    closed2 = db.get_recent(1)[0]
    # The typed 16.67 $ and the typed 2.5 % are stored as-is (Win signs the
    # percent positive; nothing is derived from the margin).
    assert closed2["pnl"] == 16.67, dict(closed2)
    assert closed2["roi"] == 2.5, dict(closed2)
    assert closed2["size"] == 50.0, dict(closed2)
    assert closed2["leverage"] is None, dict(closed2)
    print("PASS  close stores the typed P&L + typed ROI (16.67 $ / 2.5 %)")
    # Budget moved by the typed P&L: it was set to 500 $ after the first
    # close, so only this close's 16.67 counts: 500 + 16.67 = 516.67.
    assert db.get_budget() == 516.67, db.get_budget()
    print("PASS  budget shifted by the typed P&L (500 -> 516.67 $)")

    # 6b) BE close: the amount question is skipped entirely.
    oid3 = db.add_open_trade(
        symbol="XAUUSD", direction="long", market="forex", timeframe="5m",
        reason="probe", screenshot=None, trade_date="2026-09-04",
        entry_time="12:00", risk_percent=1.0, entry_price=2500.0,
        take_profit=2520.0, stop_loss=2480.0,
    )
    upd_be = tap(stub, user, chat, f"opn:c:{oid3}", 30)
    r_be = cconv.check_update(upd_be)
    assert r_be
    cx_be = app.context_types.context.from_update(upd_be, app)
    await cx_be.refresh_data()
    await cconv.handle_update(upd_be, app, r_be, cx_be)
    assert await step("➖ BE", 31) == journal.CLOSE_DATE, "BE must skip the amount"
    assert await step("2026-09-04", 32) == journal.CLOSE_HOUR
    assert await step("⏭ رد کردن", 33) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 34) == journal.CLOSE_REASON
    assert await step("دست نزدم بهش", 35) == journal.CLOSE_MOOD
    assert await step("مطمئن", 36) == journal.CLOSE_CONFIRM
    assert await step("✅ ثبت", 37) is None
    closed3 = db.get_recent(1)[0]
    assert closed3["pnl"] == 0.0 and closed3["hit"] == "be", dict(closed3)
    print("PASS  BE close skips the amount question and stores 0.0 $")

    # 7) the /trade flow through INLINE taps — the new primary interaction
    #    (menu entry, per-tap question deletion, inline cancel).
    tconv = journal.build_conversation()
    tkey = tconv._get_key(text_up(stub, user, chat, "long", 30))

    async def tstep(data, oid, text=None):
        if data is not None:
            u = tap(stub, user, chat, data, oid)
        else:
            u = text_up(stub, user, chat, text, oid)
        r = tconv.check_update(u)
        assert r, f"no route for {data or text!r} at {tconv._conversations.get(tkey)}"
        cx = app.context_types.context.from_update(u, app)
        await cx.refresh_data()
        await tconv.handle_update(u, app, r, cx)
        return tconv._conversations.get(tkey), cx

    st, cx = await tstep("menu:trade", 31)
    assert st == journal.MARKET and cx.user_data.get("_flow_q"), (st, cx.user_data)
    fq = cx.user_data["_flow_q"]
    st, cx = await tstep("q:🪙 Crypto", fq)
    assert st == journal.SYMBOL and fq in stub.deleted, (st, stub.deleted)
    print("PASS  menu:trade tap -> MARKET; market tap deletes its question")

    st, cx = await tstep(None, 32, text="EURUSD")
    assert st == journal.DIRECTION, st
    st, cx = await tstep("q:📈 Long", 33)
    assert st == journal.TIMEFRAME, st
    print("PASS  typed symbol and 📈 Long tap route through real dispatch")

    st, cx = await tstep("q:cancel", 34)
    assert st is None and cx.user_data.get("_flow_q") is None, (st, cx.user_data)
    assert 34 in stub.deleted
    print("PASS  ✖️ لغو tap ends the /trade flow and deletes the question")

    # 8) /start end-to-end through real PTB dispatch. Regression: the menu
    # text used to contain "<id>", which Telegram's HTML parser rejects —
    # /start failed silently with "can't parse entities".
    from telegram import MessageEntity
    from telegram.ext import CommandHandler

    handler = CommandHandler("start", journal.show_menu)

    def start_up(oid):
        msg = Message(
            message_id=oid,
            date=datetime.now(),
            chat=chat,
            from_user=user,
            text="/start",
            entities=[
                MessageEntity(
                    type=MessageEntity.BOT_COMMAND, offset=0, length=6
                )
            ],
        )
        msg.set_bot(stub)
        upd = Update(update_id=oid, message=msg)
        upd.set_bot(stub)
        return upd

    async def run_start_full(upd):
        res = handler.check_update(upd)
        assert res, "/start not matched by CommandHandler"
        cx = app.context_types.context.from_update(upd, app)
        await cx.refresh_data()
        await handler.handle_update(upd, app, res, cx)

    await run_start_full(start_up(40))
    assert any("👋" in t for t in stub.sent), "menu text not sent"
    # the one-time reply-bar killer went out silently ("…") and was deleted
    assert not any("رابط جدید" in t for t in stub.sent), stub.sent
    print("PASS  /start -> inline menu sent (HTML-safe) + silent bar removal")

    n = len(stub.sent)
    n_edits = len(stub.edited)
    await run_start_full(start_up(41))
    added = stub.sent[n:]
    # Second /start EDITS the existing menu message in place (morph) —
    # no duplicate menu message is stacked into the chat.
    assert not added, added
    assert len(stub.edited) > n_edits, stub.edited
    print("PASS  second /start morphs the menu message in place (no re-send)")

    # 10) the morphing-screen navigation: settings -> budget -> 🔙 back to
    # settings -> 🏠 home — ALL via in-place edits of ONE message (regression
    # for the "delete + re-send" menu navigation the trader complained about).
    nav_ctx = app.context_types.context.from_update(start_up(42), app)
    await nav_ctx.refresh_data()
    await journal._send_settings_screen(start_up(42), nav_ctx)
    settings_mid = nav_ctx.user_data["_screens"][journal._SCREEN_NAV_KEY]
    await journal._send_budget_screen(start_up(42), nav_ctx)
    # Same message id — the budget screen MORPHED the settings message.
    assert (
        nav_ctx.user_data["_screens"][journal._SCREEN_NAV_KEY] == settings_mid
    ), nav_ctx.user_data["_screens"]
    nav_upd = tap(stub, user, chat, "nav:back", 43)
    nres = journal.build_nav_callbacks().check_update(nav_upd)
    assert nres, "nav:back tap not routed"
    nctx = app.context_types.context.from_update(nav_upd, app)
    await nctx.refresh_data()
    await journal.on_nav_callback(nav_upd, nctx)
    assert any(
        mid == settings_mid and "تنظیمات" in (txt or "")
        for mid, txt in stub.edited
    ), stub.edited
    nav_upd2 = tap(stub, user, chat, "nav:home", 44)
    nctx2 = app.context_types.context.from_update(nav_upd2, app)
    await nctx2.refresh_data()
    await journal.on_nav_callback(nav_upd2, nctx2)
    assert settings_mid in stub.deleted, stub.deleted
    print("PASS  🔙 re-renders the previous screen, 🏠 returns home (morph, not re-send)")

    # 9) bot.py's real handler registration + post_init must EXECUTE cleanly
    # (regression: a missing import only exploded at startup — invisible to
    # py_compile and to plain module imports). The ☰ Menu button must be
    # registered with the full command list again.
    import bot as bot_module

    app2 = (
        ApplicationBuilder()
        .bot(stub)
        .post_init(bot_module.post_init)
        .build()
    )
    bot_module._register_handlers(app2)
    registered = sum(len(g) for g in app2.handlers.values())
    assert registered >= 10, registered
    await bot_module.post_init(app2)
    assert stub._commands and stub._commands[0].command == "start", stub._commands
    print(
        f"PASS  bot.py: {registered} handlers registered, "
        f"☰ Menu button with {len(stub._commands)} commands"
    )

    print("ALL PROBES PASSED")


asyncio.run(main())

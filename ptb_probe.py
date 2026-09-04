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

    @property
    def sent(self):
        return self._sent

    @property
    def username(self):
        return "ProbeBot"  # CommandHandler.check_update parses "@name" mentions

    @property
    def deleted(self):
        return self._deleted

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

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self._deleted.append(message_id)
        return True

    async def answer_callback_query(self, callback_query_id=None, **kw):
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
    upd2 = text_up(stub, user, chat, "🪙 کریپتو", 2)
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
    #    which left the close conversation unroutable).
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

    assert await step("✅ Win (TP)", 11) == journal.CLOSE_DATE
    assert await step("2026-09-04", 12) == journal.CLOSE_HOUR
    assert await step("الان", 13) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 14) == journal.CLOSE_REASON
    state_after_reason = await step("TP tapped, momentum gone", 15)
    assert state_after_reason == journal.CLOSE_MOOD, state_after_reason
    print("PASS  reason -> CLOSE_MOOD through the real conversation (mood bug fixed)")
    state_after_mood = await step("آرام", 16)
    assert state_after_mood == journal.CLOSE_CONFIRM, state_after_mood
    print("PASS  mood -> CLOSE_CONFIRM through the real conversation")
    state_after_save = await step("✅ ثبت", 17)
    assert state_after_save is None, state_after_save
    closed = db.get_recent(1)[0]
    assert closed["hit"] == "win" and closed["symbol"] == "BTCUSD", dict(closed)
    print("PASS  confirm -> saved into history, conversation ended")

    # 5) the recent-detail card of the just-closed two-phase trade renders —
    #    regression for the reported "click on a two-phase trade does nothing".
    detail = journal._recent_detail_text(closed)
    assert "مارجین: —" in detail and "سود و زیان: <b>—</b>" in detail, detail
    assert "دلیل ورود" in detail and "دلیل خروج" in detail, detail
    print("PASS  recent detail of the two-phase trade renders (NULL-safe)")

    # 6) budget feature: an open trade WITH a margin closed through the real
    #    conversation stores real P&L/ROI (margin × price-move), not NULL.
    oid2 = db.add_open_trade(
        symbol="ETHUSD", direction="short", market="crypto", timeframe="15m",
        reason="probe", screenshot=None, trade_date="2026-09-04",
        entry_time="11:00", risk_percent=2.0, entry_price=3000.0,
        take_profit=2900.0, stop_loss=3100.0, margin=50.0, leverage=10.0,
    )
    upd6 = tap(stub, user, chat, f"opn:c:{oid2}", 20)
    r6 = cconv.check_update(upd6)
    assert r6, "🏁 tap for the margin trade did not match"
    ctx6 = app.context_types.context.from_update(upd6, app)
    await ctx6.refresh_data()
    await cconv.handle_update(upd6, app, r6, ctx6)
    assert ctx6.user_data.get("open_margin") == 50.0, ctx6.user_data
    assert ctx6.user_data.get("open_leverage") == 10.0, ctx6.user_data
    assert await step("✅ Win (TP)", 21) == journal.CLOSE_DATE
    assert await step("2026-09-04", 22) == journal.CLOSE_HOUR
    assert await step("الان", 23) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 24) == journal.CLOSE_REASON
    assert await step("TP tapped", 25) == journal.CLOSE_MOOD
    assert await step("آرام", 26) == journal.CLOSE_CONFIRM
    assert await step("✅ ثبت", 27) is None
    closed2 = db.get_recent(1)[0]
    # short 3000 -> 2900, 50 margin, 10x leverage:
    # (3000-2900)/3000*50*10 = 16.67 USD, ROI from the rounded pnl: 33.34 %
    assert closed2["pnl"] == 16.67 and closed2["roi"] == 33.34, dict(closed2)
    assert closed2["size"] == 50.0 and closed2["leverage"] == 10.0, dict(closed2)
    print("PASS  margin-aware close stores real P&L/ROI (16.67 $ / 33.34 %)")

    detail2 = journal._recent_detail_text(closed2)
    assert "مارجین: 50" in detail2, detail2
    assert "+$16.67" in detail2 and "+33.34%" in detail2, detail2
    assert "10x" in detail2, detail2
    print("PASS  detail card shows the real margin, leverage and P&L")

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
    st, cx = await tstep("q:🪙 کریپتو", fq)
    assert st == journal.SYMBOL and fq in stub.deleted, (st, stub.deleted)
    print("PASS  menu:trade tap -> MARKET; market tap deletes its question")

    st, cx = await tstep(None, 32, text="EURUSD")
    assert st == journal.DIRECTION, st
    st, cx = await tstep("q:📈 Long", 33)
    assert st == journal.LEVERAGE, st
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
    # the one-time reply-bar-removal confirmation went out
    assert any("رابط جدید" in t for t in stub.sent), stub.sent
    print("PASS  /start -> inline menu sent (HTML-safe) + stale bar removed")

    n = len(stub.sent)
    await run_start_full(start_up(41))
    added = stub.sent[n:]
    assert not any("رابط جدید" in t for t in added), added
    assert any("👋" in t for t in added), added
    print("PASS  second /start re-sends the menu without the cleanup message")

    print("ALL PROBES PASSED")


asyncio.run(main())

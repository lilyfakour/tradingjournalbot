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

    @property
    def sent(self):
        return self._sent

    async def send_message(self, chat_id=None, text=None, **kw):
        self._sent.append(text)
        return None

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
        take_profit=2900.0, stop_loss=3100.0, margin=50.0,
    )
    upd6 = tap(stub, user, chat, f"opn:c:{oid2}", 20)
    r6 = cconv.check_update(upd6)
    assert r6, "🏁 tap for the margin trade did not match"
    ctx6 = app.context_types.context.from_update(upd6, app)
    await ctx6.refresh_data()
    await cconv.handle_update(upd6, app, r6, ctx6)
    assert ctx6.user_data.get("open_margin") == 50.0, ctx6.user_data
    assert await step("✅ Win (TP)", 21) == journal.CLOSE_DATE
    assert await step("2026-09-04", 22) == journal.CLOSE_HOUR
    assert await step("الان", 23) == journal.CLOSE_PHOTOS
    assert await step("⏭ بدون اسکرین‌شات", 24) == journal.CLOSE_REASON
    assert await step("TP tapped", 25) == journal.CLOSE_MOOD
    assert await step("آرام", 26) == journal.CLOSE_CONFIRM
    assert await step("✅ ثبت", 27) is None
    closed2 = db.get_recent(1)[0]
    # short 3000 -> 2900 with 50 margin: (3000-2900)/3000*50 = 1.67 USD,
    # ROI from the rounded pnl: 1.67/50*100 = 3.34 %
    assert closed2["pnl"] == 1.67 and closed2["roi"] == 3.34, dict(closed2)
    assert closed2["size"] == 50.0, dict(closed2)
    print("PASS  margin-aware close stores real P&L/ROI (1.67 $ / 3.34 %)")

    detail2 = journal._recent_detail_text(closed2)
    assert "مارجین: 50" in detail2, detail2
    assert "+$1.67" in detail2 and "+3.34%" in detail2, detail2
    print("PASS  detail card shows the real margin and P&L")

    print("ALL PROBES PASSED")


asyncio.run(main())

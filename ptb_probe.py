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

    print("ALL PROBES PASSED")


asyncio.run(main())

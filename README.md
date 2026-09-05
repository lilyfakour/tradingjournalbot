# Telegram Trading Journal

A personal Telegram bot that logs closed trades into a local SQLite database —
and also tracks **open trades**: register a position when you enter it, close
it through the bot later, and it moves into your history.

## Language / زبان

The bot's UI is in **Persian** (رسمی — polite register) with **technical
market terms in English** (Entry, Take Profit, Stop Loss, Margin,
Risk, Win/Loss/BE, P&L, ROI, Win rate, Profit Factor, Long, Short). Face-style
emojis were removed for a professional look. Slash commands stay English
(`/trade`, `/stats`, …), and typed English answers (`l`/`s`, `win`/`lose`/`be`,
`calm`, `daily`, `y`/`n`, `-`) still work as aliases. Where the trader has to
type English (symbols, dates, `/stats` arguments) the hint line is shown in
English so RTL/LTR mixing never scrambles it. Database values are unchanged
(`long`/`short`, mood keys like `calm`, timeframes like `1h`), so existing
data and the spreadsheet column layout keep working.

## Commands

| Command | What it does |
| --- | --- |
| `/trade` | 📈 **ثبت معامله بسته‌شده** (after you have exited): بازار (🪙 کریپتو / 💵 فارکس) -> symbol (recent / most-used as buttons) -> 📈 Long / 📉 Short -> timeframe -> Entry -> 🎯 Take Profit -> 🛑 Stop Loss -> نتیجه (✅ Win / ❌ Loss / ➖ BE) -> 💵 **سود/ضرر چند دلار؟** (the exact dollar amount is stored as-is) -> 💰 Margin (skippable with ⏭) -> ⚠️ Risk % (skippable) -> date (📅 امروز) -> hour (**🕐 الان** fills the current time) -> دلیل ورود -> Mood -> 📸 screenshot before -> 📸 screenshot after -> ✅ ذخیره / ❌ ثبت نشود. **No leverage anywhere** — what you type is what gets saved; ROI appears when a margin is given |
| `/recent` | Paginated **inline panel** — **one button per trade** (result emoji + id + pair + P&L or —, 📷 if screenshots, 🔁 if it was a two-phase open→close trade) with ◀️/▶️ paging, an All/1W/1M range filter, and the shared 🔙/🏠 row. Tapping a trade **sends a separate, airy detail message** (all fields with bullets, emoji labels and blank lines) carrying 📷 عکس چارت (entry before/after shots) + 📸 عکس‌های خروج (the up-to-4 exit shots of two-phase trades) + 🗑 Delete + ❌ Close; deleting morphs the screen back into the refreshed panel |
| `/open` | 🟢 **ثبت معامله باز جدید** — a dedicated button on the main menu (📈 ثبت معامله بسته · **🟢 ثبت معامله باز**) starts the open-trade questionnaire straight away (and always from scratch — no resume): بازار → symbol → Long/Short → timeframe → دلیل ورود → 📸 entry screenshot (optional) → entry date → entry hour (**🕐 الان** fills the current time) → Risk → 💰 Margin (**🧮 محاسبه خودکار** = بودجه × ریسک٪, or ⏭ رد کردن to leave it empty) → Entry → 🎯 TP → 🛑 SL → confirm. Saved to «معاملات باز», **not** the history |
| `/opens` | 🟢 **Open trades panel** — one button per running position (entry price + 📷 mark) with ◀️/▶️ paging, **➕ ثبت معامله باز** to start the questionnaire, and the shared 🔙/🏠 row. Tapping a trade sends its detail card |
| `/close <id>` | 🏁 Close an open trade (same flow as the 🏁 button on its detail card): status (✅ Win/TP · ❌ Loss/SL · ➖ BE · ✏️ Manual exit) → 💵 **سود/ضرر چند دلار؟** (skipped for BE) → exit date → exit time (**🕐 الان** fills the current time) → exit price (**auto-filled** for TP/SL/BE) → up to **4 exit screenshots** → دلیل خروج → Mood → confirm; the trade then moves into the normal history, `/recent` and `/stats` |
| `/stats` | Filterable performance panel with **inline buttons attached to the message** (period, symbol, reset, export); `/stats BTCUSD 1w` style arguments still work |
| `/settings` | ⚙️ **تنظیمات** — the account **budget in USD** (💰 بودجه). The budget feeds the 🧮 محاسبه خودکار margin calculation of the open questionnaire **and moves by every closed trade's P&L** (profit ➕ / loss ➖) so it stays the live account size; `حذف` clears it |
| `/delete <id>` | Delete a trade, e.g. `/delete 12` |
| `/export` | Download all trades as an `.xlsx` spreadsheet |
| `/cancel` | Abort the current `/trade` entry |

## Inline buttons

Inside `/trade` (and the open/close questionnaires) every choice appears as an
**inline button attached to the bot's own message**:

- **بازار** — 🪙 کریپتو / 💵 فارکس (first step; the margin question adapts:
  USDT vs USD حساب فارکس)
- Your **most used** and **recently traded** symbols (up to 8, top row = most used). With no trade history yet the bot simply asks you to type one.
- 📈 Long / 📉 Short (typed `l`/`s`/`buy`/`sell` still work)
- Leverage: `×2 ×3 ×5 ×10 ×20 ×50 ×100 ×125`, typed (`10`, `10x`) or ⏭ بدون اهرم
- Timeframe: `1m 5m 15m 30m 1h 4h 1D 1W 1M` (or type any timeframe like `45m`)
- Entry, 🎯 Take Profit (TP), 🛑 Stop Loss (SL) — typed prices
- نتیجه معامله — **✅ Win / ❌ Loss / ➖ BE** (Win sets exit = TP, Loss sets
  exit = SL, BE sets exit = entry; the result also drives the auto P&L)
- 💰 Margin — typed amount, or 🧮 محاسبه خودکار (budget × risk %)
- ⚠️ Risk — the percent of your account risked (`0.5% 1% 2% 3% 5% 10%`,
  typed values, or ⏭ بدون درصد). **P&L is auto-calculated** from margin,
  leverage and the exit price — no extra question
- 📅 امروز for the date (or type `2026-02-09 14:30` in one go), then the
  trade hour — hour buttons, 🕐 الان, `HH`/`HH:MM` typed, or ⏭ رد کردن
- Mood while making the trade — آرام · مطمئن · مضطرب · طمع · FOMO ·
  انتقامی (plain professional labels; tap one, type it, or ⏭ رد کردن);
  stored with the trade and shown in `/recent` and the stats breakdown
- 📝 دلیل ورود (why you entered) instead of a generic notes field
- ⏭ بدون دلیل / ⏭ بدون اسکرین‌شات
- ✅ ذخیره / ❌ ثبت نشود
- ✖️ لغو on every question (or send `/cancel`)

**Each question message is deleted as soon as it is answered** — the chat
stays clean, one question at a time. Tapping a button and typing the same
answer are equivalent (`l`/`s` for direction, `win`/`lose`/`be`/`breakeven`
for the result, `5min`/`daily`/`weekly` for timeframes, `-` as the universal
skip shortcut). The old reply-keyboard bar under the input field is gone.

## Result questions (no leverage, no auto-computed P&L)

- **No leverage question anywhere.** The trader records what actually
  happened: after picking the result the bot asks **💵 چند دلار سود یا ضرر
  کردی؟** and stores exactly that amount (Win ➕ / Loss ➖, BE stores 0 and
  skips the question).
- **Margin is optional** — ⏭ رد کردن in both `/trade` and `/open` leaves it
  empty. A stored margin gives **ROI = P&L ÷ margin**; without one ROI simply
  stays unknown.
- **The budget is live**: every closed trade moves the ⚙️ تنظیمات budget by
  its P&L, and the close confirmation shows «بودجهٔ جدید».

## Menu

- The ☰ **Menu button** next to the message bar is **registered on every
  start** with the command list (start, opens, recent, stats, settings,
  export, delete, cancel) — Telegram's own command menu, separate from the
  inline buttons on the messages.
- `/start` sends the **main menu as a message with inline buttons**: 📈 ثبت
  معامله بسته · 🟢 ثبت معامله باز / 🟢 معاملات باز · 📊 آمار / 🕘 معاملات اخیر ·
  📥 اکسل / ⚙️ تنظیمات. The closed-trade button is labeled «ثبت معامله بسته» —
  it logs a trade that was already exited (/trade) — so it can never be
  confused with the open-trade one. The menu message **stays in place** while
  secondary screens open on top of it, and 📈 / 🟢 safely restart their
  questionnaires even mid-entry.
  Every menu button also carries a **Telegram button style**
  (`KeyboardButtonStyle`, Bot API 10.0 / PTB 22.7+): blue pills for
  📈 ثبت معامله بسته · 📊 آمار · 🕘 معاملات اخیر · ⚙️ تنظیمات and green pills
  for 🟢 ثبت معامله باز · 🟢 معاملات باز · 📥 اکسل — current Telegram apps draw
  them as colored buttons, older clients just show them unstyled.
- The **first menu after (re)starting the bot** also removes the persistent
  reply-keyboard bar of the old UI — **silently**: one message with the
  `ReplyKeyboardRemove` flag is sent and instantly deleted, so the bar is
  dropped without any notification appearing in the chat.
- **Morphing navigation:** every secondary screen (⚙️ تنظیمات → 💰 بودجه, and
  the 📊/🕘/🟢 panels including the stats symbol picker) is **one message per
  chat** that is **edited in place** as the trader moves around — drilling
  into 💰 was never a delete + re-send. Every screen carries a shared
  **🔙 (one level back)** / **🏠 (straight to the main menu)** row; the main
  menu itself owns its own persistent message.
- **Fresh flows every time:** starting 🟢 ثبت معامله باز always wipes any
  half-finished draft and restarts from the market question — an abandoned
  questionnaire can never resume from an old step.

## Stats panel

`/stats` sends the report as a message with the **filter buttons attached to
the message itself** (inline keyboard). Tapping a filter edits that same
message in place — no new messages stack up, and the buttons always sit right
under the numbers they control.

- The buttons are **filters only** — they change the *period* and the
  *symbol* of the numbers above, nothing else (not timeframes, not trades).
- **Periods** — `1W · 1M · 3M · 6M · 1Y · All`; the active period is marked
  with ✓.
- **🔤 Symbols** — **morphs the panel message itself** into the picker (🔙
  brings the panel straight back): 10 symbols per page, sorted by your **most
  recent trade** first, with ◀️/▶️ page navigation, a page counter, and a
  «همه نمادها» button. Tapping a symbol filters the panel;
  tapping the active symbol again clears it.
- **♻️ Reset** clears all filters in one tap. **📤 Export** sends the
  `.xlsx` spreadsheet without leaving the panel.

The command also takes arguments, combined or alone:

```
/stats            all symbols, all time (keeps your last button filter)
/stats BTCUSD     one symbol, all time
/stats 3m         all symbols, last 90 days
/stats BTCUSD 1w  one symbol, last 7 days
```

Each panel shows the trade count, Win/Loss/BE, win rate, total/avg P&L,
**Avg ROI**, profit factor, best/worst (آمار معاملات، نرخ برد، سود/زیان و
بازده کل و میانگین، فاکتور سود، بهترین/بدترین) — technical keys stay in
English so the numbers never get scrambled by RTL text — plus a **Mood**
breakdown (trades, total P&L and win rate per mood) whenever mood data exists
in the filtered range.

## Export

`/export` sends every logged trade as an Excel workbook (`.xlsx`) — one row
per trade with id, symbol, market (crypto/forex), direction, timeframe,
entry, the price that hit, take profit, stop loss, the result
(Win/Loss/BE), margin, risk %, P&L, **ROI %**, date, mood, reason
for entry and creation time. The header row is
frozen and filterable, and the price/size/P&L/ROI columns stay **numeric** so
you can pivot, chart and do math directly in Excel or Google Sheets. Trades
logged before ROI was tracked get it back-computed from P&L / margin. The
file is generated on demand, sent as a document, and not kept on disk
(override the temp folder with the `EXPORT_DIR` environment variable).

## Screenshots

Near the end of the questionnaire the bot asks for **two** chart screenshots —
one from **before the trade** (your setup) and one from **after** (the result).
Send a photo (or an image as a file) at each step, or skip either one; both are
downloaded to the `screenshots/` folder next to `bot.py` and linked to the
trade. `/recent` marks trades that have one with 📷, and the detail card of
such a trade carries a **📷 عکس چارت** button that re-sends them (captioned
قبل/بعد). `/delete` removes the files.
Override the folder with the `SCREENSHOT_DIR` environment variable.

## Open trades (two-phase journaling)

The 🟢 معاملات باز button (or `/open` / `/opens`) opens a panel styled exactly like
`/recent`: one button per running position (entry price + 📷 mark), an
**➕ ثبت معامله باز** button on top, ◀️/▶️ paging and 🏠 Home. Tapping a trade
sends its detail card — entry, TP, SL, risk, date/time, entry reason and a
📷 button for the entry chart — carrying **🏁 Close trade**, 🗑 حذف and
❌ بستن. For a straightforward start there is also a dedicated
**🟢 ثبت معامله باز** button on the main menu that jumps straight into the
questionnaire.

- **Phase 1 — register:** tap **🟢 ثبت معامله باز** on the main menu (or ➕ on
  the 🟢 panel, or `/open`). The questionnaire asks بازار، نماد، جهت،
  تایم‌فریم، دلیل ورود، 📸 اسکرین‌شات ورود (اختیاری)، تاریخ ورود، ساعت ورود
  (دکمه **🕐 الان** زمان همین حالا را ثبت می‌کند)، ⚠️ Risk، 💰 Margin، Entry،
  🎯 TP و 🛑 SL — in that order — then shows the confirmation
  summary and saves the position to «معاملات باز». There is **no leverage
  question**; the margin step can be skipped with ⏭ رد کردن.
- **Phase 2 — close:** later, tap the trade in the 🟢 panel and press
  **🏁 Close trade** (or send `/close <id>`). The bot asks the status
  (✅ Win/TP · ❌ Loss/SL · ➖ BE · ✏️ Manual exit), the **dollar result**
  (skipped for BE), the exit date and the exit time (دکمه **🕐 الان**) as two
  separate questions, then the exit price —
  **auto-filled from TP/SL/entry for the first three statuses** — up to
  **4 exit screenshots** (one per message, skippable), the reason for exiting
  and the mood. After
  the confirmation the position leaves the open list and appears in
  `/recent`, `/stats` and the Excel export.

The 💰 margin question (budget feature): type the committed USD amount, tap
**🧮 محاسبه خودکار** so the bot computes it from the configured budget —
`مارجین = بودجه × ریسک٪ ÷ ۱۰۰` (⚙️ تنظیمات → 💰 بودجه sets the budget) — or
**⏭ رد کردن** to record the trade without a margin. A typed margin that
differs from the risk-implied one by more than 10 % gets a ⚠️ notice showing
both numbers, with a one-tap switch to the bot's suggestion (✅ پیشنهاد ربات)
or simply retype another figure.

The closed trade's **typed dollar result is stored as-is** and the configured
budget moves by it — the close confirmation shows «بودجهٔ جدید». With a margin
on the open trade, ROI = P&L ÷ margin; without one, ROI stays unknown and
`/recent` shows «—» instead of a fabricated amount.

Every close now stores the **typed dollar result** — Win ➕, Loss ➖, BE 0 —
so `/recent`, `/stats` and the Excel export always show the real amount. ROI
appears only when a margin was recorded (ROI = P&L ÷ margin); leverage is
retired — the column stays in the database for legacy rows but is never asked,
shown or exported. In `/recent` two-phase trades carry a 🔁
mark, and their detail card shows both reasons (ورود/خروج), the entry and exit
hours, the exit-photo count and — when exit shots exist — a
📸 عکس‌های خروج button that re-sends them one by one. The stats panel renders
«—» for selections whose trades have no P&L (legacy margin-less rows), so it
can never crash on them. The 🗑 button on an open trade's detail deletes it
(and its screenshots) without ever touching the history.

## Data

- Trades are stored in `journal.db` (SQLite, WAL mode) next to `bot.py`.
- Override the location with the `JOURNAL_DB` environment variable.
- Open positions live in a separate `open_trades` table; closing one copies it
  into `trades` with the exit answers and removes it from the open list.
- Databases created before the timeframe/screenshot update are migrated
  automatically on startup (new columns, existing rows keep working). Databases
  from before the open-trades update are migrated too: the new columns are
  added, P&L/margin become nullable for open-flow closes, and the
  `open_trades` table is created.
- `journal.db` and `screenshots/` are git-ignored — back them up like any
  personal data.

## Setup

1. Create a bot and get a token from [@BotFather](https://t.me/BotFather) on Telegram.
2. Install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Configure your token — either copy `.env.example` to `.env` and paste your
   token, or set the environment variable directly:

   ```powershell
   Copy-Item .env.example .env
   # then edit .env and replace the placeholder with your token
   ```

## Run

```powershell
.\.venv\Scripts\python.exe bot.py
```

Then open your bot in Telegram and send `/trade` to log your first closed trade.

## Deploy to Railway

Railway runs the bot as a long-polling worker (no web port needed). Because
Railway's container filesystem is wiped on every deploy, attach a **Volume**
for `journal.db` and the screenshots.

1. Install the CLI and log in:

   ```powershell
   npm i -g @railway/cli
   railway login
   ```

2. Create a project and link this folder to it:

   ```powershell
   railway init          # or: railway link for an existing project
   ```

3. In the Railway dashboard, open your service → **Volume** → attach one and
   set the **mount path** to `/data`.

4. Add these **Variables** to the service (never commit the real token):

   | Variable | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | your token from @BotFather |
   | `JOURNAL_DB` | `/data/journal.db` |
   | `SCREENSHOT_DIR` | `/data/screenshots` |
   | `EXPORT_DIR` | `/data/exports` |

5. Deploy — either push to GitHub (if you connected the repo) or from this
   folder:

   ```powershell
   railway up
   ```

   The `railway.json` file tells Railway to start the bot with `python bot.py`
   and to restart it if it crashes. Watch it with `railway logs`.

6. To carry your existing local history over, upload `journal.db` (and the
   `screenshots/` files) into the volume:

   ```powershell
   railway ssh -- mkdir -p /data/screenshots
   railway ssh -- scp local:C:/Users/Astrix/Dev/trading/journal.db /data/journal.db
   railway ssh -- scp -r local:C:/Users/Astrix/Dev/trading/screenshots/. /data/screenshots/
   ```

   (`railway volume files put journal.db /data/` works too. `railway ssh`
   requires a one-time invite link shown by `railway ssh --`.)

## Test

Two offline suites cover the whole `/trade`, `/open` and `/close` flows
(reply-keyboard buttons, typed fallbacks, timeframe parsing, screenshot
handling, PTB routing, database writes, migration). They use a throwaway
database and temp folder, so they are safe to run anytime:

```powershell
.\.venv\Scripts\python.exe smoke_test.py   # 300+ checks, fake Telegram objects
.\.venv\Scripts\python.exe ptb_probe.py    # real PTB conversations with a stub Bot
```

`ptb_probe.py` drives genuine `ConversationHandler.check_update` /
`handle_update` calls with bot-bound updates — it exists to catch wiring bugs
that fake objects cannot see (e.g. shortcuts needing a bound bot).

## Project layout

- `bot.py` — entry point: env loading, logging, handler wiring
- `journal.py` — the `/trade`, `/open` and `/close` conversations plus the 🟢
  open-trades panel, `/recent`, `/stats`, `/delete`
- `db.py` — SQLite storage (schema + migration, open trades, insert/delete/
  list, stats)
- `export.py` — spreadsheet export used by `/export` (openpyxl)
- `smoke_test.py` — offline checks for the `/trade` flow (no Telegram needed)
- `ptb_probe.py` — real-PTB conversation probe (stub Bot, no network)
- `screenshots/` — downloaded chart screenshots (created on demand)

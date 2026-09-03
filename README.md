# Telegram Trading Journal

A personal Telegram bot that logs closed trades into a local SQLite database.

## Language / زبان

The bot's UI is in **Persian** (رسمی — polite register) with **technical
market terms in English** (Entry, Take Profit, Stop Loss, Leverage, Margin,
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
| `/trade` | Guided entry: بازار (🪙 کریپتو / 💵 فارکس) -> symbol (recent / most-used as buttons) -> 📈 Long / 📉 Short -> Leverage (skippable) -> timeframe -> Entry -> 🎯 Take Profit -> 🛑 Stop Loss -> نتیجه (✅ Win / ❌ Loss / ➖ BE) -> 💰 Margin -> ⚠️ Risk % (skippable) -> date (📅 امروز) -> hour -> دلیل ورود -> Mood -> 📸 screenshot before -> 📸 screenshot after -> ✅ ذخیره / ❌ ثبت نشود. **P&L and ROI are calculated automatically** from margin × leverage × price move — the trader is never asked |
| `/recent` | Paginated **inline panel** — **one button per trade** (result emoji + id + pair + P&L, 📷 if screenshots) with ◀️/▶️ paging, an All/1W/1M range filter, and a 🏠 Home button. Tapping a trade **sends a separate, airy detail message** (all fields with bullets, emoji labels and blank lines) carrying 📷 عکس چارت (only when the trade has screenshots — re-sends the before/after photos) + 🗑 Delete + ❌ Close; deleting confirms on the detail and refreshes the panel |
| `/stats` | Filterable performance panel with **inline buttons attached to the message** (period, symbol, reset, export); `/stats BTCUSD 1w` style arguments still work |
| `/delete <id>` | Delete a trade, e.g. `/delete 12` |
| `/export` | Download all trades as an `.xlsx` spreadsheet |
| `/cancel` | Abort the current `/trade` entry |

## Keyboard buttons

Inside `/trade` the choices appear as **reply-keyboard buttons under the
message input field** (at the bottom of the screen):

- **بازار** — 🪙 کریپتو / 💵 فارکس (first step; the margin question adapts:
  USDT vs USD حساب فارکس)
- Your **most used** and **recently traded** symbols (up to 8, top row = most used). With no trade history yet the bot simply asks you to type one.
- 📈 Long / 📉 Short (typed `l`/`s`/`buy`/`sell` still work)
- Leverage: `×2 ×3 ×5 ×10 ×20 ×50 ×100 ×125`, typed (`10`, `10x`) or ⏭ بدون اهرم
- Timeframe: `1m 5m 15m 30m 1h 4h 1D 1W 1M` (or type any timeframe like `45m`)
- Entry, 🎯 Take Profit (TP), 🛑 Stop Loss (SL) — typed prices
- نتیجه معامله — **✅ Win / ❌ Loss / ➖ BE** (Win sets exit = TP, Loss sets
  exit = SL, BE sets exit = entry; the result also drives the auto P&L)
- 💰 Margin — how much money you committed to the trade
- ⚠️ Risk — the percent of your account risked (`0.5% 1% 2% 3% 5% 10%`,
  typed values, or ⏭ بدون درصد). **P&L is auto-calculated** from margin,
  leverage and the exit price — no extra question
- 📅 امروز for the date (or type `2026-02-09 14:30` in one go), then the
  trade hour — hour buttons, 🕐 الان, `HH`/`HH:MM` typed, or ⏭ رد کردن
- Mood while making the trade — آرام · مطمئن · مضطرب · طمع · FOMO ·
  انتقامی (plain professional labels; tap one, type it, or ⏭ رد کردن);
  stored with the trade and shown in `/recent` and the stats breakdown
- 📝 دلیل ورود (why you entered) instead of a generic notes field
- 📅 Today's date
- ⏭ بدون دلیل / ⏭ بدون اسکرین‌شات
- ✅ ذخیره / ❌ ثبت نشود
- ✖️ لغو on every choice screen (or send `/cancel`)

Tapping a button sends its label as a normal message, so typed answers work
everywhere too (`l`/`s` for direction, `win`/`lose`/`be`/`breakeven` for the
result, `5min`/`daily`/`weekly` for timeframes, `-` as the universal skip
shortcut). The keyboard hides itself for the free-text fields (symbol,
prices, margin, custom date, notes).

## Menu

- The **☰ button next to the message bar** opens the bot's command list —
  every command with a short emoji explanation. It is registered
  automatically when the bot starts (`set_my_commands`).
- `/start`, `/help` or the 🏠 منو button send the **main-menu keyboard** —
  persistent buttons under the input bar: 📈 معامله جدید · 📊 آمار /
  🕘 معاملات اخیر · 📥 اکسل / 🏠 منو. Tapping one runs the matching command;
  📈 معامله جدید safely restarts the questionnaire even mid-entry. The bar is
  re-sent after saving, discarding or cancelling an entry, so the buttons
  never disappear (no `/start` needed).

## Stats panel

`/stats` sends the report as a message with the **filter buttons attached to
the message itself** (inline keyboard). Tapping a filter edits that same
message in place — no new messages stack up, and the buttons always sit right
under the numbers they control.

- The buttons are **filters only** — they change the *period* and the
  *symbol* of the numbers above, nothing else (not timeframes, not trades).
- **Periods** — `1W · 1M · 3M · 6M · 1Y · All`; the active period is marked
  with ✓.
- **🔤 Symbols** — opens a separate, dedicated picker message so the symbol
  list never clutters the panel: 10 symbols per page, sorted by your **most
  recent trade** first, with ◀️/▶️ page navigation, a page counter, and
  «همه نمادها» / ✖️ Close buttons. Tapping a symbol filters the panel;
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
(Win/Loss/BE), leverage, margin, risk %, P&L, **ROI %**, date, mood, reason
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

## Data

- Trades are stored in `journal.db` (SQLite, WAL mode) next to `bot.py`.
- Override the location with the `JOURNAL_DB` environment variable.
- Databases created before the timeframe/screenshot update are migrated
  automatically on startup (new columns, existing rows keep working).
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

## Test

An offline smoke test covers the whole `/trade` flow (reply-keyboard buttons,
typed fallbacks, timeframe parsing, screenshot handling, PTB routing, database
writes, migration). It uses a throwaway database and temp folder, so it is
safe to run anytime:

```powershell
.\.venv\Scripts\python.exe smoke_test.py
```

## Project layout

- `bot.py` — entry point: env loading, logging, handler wiring
- `journal.py` — the `/trade` conversation plus `/recent`, `/stats`,
  `/delete`
- `db.py` — SQLite storage (schema + migration, insert/delete/list, stats)
- `export.py` — spreadsheet export used by `/export` (openpyxl)
- `smoke_test.py` — offline checks for the `/trade` flow (no Telegram needed)
- `screenshots/` — downloaded chart screenshots (created on demand)

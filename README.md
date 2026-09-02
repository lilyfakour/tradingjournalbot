# Telegram Trading Journal

A personal Telegram bot that logs closed trades into a local SQLite database.

## Commands

| Command | What it does |
| --- | --- |
| `/trade` | Guided entry with buttons: symbol -> 📈 Long / 📉 Short -> entry -> exit -> size -> P&L (🤖 Auto) -> date (📅 Today) -> notes (⏭ Skip) -> ✅ Save / ❌ Discard |
| `/recent` | Show the last 10 trades (with ids for `/delete`) |
| `/stats` | Overall performance: win rate, total P&L, profit factor, best/worst |
| `/delete <id>` | Delete a trade, e.g. `/delete 12` |
| `/cancel` | Abort the current `/trade` entry |

Button prompts: inside `/trade` the bot shows a button wherever there is a
choice to make — 📈 Long / 📉 Short, 🤖 Auto-calculate P&L, 📅 Today's date,
⏭ Skip notes, and ✅ Save / ❌ Discard to confirm. Free-text fields (symbol,
prices, size, custom date, notes, manual P&L) still accept typed input, and
`-` keeps working as the skip/default shortcut. P&L is auto-calculated as
`(exit - entry) * size` for longs and `(entry - exit) * size` for shorts; type
your own value instead if your instrument needs a multiplier (e.g. futures).
Numbers use `.` as the decimal separator (no thousands separators).

## Data

- Trades are stored in `journal.db` (SQLite, WAL mode) next to `bot.py`.
- Override the location with the `JOURNAL_DB` environment variable.
- `journal.db` is git-ignored — back it up like any personal data.

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

An offline smoke test covers the whole `/trade` flow (buttons, typed fallbacks,
PTB routing, database writes). It uses a throwaway database, so it is safe to
run anytime:

```powershell
.\.venv\Scripts\python.exe smoke_test.py
```

## Project layout

- `bot.py` — entry point: env loading, logging, handler wiring
- `journal.py` — the `/trade` conversation plus `/recent`, `/stats`, `/delete`
- `db.py` — SQLite storage (schema, insert/delete/list, aggregate stats)
- `smoke_test.py` — offline checks for the `/trade` flow (no Telegram needed)

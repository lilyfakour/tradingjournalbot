"""SQLite storage for the trading journal.

The database lives next to this file (journal.db) unless overridden with the
JOURNAL_DB environment variable. Every public function opens its own short
lived connection, which keeps the code simple and safe to call from the bot's
handlers.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.getenv("JOURNAL_DB", str(Path(__file__).resolve().parent / "journal.db"))
)
# On hosts like Railway the DB may point into a mounted volume whose
# subdirectories do not exist yet — create them, or sqlite3.connect fails.
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('long', 'short')),
    timeframe   TEXT    NOT NULL DEFAULT '',
    entry_price REAL    NOT NULL,
    exit_price  REAL    NOT NULL,
    size        REAL    NOT NULL,
    pnl         REAL    NOT NULL,
    roi         REAL,
    trade_date  TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    mood        TEXT    NOT NULL DEFAULT '',
    screenshot  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (trade_date);
CREATE TABLE IF NOT EXISTS open_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL CHECK (direction IN ('long', 'short')),
    market       TEXT    NOT NULL DEFAULT 'crypto',
    timeframe    TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    screenshot   TEXT,
    trade_date   TEXT    NOT NULL,
    entry_time   TEXT    NOT NULL DEFAULT '',
    risk_percent REAL,
    entry_price  REAL    NOT NULL,
    take_profit  REAL,
    stop_loss    REAL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_open_trades_date ON open_trades (trade_date);
"""

# Columns added after the first release; init_db() ALTERs older databases so
# they gain the new columns without touching existing rows.
_NEW_COLUMNS = {
    "timeframe": "TEXT NOT NULL DEFAULT ''",
    "mood": "TEXT NOT NULL DEFAULT ''",
    "screenshot": "TEXT",
    "market": "TEXT NOT NULL DEFAULT 'crypto'",
    "leverage": "REAL",
    "risk_percent": "REAL",
    "take_profit": "REAL",
    "stop_loss": "REAL",
    "hit": "TEXT NOT NULL DEFAULT ''",
    "screenshot_after": "TEXT",
    # Return on investment in percent (P&L / margin * 100). Legacy rows stay
    # NULL; the UI falls back to computing it from pnl/size when needed.
    "roi": "REAL",
    # --- open-trade flow (tracked in open_trades, merged on close) -----------
    # Time part of the entry ("HH:MM"); the date part lives in trade_date.
    "entry_time": "TEXT NOT NULL DEFAULT ''",
    # Time part of the exit ("HH:MM").
    "exit_time": "TEXT NOT NULL DEFAULT ''",
    # Why the position was opened. The close flow stores the EXIT reason in
    # `notes`; legacy rows keep their (entry) reason only in `notes`.
    "entry_reason": "TEXT NOT NULL DEFAULT ''",
    "exit_reason": "TEXT NOT NULL DEFAULT ''",
    # Exit chart screenshots — one filename per line (up to 4). Legacy rows
    # keep their single after-shot in `screenshot_after`.
    "exit_photos": "TEXT",
    # 'closed' = logged directly via /trade; 'open' = closed from open_trades.
    "source": "TEXT NOT NULL DEFAULT 'closed'",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _column_ddl(
    row: sqlite3.Row,
    drop_notnull: set[str] = frozenset(),
    autoinc: bool = False,
) -> str:
    """Rebuild one column's DDL from PRAGMA table_info (for _relax_trades)."""
    notnull = 0 if row["name"] in drop_notnull else row["notnull"]
    parts = [f'"{row["name"]}"', row["type"] or "TEXT"]
    if row["pk"]:
        # PRAGMA does not report AUTOINCREMENT — _relax_trades passes it.
        parts.append("PRIMARY KEY" + (" AUTOINCREMENT" if autoinc else ""))
    if notnull:
        parts.append("NOT NULL")
    if row["dflt_value"] is not None:
        # DEFAULT (expr) — parens are required around expressions and PRAGMA
        # strips the original ones, so always re-wrap (literals are fine too).
        parts.append(f"DEFAULT ({row['dflt_value']})")
    return " ".join(parts)


def _relax_trades(conn: sqlite3.Connection) -> None:
    """Allow NULL pnl/size in `trades` (table rebuild — SQLite cannot ALTER).

    Trades closed from the open-trades flow have no margin in their
    questionnaire, so their P&L is genuinely unknown: it must stay NULL
    rather than a fake 0 that would pollute win/loss statistics.
    """
    info = list(conn.execute("PRAGMA table_info(trades)"))
    by_name = {row["name"]: row for row in info}
    if not (by_name["pnl"]["notnull"] or by_name["size"]["notnull"]):
        return
    master = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'trades'"
    ).fetchone()
    autoinc = bool(master and master["sql"] and "AUTOINCREMENT" in master["sql"])
    ddl = ", ".join(
        _column_ddl(row, {"pnl", "size"}, autoinc=autoinc and row["name"] == "id")
        for row in info
    )
    names = ", ".join(f'"{row["name"]}"' for row in info)
    conn.executescript(
        "CREATE TABLE trades_migrating (" + ddl + ");"
        f"INSERT INTO trades_migrating ({names}) SELECT {names} FROM trades;"
        "DROP TABLE trades;"
        "ALTER TABLE trades_migrating RENAME TO trades;"
        "CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (trade_date);"
    )
    logger.info("trades table rebuilt: pnl/size are now nullable")


def init_db() -> None:
    """Create the tables and indexes if they do not exist yet."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        for name, decl in _NEW_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
        _relax_trades(conn)
        conn.commit()
    finally:
        conn.close()


def add_trade(
    *,
    symbol: str,
    direction: str,
    timeframe: str,
    entry_price: float,
    exit_price: float,
    size: Optional[float],
    pnl: Optional[float],
    trade_date: str,
    notes: str,
    mood: str,
    roi: Optional[float] = None,
    screenshot: Optional[str] = None,
    market: str = "crypto",
    leverage: Optional[float] = None,
    risk_percent: Optional[float] = None,
    take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    hit: str = "",
    screenshot_after: Optional[str] = None,
) -> int:
    """Insert a closed trade and return its id.

    exit_price is the price that actually closed the trade (the TP or SL that
    hit); size is the margin the trader committed (None for trades closed from
    the open-trades flow, which has no margin question — pnl stays NULL too);
    hit is 'tp', 'sl', 'win', 'lose', 'be' or 'manual'.
    roi is the return on investment in percent (P&L / margin * 100).
    """
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO trades"
            " (symbol, direction, timeframe, entry_price, exit_price, size,"
            "  pnl, roi, trade_date, notes, mood, screenshot, market, leverage,"
            "  risk_percent, take_profit, stop_loss, hit, screenshot_after)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                direction,
                timeframe,
                entry_price,
                exit_price,
                size,
                pnl,
                roi,
                trade_date,
                notes,
                mood,
                screenshot,
                market,
                leverage,
                risk_percent,
                take_profit,
                stop_loss,
                hit,
                screenshot_after,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_trade(trade_id: int) -> Optional[sqlite3.Row]:
    """Return a single trade by id, or None if it does not exist."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    finally:
        conn.close()


def get_recent(
    limit: int = 10, offset: int = 0, since: Optional[str] = None
) -> list[sqlite3.Row]:
    """Return the most recent trades, newest first.

    offset/since power the /recent panel's paging (since is a YYYY-MM-DD
    cutoff as produced by journal._recent_since).
    """
    conn = _connect()
    try:
        if since:
            return conn.execute(
                "SELECT * FROM trades WHERE trade_date >= ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (since, limit, offset),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()


def count_trades(since: Optional[str] = None) -> int:
    """Number of stored trades (optionally only those on/after `since`)."""
    conn = _connect()
    try:
        if since:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE trade_date >= ?", (since,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_all_trades() -> list[sqlite3.Row]:
    """Return every logged trade, oldest first (for the /export command)."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM trades ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()


def delete_trade(trade_id: int) -> Optional[sqlite3.Row]:
    """Delete a trade by id and return the row that was removed, if any."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        conn.commit()
        return row
    finally:
        conn.close()


def add_open_trade(
    *,
    symbol: str,
    direction: str,
    market: str,
    timeframe: str,
    reason: str,
    screenshot: Optional[str],
    trade_date: str,
    entry_time: str,
    risk_percent: Optional[float],
    entry_price: float,
    take_profit: Optional[float],
    stop_loss: Optional[float],
) -> int:
    """Insert an open (running) trade and return its id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO open_trades"
            " (symbol, direction, market, timeframe, reason, screenshot,"
            "  trade_date, entry_time, risk_percent, entry_price,"
            "  take_profit, stop_loss)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                direction,
                market,
                timeframe,
                reason,
                screenshot,
                trade_date,
                entry_time,
                risk_percent,
                entry_price,
                take_profit,
                stop_loss,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_open_trade(open_id: int) -> Optional[sqlite3.Row]:
    """Return a single open trade by id, or None if it does not exist."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM open_trades WHERE id = ?", (open_id,)
        ).fetchone()
    finally:
        conn.close()


def get_open_trades(
    limit: int = 10, offset: int = 0
) -> list[sqlite3.Row]:
    """Open trades, newest first (powers the 🟢 panel's paging)."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM open_trades ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()


def count_open_trades() -> int:
    """Number of currently open trades."""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM open_trades").fetchone()
        return int(row[0])
    finally:
        conn.close()


def close_open_trade(
    open_id: int,
    *,
    hit: str,
    exit_price: float,
    trade_date: str,
    exit_time: str,
    notes: str,
    mood: str,
    exit_photos: Optional[str],
    screenshot_after: Optional[str],
) -> Optional[int]:
    """Move an open trade into the closed `trades` list and return the new id.

    The open questionnaire has no margin question, so size/pnl/roi are stored
    as NULL (never a fake 0) — the stats panel classifies win/loss/BE from
    `hit` and the price direction for such rows.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM open_trades WHERE id = ?", (open_id,)
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "INSERT INTO trades"
            " (symbol, direction, timeframe, entry_price, exit_price, size,"
            "  pnl, roi, trade_date, notes, mood, screenshot, market,"
            "  risk_percent, take_profit, stop_loss, hit, screenshot_after,"
            "  entry_time, exit_time, entry_reason, exit_reason, exit_photos,"
            "  source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            "         ?, ?, ?, ?, ?, ?)",
            (
                row["symbol"],
                row["direction"],
                row["timeframe"],
                row["entry_price"],
                exit_price,
                None,  # size — unknown (no margin question)
                None,  # pnl  — unknown (no margin to compute it from)
                None,  # roi
                trade_date,
                notes,  # the exit reason lives in `notes`
                mood,
                row["screenshot"],
                row["market"],
                row["risk_percent"],
                row["take_profit"],
                row["stop_loss"],
                hit,
                screenshot_after,
                row["entry_time"],
                exit_time,
                row["reason"],
                notes,
                exit_photos,
                "open",
            ),
        )
        new_id = int(cursor.lastrowid)
        conn.execute("DELETE FROM open_trades WHERE id = ?", (open_id,))
        conn.commit()
        return new_id
    finally:
        conn.close()


def delete_open_trade(open_id: int) -> Optional[sqlite3.Row]:
    """Delete an open trade by id and return the row that was removed."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM open_trades WHERE id = ?", (open_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM open_trades WHERE id = ?", (open_id,))
        conn.commit()
        return row
    finally:
        conn.close()


def get_symbol_suggestions(
    recent_limit: int = 4, top_limit: int = 4
) -> tuple[list[str], list[str]]:
    """Return (recent, top) symbol lists for the /trade symbol keyboard.

    recent — distinct symbols ordered by how recently they were traded;
    top — distinct symbols ordered by how often they were traded (recency
    breaks ties).
    """
    conn = _connect()
    try:
        recent_rows = conn.execute(
            "SELECT symbol, MAX(id) AS last_id FROM trades"
            " GROUP BY symbol ORDER BY last_id DESC LIMIT ?",
            (recent_limit,),
        ).fetchall()
        top_rows = conn.execute(
            "SELECT symbol, COUNT(*) AS uses, MAX(id) AS last_id FROM trades"
            " GROUP BY symbol ORDER BY uses DESC, last_id DESC LIMIT ?",
            (top_limit,),
        ).fetchall()
        return (
            [row["symbol"] for row in recent_rows],
            [row["symbol"] for row in top_rows],
        )
    finally:
        conn.close()


def clear_screenshot(trade_id: int) -> Optional[str]:
    """Remove the screenshot reference of a trade; return the old filename."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT screenshot FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if row is None:
            return None
        old = row["screenshot"]
        conn.execute(
            "UPDATE trades SET screenshot = NULL WHERE id = ?", (trade_id,)
        )
        conn.commit()
        return old
    finally:
        conn.close()


def get_stats(
    symbol: Optional[str] = None, since: Optional[str] = None
) -> dict[str, Any]:
    """Return aggregate performance numbers over the logged trades.

    symbol — only trades of this symbol (case-insensitive);
    since — only trades on/after this YYYY-MM-DD date string.
    """
    # Trades closed from the open-trades flow have no margin, so their P&L is
    # NULL: classify win/loss/BE from `hit` and the price direction instead.
    _hit_win = (
        "hit IN ('tp', 'win')"
        " OR (hit = 'manual' AND ((direction = 'long' AND exit_price > entry_price)"
        " OR (direction = 'short' AND exit_price < entry_price)))"
    )
    _hit_loss = (
        "hit IN ('sl', 'lose')"
        " OR (hit = 'manual' AND ((direction = 'long' AND exit_price < entry_price)"
        " OR (direction = 'short' AND exit_price > entry_price)))"
    )
    _hit_be = "hit = 'be' OR (hit = 'manual' AND exit_price = entry_price)"
    conn = _connect()
    try:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if since:
            clauses.append("trade_date >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = conn.execute(
            "SELECT COUNT(*) AS trades,"
            " SUM(pnl) AS total,"
            " SUM(CASE WHEN pnl > 0 THEN 1"
            f" WHEN pnl IS NULL AND ({_hit_win}) THEN 1 ELSE 0 END) AS wins,"
            " SUM(CASE WHEN pnl < 0 THEN 1"
            f" WHEN pnl IS NULL AND ({_hit_loss}) THEN 1 ELSE 0 END) AS losses,"
            " SUM(CASE WHEN pnl = 0 THEN 1"
            f" WHEN pnl IS NULL AND ({_hit_be}) THEN 1 ELSE 0 END) AS be,"
            " AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,"
            " AVG(CASE WHEN pnl < 0 THEN pnl END) AS avg_loss,"
            " AVG(roi) AS avg_roi,"
            " MAX(pnl) AS best,"
            " MIN(pnl) AS worst,"
            " SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,"
            " SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gross_loss"
            " FROM trades" + where,
            params,
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def get_mood_breakdown(
    symbol: Optional[str] = None, since: Optional[str] = None
) -> list[sqlite3.Row]:
    """Per-mood aggregates (trades, total P&L, wins), best P&L first."""
    conn = _connect()
    try:
        clauses, params = ["mood != ''"], []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if since:
            clauses.append("trade_date >= ?")
            params.append(since)
        where = " WHERE " + " AND ".join(clauses)
        return conn.execute(
            "SELECT mood, COUNT(*) AS trades,"
            " SUM(pnl) AS total,"
            " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins"
            " FROM trades" + where + " GROUP BY mood ORDER BY total DESC",
            params,
        ).fetchall()
    finally:
        conn.close()


def get_symbol_counts(limit: int = 8) -> list[tuple[str, int]]:
    """Distinct symbols with trade counts, most traded first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS uses, MAX(id) AS last_id FROM trades"
            " GROUP BY symbol ORDER BY uses DESC, last_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(row["symbol"], row["uses"]) for row in rows]
    finally:
        conn.close()


def get_all_symbols() -> list[tuple[str, int]]:
    """Every distinct symbol with its trade count, latest trade first.

    Used by the /stats symbol picker: sorted by the id of the most recent
    trade, so the symbols the trader touched last come first.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS uses, MAX(id) AS last_id FROM trades"
            " GROUP BY symbol ORDER BY last_id DESC, symbol"
        ).fetchall()
        return [(row["symbol"], row["uses"]) for row in rows]
    finally:
        conn.close()
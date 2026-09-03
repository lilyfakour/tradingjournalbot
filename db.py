"""SQLite storage for the trading journal.

The database lives next to this file (journal.db) unless overridden with the
JOURNAL_DB environment variable. Every public function opens its own short
lived connection, which keeps the code simple and safe to call from the bot's
handlers.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

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
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create the tables and indexes if they do not exist yet."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        for name, decl in _NEW_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
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
    size: float,
    pnl: float,
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
    hit); size is the margin the trader committed; hit is 'tp' or 'sl'.
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
            " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,"
            " SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,"
            " SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END) AS be,"
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
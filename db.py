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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('long', 'short')),
    entry_price REAL    NOT NULL,
    exit_price  REAL    NOT NULL,
    size        REAL    NOT NULL,
    pnl         REAL    NOT NULL,
    trade_date  TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (trade_date);
"""


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
        conn.commit()
    finally:
        conn.close()


def add_trade(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    size: float,
    pnl: float,
    trade_date: str,
    notes: str,
) -> int:
    """Insert a closed trade and return its id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO trades"
            " (symbol, direction, entry_price, exit_price, size, pnl, trade_date, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, direction, entry_price, exit_price, size, pnl, trade_date, notes),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_recent(limit: int = 10) -> list[sqlite3.Row]:
    """Return the most recent trades, newest first."""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
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


def get_stats() -> dict[str, Any]:
    """Return aggregate performance numbers over all logged trades."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS trades,"
            " SUM(pnl) AS total,"
            " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,"
            " SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,"
            " AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,"
            " AVG(CASE WHEN pnl < 0 THEN pnl END) AS avg_loss,"
            " MAX(pnl) AS best,"
            " MIN(pnl) AS worst,"
            " SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,"
            " SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) AS gross_loss"
            " FROM trades"
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()
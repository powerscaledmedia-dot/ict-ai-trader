"""
ICT-specific database tables.
Called from main.py after init_database().
"""

import logging
from database import get_db_connection

logger = logging.getLogger(__name__)


def init_ict_tables() -> None:
    """Create ICT tables if they don't exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # ICT trades table — full lifecycle
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ict_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_uuid TEXT UNIQUE NOT NULL,
                setup_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                instrument TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                pnl REAL DEFAULT 0,
                outcome TEXT,
                grade TEXT NOT NULL,
                score REAL NOT NULL,
                killzone TEXT,
                confluence_factors TEXT,
                news_status TEXT,
                tradovate_order_id TEXT,
                contracts INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                opened_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT,
                raw_payload TEXT
            )
        """)

        # ICT signals — TradingView webhook log (all signals, including rejected)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ict_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_uuid TEXT UNIQUE NOT NULL,
                received_at TEXT DEFAULT (datetime('now')),
                payload TEXT NOT NULL,
                killzone_status TEXT,
                scanner_grade TEXT,
                scanner_score REAL,
                risk_status TEXT,
                news_status TEXT,
                final_decision TEXT NOT NULL,
                rejection_reason TEXT,
                trade_id INTEGER
            )
        """)

        # News sentinel log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ict_news_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT DEFAULT (datetime('now')),
                status TEXT NOT NULL,
                reason TEXT,
                headlines TEXT
            )
        """)

        # Analyst reports
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ict_analyst_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT UNIQUE NOT NULL,
                trades_analyzed INTEGER,
                win_rate REAL,
                updated_weights TEXT,
                full_report TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        conn.commit()
        logger.info("ICT database tables initialized")

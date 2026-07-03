import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Configuration

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "receipts.db"
DB_PATH: str = os.environ.get("DB_PATH", str(_DEFAULT_DB_PATH))

DEFAULT_CURRENCY: str = "MYR"

VALID_CATEGORIES: tuple[str, ...] = (
    "food",
    "transport",
    "utilities",
    "shopping",
    "entertainment",
    "health",
    "education",
    "other",
)


# Custom exceptions

class DatabaseError(Exception):
    """Raised when any database operation fails."""


class DuplicateReceiptError(DatabaseError):
    """Raised when a receipt with the same merchant, date, and total exists."""


# Connection handling


def _ensure_parent_dir() -> None:
    """Create the folder for the database file if it does not exist."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection, commit on success, rollback on error, always close."""
    _ensure_parent_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Schema creation

def init_db() -> None:
    """Create tables if they do not already exist. Safe to call on every startup."""
    try:
        with get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant   TEXT,
                    date       TEXT,
                    total      REAL,
                    currency   TEXT DEFAULT 'MYR',
                    image_ext  TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL,
                    name       TEXT NOT NULL,
                    price      REAL NOT NULL,
                    category   TEXT NOT NULL,
                    FOREIGN KEY (receipt_id) REFERENCES receipts (id)
                )
                """
            )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to initialise the database: {exc}") from exc


# Helpers

def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def normalize_category(category: str | None) -> str:
    """Map any category value onto one of the allowed VALID_CATEGORIES."""
    if category is None:
        return "other"
    cleaned = category.strip().lower()
    return cleaned if cleaned in VALID_CATEGORIES else "other"


# Duplicate detection

def check_duplicate(
    merchant: str | None, receipt_date: str | None, total: float | None
) -> dict | None:
    """Check if a receipt with the same merchant, date, and total already exists."""
    try:
        with get_connection() as conn:
            if merchant is None and receipt_date is None and total is None:
                return None

            row = conn.execute(
                """
                SELECT id, merchant, date, total, currency, created_at
                FROM receipts
                WHERE merchant IS ? AND date IS ? AND total IS ?
                LIMIT 1
                """,
                (merchant, receipt_date, total),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to check duplicate: {exc}") from exc

    return dict(row) if row is not None else None


# Receipts + items

def create_receipt(
    merchant: str | None,
    receipt_date: str | None,
    total: float | None,
    currency: str | None,
    items: list[dict],
    image_ext: str | None = None,
) -> dict:
    """Save one receipt and all of its items in a single transaction."""
    currency = currency or DEFAULT_CURRENCY
    created_at = _now_iso()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO receipts
                    (merchant, date, total, currency, image_ext, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (merchant, receipt_date, total, currency, image_ext, created_at),
            )
            receipt_id = cursor.lastrowid

            for item in items:
                conn.execute(
                    """
                    INSERT INTO items (receipt_id, name, price, category)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        item["name"],
                        float(item["price"]),
                        normalize_category(item.get("category")),
                    ),
                )
    except (sqlite3.Error, KeyError, ValueError, TypeError) as exc:
        raise DatabaseError(f"Failed to save receipt: {exc}") from exc

    saved = get_receipt(receipt_id)
    if saved is None:
        raise DatabaseError("Receipt was saved but could not be read back")
    return saved


def get_all_receipts() -> list[dict]:
    """Return all receipts, newest first, WITHOUT items."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, merchant, date, total, currency, image_ext, created_at
                FROM receipts
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to list receipts: {exc}") from exc

    return [dict(row) for row in rows]


def get_receipt(receipt_id: int) -> dict | None:
    """Return one receipt WITH its items, or None if not found."""
    try:
        with get_connection() as conn:
            receipt_row = conn.execute(
                """
                SELECT id, merchant, date, total, currency, image_ext, created_at
                FROM receipts WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()

            if receipt_row is None:
                return None

            item_rows = conn.execute(
                """
                SELECT id, receipt_id, name, price, category
                FROM items WHERE receipt_id = ? ORDER BY id
                """,
                (receipt_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to fetch receipt {receipt_id}: {exc}") from exc

    receipt = dict(receipt_row)
    receipt["items"] = [dict(row) for row in item_rows]
    return receipt


def delete_receipt(receipt_id: int) -> bool:
    """Delete a receipt and all of its items. Returns True if it existed."""
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM items WHERE receipt_id = ?", (receipt_id,))
            cursor = conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to delete receipt {receipt_id}: {exc}") from exc


# Analytics 

def get_analytics(start: str | None = None, end: str | None = None) -> dict:
    """Return spending breakdown by category plus totals.
    """
    where_parts: list[str] = []
    params: list[str] = []

    if start:
        where_parts.append("r.date >= ?")
        params.append(start)
    if end:
        where_parts.append("r.date <= ?")
        params.append(end)

    date_filter = ""
    if where_parts:
        date_filter = " AND r.date IS NOT NULL AND " + " AND ".join(where_parts)

    try:
        with get_connection() as conn:
            category_rows = conn.execute(
                f"""
                SELECT i.category AS category, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE 1=1 {date_filter}
                GROUP BY i.category
                ORDER BY total DESC
                """,
                params,
            ).fetchall()

            count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM receipts r
                WHERE 1=1 {date_filter}
                """,
                params,
            ).fetchone()

            range_row = conn.execute(
                f"""
                SELECT MIN(r.date) AS start, MAX(r.date) AS end
                FROM receipts r
                WHERE r.date IS NOT NULL{date_filter}
                """,
                params,
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to build analytics: {exc}") from exc

    breakdown = {row["category"]: round(row["total"], 2) for row in category_rows}
    total_spent = round(sum(breakdown.values()), 2)

    return {
        "category_breakdown": breakdown,
        "total_spent": total_spent,
        "receipt_count": count_row["n"],
        "date_range": {"start": range_row["start"], "end": range_row["end"]},
    }


def get_spending_last_30_days() -> dict:
    """Gather the last 30 days of spending for AI insights."""
    period_end = date.today()
    period_start = period_end - timedelta(days=30)
    start_str = period_start.isoformat()

    try:
        with get_connection() as conn:
            category_rows = conn.execute(
                """
                SELECT i.category AS category, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.date IS NOT NULL AND r.date >= ?
                GROUP BY i.category
                ORDER BY total DESC
                """,
                (start_str,),
            ).fetchall()

            count_row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM receipts
                WHERE date IS NOT NULL AND date >= ?
                """,
                (start_str,),
            ).fetchone()

            biggest_row = conn.execute(
                """
                SELECT i.name AS name, i.price AS price
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.date IS NOT NULL AND r.date >= ?
                ORDER BY i.price DESC LIMIT 1
                """,
                (start_str,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to build 30-day summary: {exc}") from exc

    category_totals = {row["category"]: round(row["total"], 2) for row in category_rows}
    biggest_expense = (
        {"name": biggest_row["name"], "price": round(biggest_row["price"], 2)}
        if biggest_row is not None
        else None
    )

    return {
        "category_totals": category_totals,
        "total_spent": round(sum(category_totals.values()), 2),
        "receipt_count": count_row["n"],
        "biggest_expense": biggest_expense,
        "period_start": start_str,
        "period_end": period_end.isoformat(),
    }


# Spending timeline (for the chart)

def get_spending_timeline(period: str = "weekly") -> list[dict]:
    """Return spending grouped by time period for charting."""

    if period == "monthly":
        group_expr = "strftime('%Y-%m', r.date)"
    else:
        group_expr = "strftime('%Y-%m-%d', r.date, 'weekday 0', '-6 days')"

    try:
        with get_connection() as conn:
            total_rows = conn.execute(
                f"""
                SELECT {group_expr} AS period, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.date IS NOT NULL
                GROUP BY period
                ORDER BY period
                """
            ).fetchall()

            cat_rows = conn.execute(
                f"""
                SELECT {group_expr} AS period, i.category, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.date IS NOT NULL
                GROUP BY period, i.category
                ORDER BY period
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to build timeline: {exc}") from exc

    cat_map: dict[str, dict[str, float]] = {}
    for row in cat_rows:
        p = row["period"]
        if p not in cat_map:
            cat_map[p] = {}
        cat_map[p][row["category"]] = round(row["total"], 2)

    return [
        {
            "period": row["period"],
            "total": round(row["total"], 2),
            "categories": cat_map.get(row["period"], {}),
        }
        for row in total_rows
    ]

init_db()
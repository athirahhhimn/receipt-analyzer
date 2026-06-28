import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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

class DatabaseError(Exception):
    """Raised when any database operation fails.

    app.py catches this to return a 500 ("Database connection fails") response
    instead of letting the whole server crash.
    """


class DuplicateUsernameError(DatabaseError):
    """Raised by `create_user` when the username is already taken."""


def _ensure_parent_dir() -> None:
    parent = Path(DB_PATH).parent
    parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
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

def init_db() -> None:
    try:
        with get_connection() as conn:
            # users: one row per person using the app.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    username   TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            # receipts: one row per uploaded receipt, linked to a user.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    merchant   TEXT,
                    date       TEXT,
                    total      REAL,
                    currency   TEXT DEFAULT 'MYR',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
                """
            )
            # items: one row per line-item on a receipt, linked to a receipt.
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

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_category(category: str | None) -> str:
    if category is None:
        return "other"
    cleaned = category.strip().lower()
    return cleaned if cleaned in VALID_CATEGORIES else "other"

def create_user(username: str) -> dict:

    if not username or not username.strip():
        raise ValueError("username must not be empty")

    username = username.strip()
    created_at = _now_iso()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, created_at) VALUES (?, ?)",
                (username, created_at),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        # UNIQUE constraint on username failed -> name is taken.
        raise DuplicateUsernameError(f"username '{username}' is already taken") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to create user: {exc}") from exc

    return {"id": user_id, "username": username, "created_at": created_at}


def get_all_users() -> list[dict]:
 
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, username, created_at FROM users ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to list users: {exc}") from exc

    return [dict(row) for row in rows]


def get_user(user_id: int) -> dict | None:
   
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, username, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to fetch user {user_id}: {exc}") from exc

    return dict(row) if row is not None else None


def user_exists(user_id: int) -> bool:
  
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to check user {user_id}: {exc}") from exc

    return row is not None


def create_receipt(
    user_id: int,
    merchant: str | None,
    date: str | None,
    total: float | None,
    currency: str | None,
    items: list[dict],
) -> dict:
  
    currency = currency or DEFAULT_CURRENCY
    created_at = _now_iso()
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO receipts
                    (user_id, merchant, date, total, currency, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, merchant, date, total, currency, created_at),
            )
            receipt_id = cursor.lastrowid

            # Insert each item, normalising its category so it is always valid.
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
        # KeyError/ValueError/TypeError guard against malformed item dicts.
        raise DatabaseError(f"Failed to save receipt: {exc}") from exc

    # Return the full saved object (receipt + items) for the API response.
    saved = get_receipt(receipt_id)
    if saved is None:  # pragma: no cover - should never happen after insert
        raise DatabaseError("Receipt was saved but could not be read back")
    return saved


def get_receipts_for_user(user_id: int) -> list[dict]:
    
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, merchant, date, total, currency, created_at
                FROM receipts
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Failed to list receipts for user {user_id}: {exc}"
        ) from exc

    return [dict(row) for row in rows]


def get_receipt(receipt_id: int) -> dict | None:
  
    try:
        with get_connection() as conn:
            receipt_row = conn.execute(
                """
                SELECT id, user_id, merchant, date, total, currency, created_at
                FROM receipts
                WHERE id = ?
                """,
                (receipt_id,),
            ).fetchone()

            if receipt_row is None:
                return None

            item_rows = conn.execute(
                """
                SELECT id, receipt_id, name, price, category
                FROM items
                WHERE receipt_id = ?
                ORDER BY id
                """,
                (receipt_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to fetch receipt {receipt_id}: {exc}") from exc

    receipt = dict(receipt_row)
    receipt["items"] = [dict(row) for row in item_rows]
    return receipt


def delete_receipt(receipt_id: int) -> bool:
   
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM items WHERE receipt_id = ?", (receipt_id,))
            cursor = conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
            deleted = cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise DatabaseError(f"Failed to delete receipt {receipt_id}: {exc}") from exc

    return deleted


def get_analytics(user_id: int) -> dict:
 
    try:
        with get_connection() as conn:
            category_rows = conn.execute(
                """
                SELECT i.category AS category, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.user_id = ?
                GROUP BY i.category
                ORDER BY total DESC
                """,
                (user_id,),
            ).fetchall()

            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM receipts WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            range_row = conn.execute(
                """
                SELECT MIN(date) AS start, MAX(date) AS end
                FROM receipts
                WHERE user_id = ? AND date IS NOT NULL
                """,
                (user_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Failed to build analytics for user {user_id}: {exc}"
        ) from exc

    breakdown = {row["category"]: round(row["total"], 2) for row in category_rows}
    total_spent = round(sum(breakdown.values()), 2)

    return {
        "category_breakdown": breakdown,
        "total_spent": total_spent,
        "receipt_count": count_row["n"],
        "date_range": {"start": range_row["start"], "end": range_row["end"]},
    }


def get_spending_last_30_days(user_id: int) -> dict:

    period_end = date.today()
    period_start = period_end - timedelta(days=30)
    start_str = period_start.isoformat()
    end_str = period_end.isoformat()

    try:
        with get_connection() as conn:
            category_rows = conn.execute(
                """
                SELECT i.category AS category, SUM(i.price) AS total
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.user_id = ?
                  AND r.date IS NOT NULL
                  AND r.date >= ?
                GROUP BY i.category
                ORDER BY total DESC
                """,
                (user_id, start_str),
            ).fetchall()

            count_row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM receipts
                WHERE user_id = ? AND date IS NOT NULL AND date >= ?
                """,
                (user_id, start_str),
            ).fetchone()

            biggest_row = conn.execute(
                """
                SELECT i.name AS name, i.price AS price
                FROM items i
                JOIN receipts r ON i.receipt_id = r.id
                WHERE r.user_id = ?
                  AND r.date IS NOT NULL
                  AND r.date >= ?
                ORDER BY i.price DESC
                LIMIT 1
                """,
                (user_id, start_str),
            ).fetchone()
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Failed to build 30-day summary for user {user_id}: {exc}"
        ) from exc

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
        "period_end": end_str,
    }

init_db()


if __name__ == "__main__":

    print(f"Using database at: {DB_PATH}")
    print(f"Valid categories: {', '.join(VALID_CATEGORIES)}")

    demo_user = create_user(f"demo_{datetime.now(UTC).timestamp()}")
    print("Created user:", demo_user)

    demo_receipt = create_receipt(
        user_id=demo_user["id"],
        merchant="99 Speedmart",
        date=date.today().isoformat(),
        total=15.40,
        currency="MYR",
        items=[
            {"name": "Milo 3in1", "price": 12.90, "category": "food"},
            {"name": "Plastic bag", "price": 0.20, "category": "SHOPPING"},
            {"name": "Mystery item", "price": 2.30, "category": "???"},
        ],
    )
    print("Created receipt id:", demo_receipt["id"])
    print(
        "  items stored:", [(i["name"], i["category"]) for i in demo_receipt["items"]]
    )

    print("Receipts for user:", get_receipts_for_user(demo_user["id"]))
    print("Analytics:", get_analytics(demo_user["id"]))
    print("Last 30 days:", get_spending_last_30_days(demo_user["id"]))
    print("Deleted receipt:", delete_receipt(demo_receipt["id"]))
    print("Self-test finished OK.")
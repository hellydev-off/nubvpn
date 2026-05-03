from datetime import datetime, timezone
from typing import Any

import aiosqlite

DB_PATH = "db.sqlite3"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id      INTEGER PRIMARY KEY,
                marzban_username TEXT NOT NULL,
                added_by         INTEGER,
                added_at         TEXT,
                note             TEXT
            )
        """)
        await db.commit()


async def add_user(
    telegram_id: int,
    marzban_username: str,
    added_by: int,
    note: str = "",
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, marzban_username, added_by, added_at, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                marzban_username = excluded.marzban_username,
                added_by         = excluded.added_by,
                note             = excluded.note
            """,
            (
                telegram_id,
                marzban_username,
                added_by,
                datetime.now(timezone.utc).isoformat(),
                note,
            ),
        )
        await db.commit()


async def remove_user(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        await db.commit()


async def get_user(telegram_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_users() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users ORDER BY added_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_users_page(offset: int, limit: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ClubUser:
    telegram_id: int
    username: str | None
    display_name: str
    profile_id: int
    profile_url: str


@dataclass(frozen=True)
class Booking:
    id: int
    booking_date: str
    hour: int
    telegram_id: int
    username: str | None
    display_name: str


@dataclass(frozen=True)
class ChatMember:
    chat_id: int
    telegram_id: int
    username: str | None
    full_name: str


class Storage:
    def __init__(self, path: str):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT NOT NULL,
                profile_id INTEGER NOT NULL UNIQUE,
                profile_url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_date TEXT NOT NULL,
                hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(booking_date, hour)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, telegram_id)
            );
            """
        )
        self.conn.commit()

    def get_user(self, telegram_id: int) -> ClubUser | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return _user_from_row(row) if row else None

    def profile_exists(self, profile_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM users WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        return row is not None

    def add_user(
        self,
        telegram_id: int,
        username: str | None,
        display_name: str,
        profile_id: int,
        profile_url: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO users (telegram_id, username, display_name, profile_id, profile_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, username, display_name, profile_id, profile_url),
        )
        self.conn.commit()

    def list_users(self) -> list[ClubUser]:
        rows = self.conn.execute(
            "SELECT * FROM users ORDER BY LOWER(display_name), telegram_id"
        ).fetchall()
        return [_user_from_row(row) for row in rows]

    def list_bookings(self, dates: Iterable[date]) -> list[Booking]:
        date_values = [item.isoformat() for item in dates]
        if not date_values:
            return []
        placeholders = ",".join("?" for _ in date_values)
        rows = self.conn.execute(
            f"""
            SELECT b.*, u.username, u.display_name
            FROM bookings b
            JOIN users u ON u.telegram_id = b.telegram_id
            WHERE b.booking_date IN ({placeholders})
            ORDER BY b.booking_date, b.hour
            """,
            date_values,
        ).fetchall()
        return [_booking_from_row(row) for row in rows]

    def get_booking(self, booking_date: date, hour: int) -> Booking | None:
        row = self.conn.execute(
            """
            SELECT b.*, u.username, u.display_name
            FROM bookings b
            JOIN users u ON u.telegram_id = b.telegram_id
            WHERE b.booking_date = ? AND b.hour = ?
            """,
            (booking_date.isoformat(), hour),
        ).fetchone()
        return _booking_from_row(row) if row else None

    def add_booking(self, booking_date: date, hour: int, telegram_id: int) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO bookings (booking_date, hour, telegram_id) VALUES (?, ?, ?)",
                (booking_date.isoformat(), hour, telegram_id),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_booking(self, booking_date: date, hour: int, telegram_id: int) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM bookings
            WHERE booking_date = ? AND hour = ? AND telegram_id = ?
            """,
            (booking_date.isoformat(), hour, telegram_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def upsert_chat_member(
        self,
        chat_id: int,
        telegram_id: int,
        username: str | None,
        full_name: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO chat_members (chat_id, telegram_id, username, full_name, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, telegram_id, username, full_name),
        )
        self.conn.commit()

    def list_chat_members(self, chat_id: int) -> list[ChatMember]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM chat_members
            WHERE chat_id = ?
            ORDER BY LOWER(full_name), telegram_id
            """,
            (chat_id,),
        ).fetchall()
        return [_chat_member_from_row(row) for row in rows]

    def delete_chat_member(self, chat_id: int, telegram_id: int) -> None:
        self.conn.execute(
            "DELETE FROM chat_members WHERE chat_id = ? AND telegram_id = ?",
            (chat_id, telegram_id),
        )
        self.conn.commit()


def _user_from_row(row: sqlite3.Row) -> ClubUser:
    return ClubUser(
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"],
        profile_id=row["profile_id"],
        profile_url=row["profile_url"],
    )


def _booking_from_row(row: sqlite3.Row) -> Booking:
    return Booking(
        id=row["id"],
        booking_date=row["booking_date"],
        hour=row["hour"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"],
    )


def _chat_member_from_row(row: sqlite3.Row) -> ChatMember:
    return ChatMember(
        chat_id=row["chat_id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        full_name=row["full_name"],
    )

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ClubUser:
    telegram_id: int
    username: str | None
    display_name: str
    profile_id: int
    profile_url: str


@dataclass(frozen=True)
class RegistrationRequest:
    telegram_id: int
    username: str | None
    telegram_name: str
    display_name: str
    profile_id: int
    profile_url: str
    created_at: str


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


@dataclass(frozen=True)
class Giveaway:
    id: int
    author_telegram_id: int
    author_username: str | None
    author_display_name: str
    title: str
    description: str
    media_type: str | None
    media_file_id: str | None
    winner_count: int
    ends_at: str
    status: str
    chat_id: int | None
    thread_id: int | None
    message_id: int | None
    participant_count: int
    created_at: str
    completed_at: str | None


class Storage:
    def __init__(self, url_or_path: str):
        self.is_postgres = url_or_path.startswith(("postgres://", "postgresql://"))
        self.placeholder = "%s" if self.is_postgres else "?"
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row

            self.integrity_error = psycopg.IntegrityError
            self.conn = psycopg.connect(url_or_path, row_factory=dict_row)
        else:
            self.integrity_error = sqlite3.IntegrityError
            self.path = Path(url_or_path)
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def init(self) -> None:
        if self.is_postgres:
            self._init_postgres()
        else:
            self._init_sqlite()

    def _init_sqlite(self) -> None:
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

            CREATE TABLE IF NOT EXISTS registration_requests (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                telegram_name TEXT NOT NULL,
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

            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                media_type TEXT,
                media_file_id TEXT,
                winner_count INTEGER NOT NULL CHECK(winner_count > 0),
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                chat_id INTEGER,
                thread_id INTEGER,
                message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (giveaway_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                PRIMARY KEY (giveaway_id, telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_giveaways_status_ends
            ON giveaways(status, ends_at);
            """
        )
        self.conn.commit()

    def _init_postgres(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                display_name TEXT NOT NULL,
                profile_id BIGINT NOT NULL UNIQUE,
                profile_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS registration_requests (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                telegram_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                profile_id BIGINT NOT NULL UNIQUE,
                profile_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id BIGSERIAL PRIMARY KEY,
                booking_date TEXT NOT NULL,
                hour INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(booking_date, hour)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id BIGINT NOT NULL,
                telegram_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, telegram_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id BIGSERIAL PRIMARY KEY,
                author_telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                media_type TEXT,
                media_file_id TEXT,
                winner_count INTEGER NOT NULL CHECK(winner_count > 0),
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                chat_id BIGINT,
                thread_id BIGINT,
                message_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_id BIGINT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (giveaway_id, telegram_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id BIGINT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                PRIMARY KEY (giveaway_id, telegram_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_giveaways_status_ends
            ON giveaways(status, ends_at)
            """,
        ]
        for statement in statements:
            self.conn.execute(statement)
        self.conn.commit()

    def _ph(self, count: int) -> str:
        return ",".join(self.placeholder for _ in range(count))

    def get_user(self, telegram_id: int) -> ClubUser | None:
        row = self.conn.execute(
            f"SELECT * FROM users WHERE telegram_id = {self.placeholder}",
            (telegram_id,),
        ).fetchone()
        return _user_from_row(row) if row else None

    def profile_exists(self, profile_id: int) -> bool:
        row = self.conn.execute(
            f"SELECT 1 FROM users WHERE profile_id = {self.placeholder}",
            (profile_id,),
        ).fetchone()
        return row is not None

    def get_registration_request(
        self,
        telegram_id: int,
    ) -> RegistrationRequest | None:
        row = self.conn.execute(
            f"""
            SELECT *
            FROM registration_requests
            WHERE telegram_id = {self.placeholder}
            """,
            (telegram_id,),
        ).fetchone()
        return _registration_request_from_row(row) if row else None

    def list_registration_requests(self) -> list[RegistrationRequest]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM registration_requests
            ORDER BY created_at, telegram_id
            """
        ).fetchall()
        return [_registration_request_from_row(row) for row in rows]

    def create_registration_request(
        self,
        telegram_id: int,
        username: str | None,
        telegram_name: str,
        display_name: str,
        profile_id: int,
        profile_url: str,
    ) -> bool:
        try:
            self.conn.execute(
                f"""
                DELETE FROM registration_requests
                WHERE telegram_id = {self.placeholder}
                """,
                (telegram_id,),
            )
            self.conn.execute(
                f"""
                INSERT INTO registration_requests (
                    telegram_id, username, telegram_name, display_name,
                    profile_id, profile_url
                )
                VALUES ({self._ph(6)})
                """,
                (
                    telegram_id,
                    username,
                    telegram_name,
                    display_name,
                    profile_id,
                    profile_url,
                ),
            )
            self.conn.commit()
            return True
        except self.integrity_error:
            self.conn.rollback()
            return False

    def approve_registration(self, telegram_id: int) -> ClubUser | None:
        request = self.get_registration_request(telegram_id)
        if not request:
            return None
        try:
            self.conn.execute(
                f"""
                INSERT INTO users (
                    telegram_id, username, display_name, profile_id, profile_url
                )
                VALUES ({self._ph(5)})
                """,
                (
                    request.telegram_id,
                    request.username,
                    request.display_name,
                    request.profile_id,
                    request.profile_url,
                ),
            )
            self.conn.execute(
                f"""
                DELETE FROM registration_requests
                WHERE telegram_id = {self.placeholder}
                """,
                (telegram_id,),
            )
            self.conn.commit()
        except self.integrity_error:
            self.conn.rollback()
            return None
        return self.get_user(telegram_id)

    def reject_registration(
        self,
        telegram_id: int,
    ) -> RegistrationRequest | None:
        request = self.get_registration_request(telegram_id)
        if not request:
            return None
        self.conn.execute(
            f"""
            DELETE FROM registration_requests
            WHERE telegram_id = {self.placeholder}
            """,
            (telegram_id,),
        )
        self.conn.commit()
        return request

    def add_user(
        self,
        telegram_id: int,
        username: str | None,
        display_name: str,
        profile_id: int,
        profile_url: str,
    ) -> None:
        self.conn.execute(
            f"""
            INSERT INTO users (telegram_id, username, display_name, profile_id, profile_url)
            VALUES ({self._ph(5)})
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
        rows = self.conn.execute(
            f"""
            SELECT b.*, u.username, u.display_name
            FROM bookings b
            JOIN users u ON u.telegram_id = b.telegram_id
            WHERE b.booking_date IN ({self._ph(len(date_values))})
            ORDER BY b.booking_date, b.hour
            """,
            date_values,
        ).fetchall()
        return [_booking_from_row(row) for row in rows]

    def get_booking(self, booking_date: date, hour: int) -> Booking | None:
        row = self.conn.execute(
            f"""
            SELECT b.*, u.username, u.display_name
            FROM bookings b
            JOIN users u ON u.telegram_id = b.telegram_id
            WHERE b.booking_date = {self.placeholder} AND b.hour = {self.placeholder}
            """,
            (booking_date.isoformat(), hour),
        ).fetchone()
        return _booking_from_row(row) if row else None

    def add_booking(self, booking_date: date, hour: int, telegram_id: int) -> bool:
        try:
            self.conn.execute(
                f"""
                INSERT INTO bookings (booking_date, hour, telegram_id)
                VALUES ({self._ph(3)})
                """,
                (booking_date.isoformat(), hour, telegram_id),
            )
            self.conn.commit()
            return True
        except self.integrity_error:
            self.conn.rollback()
            return False

    def delete_booking(self, booking_date: date, hour: int, telegram_id: int) -> bool:
        cursor = self.conn.execute(
            f"""
            DELETE FROM bookings
            WHERE booking_date = {self.placeholder}
                AND hour = {self.placeholder}
                AND telegram_id = {self.placeholder}
            """,
            (booking_date.isoformat(), hour, telegram_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            f"""
            INSERT INTO settings (key, value)
            VALUES ({self._ph(2)})
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute(
            f"SELECT value FROM settings WHERE key = {self.placeholder}",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else None

    def create_giveaway(
        self,
        author_telegram_id: int,
        title: str,
        description: str,
        media_type: str | None,
        media_file_id: str | None,
        winner_count: int,
        ends_at: str,
        chat_id: int,
        thread_id: int | None,
    ) -> int:
        params = (
            author_telegram_id,
            title,
            description,
            media_type,
            media_file_id,
            winner_count,
            ends_at,
            chat_id,
            thread_id,
        )
        sql = f"""
            INSERT INTO giveaways (
                author_telegram_id, title, description, media_type, media_file_id,
                winner_count, ends_at, chat_id, thread_id
            )
            VALUES ({self._ph(9)})
        """
        if self.is_postgres:
            row = self.conn.execute(f"{sql} RETURNING id", params).fetchone()
            giveaway_id = int(row["id"])
        else:
            cursor = self.conn.execute(sql, params)
            giveaway_id = int(cursor.lastrowid)
        self.conn.commit()
        return giveaway_id

    def set_giveaway_message(self, giveaway_id: int, message_id: int) -> None:
        self.conn.execute(
            f"""
            UPDATE giveaways
            SET message_id = {self.placeholder}
            WHERE id = {self.placeholder}
            """,
            (message_id, giveaway_id),
        )
        self.conn.commit()

    def get_giveaway(self, giveaway_id: int) -> Giveaway | None:
        row = self.conn.execute(
            f"""
            SELECT g.*, u.username AS author_username,
                   u.display_name AS author_display_name,
                   (
                       SELECT COUNT(*)
                       FROM giveaway_participants gp
                       WHERE gp.giveaway_id = g.id
                   ) AS participant_count
            FROM giveaways g
            JOIN users u ON u.telegram_id = g.author_telegram_id
            WHERE g.id = {self.placeholder}
            """,
            (giveaway_id,),
        ).fetchone()
        return _giveaway_from_row(row) if row else None

    def list_giveaways(
        self,
        statuses: Iterable[str],
        limit: int = 20,
    ) -> list[Giveaway]:
        status_values = list(statuses)
        if not status_values:
            return []
        rows = self.conn.execute(
            f"""
            SELECT g.*, u.username AS author_username,
                   u.display_name AS author_display_name,
                   (
                       SELECT COUNT(*)
                       FROM giveaway_participants gp
                       WHERE gp.giveaway_id = g.id
                   ) AS participant_count
            FROM giveaways g
            JOIN users u ON u.telegram_id = g.author_telegram_id
            WHERE g.status IN ({self._ph(len(status_values))})
            ORDER BY
                CASE WHEN g.status = 'active' THEN g.ends_at END ASC,
                g.id DESC
            LIMIT {self.placeholder}
            """,
            (*status_values, limit),
        ).fetchall()
        return [_giveaway_from_row(row) for row in rows]

    def list_due_giveaways(self, now_iso: str) -> list[Giveaway]:
        rows = self.conn.execute(
            f"""
            SELECT g.*, u.username AS author_username,
                   u.display_name AS author_display_name,
                   (
                       SELECT COUNT(*)
                       FROM giveaway_participants gp
                       WHERE gp.giveaway_id = g.id
                   ) AS participant_count
            FROM giveaways g
            JOIN users u ON u.telegram_id = g.author_telegram_id
            WHERE g.status = 'active' AND g.ends_at <= {self.placeholder}
            ORDER BY g.ends_at
            """,
            (now_iso,),
        ).fetchall()
        return [_giveaway_from_row(row) for row in rows]

    def add_giveaway_participant(self, giveaway_id: int, telegram_id: int) -> bool:
        try:
            self.conn.execute(
                f"""
                INSERT INTO giveaway_participants (giveaway_id, telegram_id)
                VALUES ({self._ph(2)})
                """,
                (giveaway_id, telegram_id),
            )
            self.conn.commit()
            return True
        except self.integrity_error:
            self.conn.rollback()
            return False

    def list_giveaway_participants(self, giveaway_id: int) -> list[ClubUser]:
        rows = self.conn.execute(
            f"""
            SELECT u.*
            FROM giveaway_participants gp
            JOIN users u ON u.telegram_id = gp.telegram_id
            WHERE gp.giveaway_id = {self.placeholder}
            ORDER BY gp.joined_at, gp.telegram_id
            """,
            (giveaway_id,),
        ).fetchall()
        return [_user_from_row(row) for row in rows]

    def claim_giveaway(self, giveaway_id: int) -> bool:
        cursor = self.conn.execute(
            f"""
            UPDATE giveaways
            SET status = 'drawing'
            WHERE id = {self.placeholder} AND status = 'active'
            """,
            (giveaway_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def restore_active_giveaway(self, giveaway_id: int) -> None:
        self.conn.execute(
            f"""
            UPDATE giveaways
            SET status = 'active'
            WHERE id = {self.placeholder} AND status = 'drawing'
            """,
            (giveaway_id,),
        )
        self.conn.commit()

    def recover_drawing_giveaways(self) -> None:
        self.conn.execute(
            """
            UPDATE giveaways
            SET status = 'active'
            WHERE status = 'drawing'
            """
        )
        self.conn.commit()

    def complete_giveaway(
        self,
        giveaway_id: int,
        winner_ids: Iterable[int],
        completed_at: str,
    ) -> None:
        try:
            for position, telegram_id in enumerate(winner_ids, start=1):
                self.conn.execute(
                    f"""
                    INSERT INTO giveaway_winners (giveaway_id, telegram_id, position)
                    VALUES ({self._ph(3)})
                    ON CONFLICT(giveaway_id, telegram_id) DO NOTHING
                    """,
                    (giveaway_id, telegram_id, position),
                )
            self.conn.execute(
                f"""
                UPDATE giveaways
                SET status = 'completed', completed_at = {self.placeholder}
                WHERE id = {self.placeholder}
                """,
                (completed_at, giveaway_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def cancel_giveaway(self, giveaway_id: int, completed_at: str) -> bool:
        cursor = self.conn.execute(
            f"""
            UPDATE giveaways
            SET status = 'cancelled', completed_at = {self.placeholder}
            WHERE id = {self.placeholder} AND status = 'active'
            """,
            (completed_at, giveaway_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def list_giveaway_winners(self, giveaway_id: int) -> list[ClubUser]:
        rows = self.conn.execute(
            f"""
            SELECT u.*
            FROM giveaway_winners gw
            JOIN users u ON u.telegram_id = gw.telegram_id
            WHERE gw.giveaway_id = {self.placeholder}
            ORDER BY gw.position
            """,
            (giveaway_id,),
        ).fetchall()
        return [_user_from_row(row) for row in rows]

    def upsert_chat_member(
        self,
        chat_id: int,
        telegram_id: int,
        username: str | None,
        full_name: str,
    ) -> None:
        self.conn.execute(
            f"""
            INSERT INTO chat_members (chat_id, telegram_id, username, full_name, updated_at)
            VALUES ({self._ph(4)}, CURRENT_TIMESTAMP)
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
            f"""
            SELECT *
            FROM chat_members
            WHERE chat_id = {self.placeholder}
            ORDER BY LOWER(full_name), telegram_id
            """,
            (chat_id,),
        ).fetchall()
        return [_chat_member_from_row(row) for row in rows]

    def delete_chat_member(self, chat_id: int, telegram_id: int) -> None:
        self.conn.execute(
            f"""
            DELETE FROM chat_members
            WHERE chat_id = {self.placeholder} AND telegram_id = {self.placeholder}
            """,
            (chat_id, telegram_id),
        )
        self.conn.commit()


def _user_from_row(row: Any) -> ClubUser:
    return ClubUser(
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"],
        profile_id=row["profile_id"],
        profile_url=row["profile_url"],
    )


def _registration_request_from_row(row: Any) -> RegistrationRequest:
    return RegistrationRequest(
        telegram_id=int(row["telegram_id"]),
        username=row["username"],
        telegram_name=row["telegram_name"],
        display_name=row["display_name"],
        profile_id=int(row["profile_id"]),
        profile_url=row["profile_url"],
        created_at=str(row["created_at"]),
    )


def _booking_from_row(row: Any) -> Booking:
    return Booking(
        id=row["id"],
        booking_date=row["booking_date"],
        hour=row["hour"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        display_name=row["display_name"],
    )


def _chat_member_from_row(row: Any) -> ChatMember:
    return ChatMember(
        chat_id=row["chat_id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        full_name=row["full_name"],
    )


def _giveaway_from_row(row: Any) -> Giveaway:
    return Giveaway(
        id=int(row["id"]),
        author_telegram_id=int(row["author_telegram_id"]),
        author_username=row["author_username"],
        author_display_name=row["author_display_name"],
        title=row["title"],
        description=row["description"],
        media_type=row["media_type"],
        media_file_id=row["media_file_id"],
        winner_count=int(row["winner_count"]),
        ends_at=str(row["ends_at"]),
        status=row["status"],
        chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
        thread_id=int(row["thread_id"]) if row["thread_id"] is not None else None,
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        participant_count=int(row["participant_count"]),
        created_at=str(row["created_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
    )

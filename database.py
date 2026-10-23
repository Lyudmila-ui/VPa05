"""SQLite storage for reminders."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

STATUS_PENDING = "Ожидает"
STATUS_DONE = "Готово"
STATUS_OVERDUE = "Просрочено"
STATUS_CANCELLED = "Отменено"
STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_OVERDUE, STATUS_CANCELLED)

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def default_db_path() -> Path:
    return Path(__file__).resolve().parent / "reminders.db"


@dataclass(frozen=True)
class Reminder:
    id: int
    title: str
    description: str
    trigger_at: datetime
    status: str
    notified: bool


class ReminderDatabase:
    """Creates the schema on startup and keeps reminder rows consistent."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    trigger_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Ожидает'
                        CHECK (status IN ('Ожидает', 'Готово', 'Просрочено', 'Отменено')),
                    notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1)),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminders_status_trigger
                ON reminders (status, trigger_at)
                """
            )

    def add(self, title: str, description: str, trigger_at: datetime) -> Reminder:
        created_at = _format_time(datetime.now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reminders (title, description, trigger_at, status, notified, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (title, description, _format_time(trigger_at), STATUS_PENDING, created_at),
            )
            reminder_id = int(cursor.lastrowid)
        reminder = self.get(reminder_id)
        if reminder is None:
            raise RuntimeError("Напоминание не сохранилось.")
        return reminder

    def get(self, reminder_id: int) -> Reminder | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, title, description, trigger_at, status, notified
                FROM reminders
                WHERE id = ?
                """,
                (reminder_id,),
            ).fetchone()
        return _reminder_from_row(row) if row is not None else None

    def list_reminders(self, status: str | None = None) -> list[Reminder]:
        if status is not None and status not in STATUSES:
            raise ValueError(status)
        query = """
            SELECT id, title, description, trigger_at, status, notified
            FROM reminders
        """
        parameters: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY trigger_at ASC, id ASC"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_reminder_from_row(row) for row in rows]

    def next_pending(self) -> Reminder | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, title, description, trigger_at, status, notified
                FROM reminders
                WHERE status = ?
                ORDER BY trigger_at ASC, id ASC
                LIMIT 1
                """,
                (STATUS_PENDING,),
            ).fetchone()
        return _reminder_from_row(row) if row is not None else None

    def set_status(self, reminder_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(status)
        notified = 0 if status == STATUS_PENDING else 1
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET status = ?, notified = ?
                WHERE id = ?
                """,
                (status, notified, reminder_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(reminder_id)

    def delete(self, reminder_id: int) -> None:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            if cursor.rowcount == 0:
                raise KeyError(reminder_id)

    def claim_due(self, now: datetime) -> list[Reminder]:
        """Mark waiting reminders whose time has come as overdue, once.

        Returns the reminders that should be shown now. A second call does not
        return them again.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, description, trigger_at, status, notified
                FROM reminders
                WHERE status = ? AND notified = 0 AND trigger_at <= ?
                ORDER BY trigger_at ASC, id ASC
                """,
                (STATUS_PENDING, _format_time(now)),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE reminders
                    SET status = ?, notified = 1
                    WHERE id = ?
                    """,
                    [(STATUS_OVERDUE, int(row["id"])) for row in rows],
                )
        return [_reminder_from_row(row) for row in rows]


def _format_time(value: datetime) -> str:
    return value.strftime(_TIME_FORMAT)


def _reminder_from_row(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=int(row["id"]),
        title=str(row["title"]),
        description=str(row["description"]),
        trigger_at=datetime.strptime(str(row["trigger_at"]), _TIME_FORMAT),
        status=str(row["status"]),
        notified=bool(row["notified"]),
    )

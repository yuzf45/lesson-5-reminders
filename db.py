"""Хранилище напоминаний в SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

STATUS_PENDING = "Ожидает"
STATUS_DONE = "Готово"
STATUS_OVERDUE = "Просрочено"
STATUS_CANCELLED = "Отменено"

STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_OVERDUE, STATUS_CANCELLED)

_DUE_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_due(value: str) -> str:
    """Показать сохранённую метку времени как ДД.ММ.ГГГГ ЧЧ:ММ."""
    return datetime.strptime(value, _DUE_FORMAT).strftime("%d.%m.%Y %H:%M")


class ReminderDB:
    """Соединение с базой. Таблица создаётся при открытии, если её ещё нет."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Ожидает'
                    CHECK (status IN ('Ожидает', 'Готово', 'Просрочено', 'Отменено')),
                notified INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1)),
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def add(self, title: str, description: str, due_at: datetime) -> int:
        created_at = datetime.now().strftime(_DUE_FORMAT)
        cursor = self.conn.execute(
            """
            INSERT INTO reminders (title, description, due_at, status, notified, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (title, description, due_at.strftime(_DUE_FORMAT), STATUS_PENDING, created_at),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get(self, reminder_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_reminders(self, status: str | None = None) -> list[dict]:
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"Неизвестный статус: {status}")
            rows = self.conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = ?
                ORDER BY due_at ASC, id ASC
                """,
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM reminders
                ORDER BY due_at ASC, id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, reminder_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM reminders WHERE id = ?",
            (reminder_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_status(self, reminder_id: int, status: str) -> bool:
        if status not in (STATUS_DONE, STATUS_CANCELLED, STATUS_PENDING, STATUS_OVERDUE):
            raise ValueError(f"Неизвестный статус: {status}")
        # Ручная смена статуса гасит повторное всплытие.
        cursor = self.conn.execute(
            """
            UPDATE reminders
            SET status = ?, notified = 1
            WHERE id = ?
            """,
            (status, reminder_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_overdue(self, now: datetime | None = None) -> int:
        """Перевести ожидающие напоминания с наступившим сроком в «Просрочено»."""
        moment = (now or datetime.now()).strftime(_DUE_FORMAT)
        cursor = self.conn.execute(
            """
            UPDATE reminders
            SET status = ?
            WHERE status = ? AND due_at <= ?
            """,
            (STATUS_OVERDUE, STATUS_PENDING, moment),
        )
        self.conn.commit()
        return cursor.rowcount

    def due_unnotified(self, now: datetime | None = None) -> list[dict]:
        """Просроченные напоминания, для которых уведомление ещё не показывали."""
        moment = (now or datetime.now()).strftime(_DUE_FORMAT)
        rows = self.conn.execute(
            """
            SELECT * FROM reminders
            WHERE status = ? AND notified = 0 AND due_at <= ?
            ORDER BY due_at ASC, id ASC
            """,
            (STATUS_OVERDUE, moment),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_notified(self, reminder_id: int) -> None:
        self.conn.execute(
            "UPDATE reminders SET notified = 1 WHERE id = ?",
            (reminder_id,),
        )
        self.conn.commit()

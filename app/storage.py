"""SQLite: записи (основное хранилище) и журнал диалогов."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    ip_hash       TEXT NOT NULL DEFAULT '',
    service_id    TEXT NOT NULL,
    service_name  TEXT NOT NULL,
    price_from    INTEGER NOT NULL DEFAULT 0,
    visit_date    TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT NOT NULL,
    client_name   TEXT NOT NULL,
    phone         TEXT NOT NULL,
    extra_json    TEXT NOT NULL DEFAULT '{}',
    comment       TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL DEFAULT 'web',
    status        TEXT NOT NULL DEFAULT 'new',
    sheet_synced  INTEGER NOT NULL DEFAULT 0,
    parts_choice  TEXT NOT NULL DEFAULT '',
    estimate_from INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(visit_date);
CREATE INDEX IF NOT EXISTS idx_bookings_sync ON bookings(sheet_synced);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""

# Колонки, добавленные после первой версии: старые базы дополняются при запуске.
MIGRATIONS = {
    "parts_choice": "ALTER TABLE bookings ADD COLUMN parts_choice TEXT NOT NULL DEFAULT ''",
    "estimate_from": "ALTER TABLE bookings ADD COLUMN estimate_from INTEGER NOT NULL DEFAULT 0",
}


@dataclass
class Booking:
    id: int
    created_at: datetime
    session_id: str
    ip_hash: str
    service_id: str
    service_name: str
    price_from: int
    visit_date: date
    start_time: time
    end_time: time
    client_name: str
    phone: str
    extra: dict[str, str] = field(default_factory=dict)
    comment: str = ""
    summary: str = ""
    channel: str = "web"
    status: str = "new"
    sheet_synced: bool = False
    parts_choice: str = ""
    estimate_from: int = 0


def _row_to_booking(row: sqlite3.Row) -> Booking:
    return Booking(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        session_id=row["session_id"],
        ip_hash=row["ip_hash"],
        service_id=row["service_id"],
        service_name=row["service_name"],
        price_from=row["price_from"],
        visit_date=date.fromisoformat(row["visit_date"]),
        start_time=time.fromisoformat(row["start_time"]),
        end_time=time.fromisoformat(row["end_time"]),
        client_name=row["client_name"],
        phone=row["phone"],
        extra=json.loads(row["extra_json"] or "{}"),
        comment=row["comment"],
        summary=row["summary"],
        channel=row["channel"],
        status=row["status"],
        sheet_synced=bool(row["sheet_synced"]),
        parts_choice=row["parts_choice"],
        estimate_from=row["estimate_from"],
    )


class Storage:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(bookings)")}
            for column, statement in MIGRATIONS.items():
                if column not in columns:
                    self._conn.execute(statement)
            self._conn.commit()
            self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- записи --------------------------------------------------------

    def add_booking(self, booking: Booking) -> Booking:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO bookings (created_at, session_id, ip_hash, service_id, service_name, price_from,
                       visit_date, start_time, end_time, client_name, phone, extra_json, comment, summary,
                       channel, status, parts_choice, estimate_from)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    booking.created_at.isoformat(timespec="seconds"),
                    booking.session_id,
                    booking.ip_hash,
                    booking.service_id,
                    booking.service_name,
                    booking.price_from,
                    booking.visit_date.isoformat(),
                    booking.start_time.strftime("%H:%M"),
                    booking.end_time.strftime("%H:%M"),
                    booking.client_name,
                    booking.phone,
                    json.dumps(booking.extra, ensure_ascii=False),
                    booking.comment,
                    booking.summary,
                    booking.channel,
                    booking.status,
                    booking.parts_choice,
                    booking.estimate_from,
                ),
            )
            self._conn.commit()
            booking.id = int(cur.lastrowid)
        return booking

    def day_intervals(self, d: date) -> list[tuple[time, time]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT start_time, end_time FROM bookings WHERE visit_date = ? AND status != 'cancelled'",
                (d.isoformat(),),
            ).fetchall()
        return [(time.fromisoformat(r["start_time"]), time.fromisoformat(r["end_time"])) for r in rows]

    def recent_bookings(self, limit: int = 20) -> list[Booking]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_booking(r) for r in rows]

    def unsynced_bookings(self, limit: int = 50) -> list[Booking]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bookings WHERE sheet_synced = 0 ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_booking(r) for r in rows]

    def mark_synced(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._conn.executemany("UPDATE bookings SET sheet_synced = 1 WHERE id = ?", [(i,) for i in ids])
            self._conn.commit()

    def count_bookings_by_ip_since(self, ip_hash: str, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM bookings WHERE ip_hash = ? AND created_at >= ?",
                (ip_hash, since.isoformat(timespec="seconds")),
            ).fetchone()
        return int(row["n"])

    # --- журнал диалогов ------------------------------------------------

    def log_message(self, session_id: str, role: str, content: str, at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, created_at, role, content) VALUES (?, ?, ?, ?)",
                (session_id, at.isoformat(timespec="seconds"), role, content),
            )
            self._conn.commit()

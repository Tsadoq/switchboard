import sqlite3
from dataclasses import dataclass

PRESENCE_WINDOW_SECONDS = 120


@dataclass(frozen=True)
class Presence:
    last_seen_at: float | None = None
    last_poll_at: float | None = None

    def online(self, now: float) -> bool:
        return self.last_poll_at is not None and now - self.last_poll_at <= PRESENCE_WINDOW_SECONDS


def record_seen(conn: sqlite3.Connection, principal: str, at: float) -> None:
    conn.execute(
        "INSERT INTO principals (principal, last_seen_at) VALUES (?, ?) "
        "ON CONFLICT (principal) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (principal, at),
    )


def record_poll(conn: sqlite3.Connection, principal: str, at: float) -> None:
    conn.execute(
        "INSERT INTO principals (principal, last_poll_at) VALUES (?, ?) "
        "ON CONFLICT (principal) DO UPDATE SET last_poll_at = excluded.last_poll_at",
        (principal, at),
    )


def everyone(conn: sqlite3.Connection) -> dict[str, Presence]:
    rows = conn.execute("SELECT principal, last_seen_at, last_poll_at FROM principals")
    return {row["principal"]: Presence(row["last_seen_at"], row["last_poll_at"]) for row in rows}

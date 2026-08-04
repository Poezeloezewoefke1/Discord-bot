"""SQLite persistence. Every SQL statement in the project lives in this file.

Plain stdlib sqlite3 on purpose — no ORM, no database server to run, and the
queries stay readable for whoever picks this up next.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from config import DB_PATH

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_COMPLETE = "complete"

KIND_PROGRESS = "progress"
KIND_HANDOFF = "handoff"
KIND_COMPLETE = "complete"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            INTEGER PRIMARY KEY,
    builder_role_id     INTEGER,
    scripter_role_id    INTEGER,
    requests_channel_id INTEGER,
    board_channel_id    INTEGER,
    board_message_id    INTEGER
);

CREATE TABLE IF NOT EXISTS builds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    requested_by INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    claimed_by   INTEGER,
    claimed_at   TEXT,
    message_id   INTEGER,
    thread_id    INTEGER,
    created_at   TEXT    NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS updates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id   INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    builder_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    note       TEXT,
    file_name  TEXT,
    file_path  TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_builds_guild_status ON builds (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_updates_build ON updates (build_id, id);
"""

_db_path = DB_PATH


def set_db_path(path) -> None:
    """Point the module at a different database file. Used by the tests."""
    global _db_path
    _db_path = path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_unix(iso: str | None) -> int | None:
    """ISO-8601 -> unix seconds, for Discord's <t:...:R> relative timestamps."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# guild config
# --------------------------------------------------------------------------

def get_config(guild_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()


def save_config(guild_id: int, **fields) -> None:
    """Upsert the guild's configuration, leaving unspecified columns untouched."""
    allowed = {
        "builder_role_id",
        "scripter_role_id",
        "requests_channel_id",
        "board_channel_id",
        "board_message_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown config fields: {sorted(unknown)}")

    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
        )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            conn.execute(
                f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
                (*fields.values(), guild_id),
            )


# --------------------------------------------------------------------------
# builds
# --------------------------------------------------------------------------

def create_build(guild_id: int, title: str, description: str, requested_by: int) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO builds (guild_id, title, description, requested_by, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, title, description, requested_by, STATUS_OPEN, now_iso()),
        )
        return int(cur.lastrowid)


def get_build(build_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()


def attach_message(build_id: int, message_id: int, thread_id: int | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE builds SET message_id = ?, thread_id = ? WHERE id = ?",
            (message_id, thread_id, build_id),
        )


def claim_build(build_id: int, user_id: int) -> bool:
    """Atomically take an open build. Returns False if somebody else already has it.

    This single conditional UPDATE is the whole anti-duplicate-work guarantee:
    two builders clicking Claim in the same second cannot both win, because the
    second statement matches zero rows once status is no longer 'open'.
    """
    with connect() as conn:
        cur = conn.execute(
            """UPDATE builds
                  SET status = ?, claimed_by = ?, claimed_at = ?
                WHERE id = ? AND status = ?""",
            (STATUS_CLAIMED, user_id, now_iso(), build_id, STATUS_OPEN),
        )
        return cur.rowcount == 1


def release_build(build_id: int, user_id: int | None = None) -> bool:
    """Send a claimed build back to the open pool.

    Passing user_id restricts the release to that builder's own claim; passing
    None force-releases (used by admins and by the handoff flow).
    """
    with connect() as conn:
        if user_id is None:
            cur = conn.execute(
                """UPDATE builds SET status = ?, claimed_by = NULL, claimed_at = NULL
                    WHERE id = ? AND status = ?""",
                (STATUS_OPEN, build_id, STATUS_CLAIMED),
            )
        else:
            cur = conn.execute(
                """UPDATE builds SET status = ?, claimed_by = NULL, claimed_at = NULL
                    WHERE id = ? AND status = ? AND claimed_by = ?""",
                (STATUS_OPEN, build_id, STATUS_CLAIMED, user_id),
            )
        return cur.rowcount == 1


def complete_build(build_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE builds
                  SET status = ?, claimed_by = NULL, claimed_at = NULL, completed_at = ?
                WHERE id = ? AND status != ?""",
            (STATUS_COMPLETE, now_iso(), build_id, STATUS_COMPLETE),
        )
        return cur.rowcount == 1


def reopen_build(build_id: int) -> bool:
    """Undo a completion — for when something was marked done by mistake."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE builds SET status = ?, completed_at = NULL
                WHERE id = ? AND status = ?""",
            (STATUS_OPEN, build_id, STATUS_COMPLETE),
        )
        return cur.rowcount == 1


def delete_build(build_id: int) -> bool:
    """Remove a build and its whole update history.

    The updates table declares ON DELETE CASCADE and connect() turns foreign keys
    on, so the history goes with it rather than being orphaned.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))
        return cur.rowcount == 1


def list_builds(guild_id: int, status: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM builds WHERE guild_id = ? AND status = ? ORDER BY id",
                (guild_id, status),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM builds WHERE guild_id = ? ORDER BY id", (guild_id,)
        ).fetchall()


def recently_finished(guild_id: int, days: int) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM builds
                WHERE guild_id = ? AND status = ? AND completed_at >= ?
                ORDER BY completed_at DESC""",
            (guild_id, STATUS_COMPLETE, cutoff),
        ).fetchall()


def busy_builder_ids(guild_id: int) -> set[int]:
    """Builders who currently hold at least one claim."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT claimed_by FROM builds
                WHERE guild_id = ? AND status = ? AND claimed_by IS NOT NULL""",
            (guild_id, STATUS_CLAIMED),
        ).fetchall()
    return {int(row["claimed_by"]) for row in rows}


# --------------------------------------------------------------------------
# updates
# --------------------------------------------------------------------------

def add_update(
    build_id: int,
    builder_id: int,
    kind: str,
    note: str | None = None,
    file_name: str | None = None,
    file_path: str | None = None,
) -> int:
    if kind not in (KIND_PROGRESS, KIND_HANDOFF, KIND_COMPLETE):
        raise ValueError(f"unknown update kind: {kind}")
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO updates (build_id, builder_id, kind, note, file_name, file_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (build_id, builder_id, kind, note, file_name, file_path, now_iso()),
        )
        return int(cur.lastrowid)


def list_updates(build_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM updates WHERE build_id = ? ORDER BY id", (build_id,)
        ).fetchall()


def latest_schematic(build_id: int) -> sqlite3.Row | None:
    """The newest update that actually carried a file — what the next builder continues from."""
    with connect() as conn:
        return conn.execute(
            """SELECT * FROM updates
                WHERE build_id = ? AND file_path IS NOT NULL
                ORDER BY id DESC LIMIT 1""",
            (build_id,),
        ).fetchone()


def schematic_count(build_id: int) -> int:
    """How many files this build has received — the user-facing v1/v2/v3 number."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM updates WHERE build_id = ? AND file_path IS NOT NULL",
            (build_id,),
        ).fetchone()
    return int(row["n"])


def contributors(build_id: int) -> list[int]:
    """Everyone who has posted an update, oldest first — the handoff chain."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT builder_id FROM updates WHERE build_id = ? ORDER BY id",
            (build_id,),
        ).fetchall()
    return [int(row["builder_id"]) for row in rows]

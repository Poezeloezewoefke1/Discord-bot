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
    guild_id               INTEGER PRIMARY KEY,
    builder_role_id        INTEGER,
    scripter_role_id       INTEGER,
    requests_channel_id    INTEGER,
    board_channel_id       INTEGER,
    board_message_id       INTEGER,
    welcome_channel_id     INTEGER,
    applications_channel_id INTEGER,
    auto_role_id           INTEGER,
    goodbye_channel_id     INTEGER,
    security_log_channel_id INTEGER,
    honeypot_channel_id    INTEGER,
    security_watch_only    INTEGER,
    security_action        TEXT,
    min_account_age_days   INTEGER,
    raid_join_count        INTEGER,
    raid_window_seconds    INTEGER,
    scam_scanning          INTEGER
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

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER,
    opener_id    INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    subject      TEXT,
    about        TEXT,
    status       TEXT    NOT NULL DEFAULT 'open',
    claimed_by   INTEGER,
    created_at   TEXT    NOT NULL,
    closed_at    TEXT,
    closed_by    INTEGER,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS youtube_feeds (
    handle        TEXT PRIMARY KEY,
    channel_id    TEXT,
    title         TEXT,
    last_video_id TEXT,
    last_checked  TEXT,
    last_error    TEXT,
    initialised   INTEGER NOT NULL DEFAULT 0,
    feed_kind     TEXT,
    feed_author   TEXT,
    id_source     TEXT
);

CREATE TABLE IF NOT EXISTS announced_videos (
    video_id     TEXT PRIMARY KEY,
    handle       TEXT NOT NULL,
    announced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announced_handle ON announced_videos (handle, announced_at);

CREATE INDEX IF NOT EXISTS idx_tickets_guild_status ON tickets (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);

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


# Columns added to guild_config after the first release, as (name, type) pairs.
# CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so a
# database created before these were added needs them bolted on, or every query
# fails with "no such column".
LATER_CONFIG_COLUMNS = (
    # welcome / goodbye
    ("welcome_channel_id", "INTEGER"),
    ("applications_channel_id", "INTEGER"),
    ("auto_role_id", "INTEGER"),
    ("goodbye_channel_id", "INTEGER"),
    # anti-raid / anti-spam
    ("security_log_channel_id", "INTEGER"),
    ("honeypot_channel_id", "INTEGER"),
    ("security_watch_only", "INTEGER"),
    ("security_action", "TEXT"),
    ("min_account_age_days", "INTEGER"),
    ("raid_join_count", "INTEGER"),
    ("raid_window_seconds", "INTEGER"),
    ("scam_scanning", "INTEGER"),
    # tickets
    ("ticket_category_id", "INTEGER"),
    ("ticket_support_role_id", "INTEGER"),
    ("ticket_log_channel_id", "INTEGER"),
    ("ticket_panel_channel_id", "INTEGER"),
    ("ticket_panel_message_id", "INTEGER"),
    # upload notifications
    ("notify_channel_id", "INTEGER"),
)

LATER_CONFIG_NAMES = tuple(name for name, _ in LATER_CONFIG_COLUMNS)

# Columns added to youtube_feeds after that table first shipped. Same problem as
# guild_config: the table already exists in the live database, so CREATE TABLE
# IF NOT EXISTS won't touch it.
LATER_FEED_COLUMNS = (
    ("feed_kind", "TEXT"),
    ("feed_author", "TEXT"),
    ("id_source", "TEXT"),
)

LATER_COLUMNS = {
    "guild_config": LATER_CONFIG_COLUMNS,
    "youtube_feeds": LATER_FEED_COLUMNS,
}


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Bring older tables up to date. Safe to run on every start."""
    added = []
    for table, columns in LATER_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table not created yet; the schema above will handle it
        for column, decl in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added.append(f"{table}.{column}")
    return added


def init_db() -> None:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        added = _add_missing_columns(conn)
    if added:
        print(f"[db] migrated guild_config, added: {', '.join(added)}", flush=True)


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
        *LATER_CONFIG_NAMES,
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
# upload notifications
# --------------------------------------------------------------------------

# Enough history to never re-announce, without letting a table that gets
# committed to a git branch every few minutes grow without bound.
ANNOUNCED_KEEP_PER_HANDLE = 100


def get_feed(handle: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM youtube_feeds WHERE handle = ?", (handle,)
        ).fetchone()


def list_feeds() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM youtube_feeds ORDER BY handle").fetchall()


def save_feed(handle: str, **fields) -> None:
    allowed = {
        "channel_id", "title", "last_video_id", "last_checked",
        "last_error", "initialised", "feed_kind", "feed_author", "id_source",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown feed fields: {sorted(unknown)}")

    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO youtube_feeds (handle) VALUES (?)", (handle,))
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            conn.execute(
                f"UPDATE youtube_feeds SET {assignments} WHERE handle = ?",
                (*fields.values(), handle),
            )


def was_announced(video_id: str) -> bool:
    with connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM announced_videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            is not None
        )


def mark_announced(video_id: str, handle: str) -> bool:
    """Record a video as seen. False if it was already recorded.

    The insert is the claim: two polls overlapping can't both announce, and a
    restart mid-shift resumes rather than repeating.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO announced_videos (video_id, handle, announced_at) "
            "VALUES (?, ?, ?)",
            (video_id, handle, now_iso()),
        )
        return cur.rowcount == 1


def prune_announced(handle: str, keep: int = ANNOUNCED_KEEP_PER_HANDLE) -> int:
    with connect() as conn:
        cur = conn.execute(
            """DELETE FROM announced_videos
                WHERE handle = ? AND video_id NOT IN (
                    SELECT video_id FROM announced_videos
                     WHERE handle = ? ORDER BY announced_at DESC, rowid DESC LIMIT ?
                )""",
            (handle, handle, keep),
        )
        return cur.rowcount


def count_announced(handle: str | None = None) -> int:
    with connect() as conn:
        if handle:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM announced_videos WHERE handle = ?", (handle,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM announced_videos").fetchone()
    return int(row["n"])


def guilds_with_notify_channel() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM guild_config WHERE notify_channel_id IS NOT NULL"
        ).fetchall()


# --------------------------------------------------------------------------
# tickets
# --------------------------------------------------------------------------

TICKET_OPEN = "open"
TICKET_CLAIMED = "claimed"
TICKET_CLOSED = "closed"

TICKET_LIVE = (TICKET_OPEN, TICKET_CLAIMED)


def create_ticket(
    guild_id: int, opener_id: int, kind: str, subject: str | None, about: str | None
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO tickets (guild_id, opener_id, kind, subject, about, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, opener_id, kind, subject, about, TICKET_OPEN, now_iso()),
        )
        return int(cur.lastrowid)


def get_ticket(ticket_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()


def get_ticket_by_channel(channel_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        ).fetchone()


def attach_ticket_channel(ticket_id: int, channel_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE tickets SET channel_id = ? WHERE id = ?", (channel_id, ticket_id)
        )


def open_ticket_for(guild_id: int, opener_id: int) -> sqlite3.Row | None:
    """The ticket this person already has open, if any.

    One open ticket per person: without this, somebody bored can create fifty
    channels in a minute.
    """
    placeholders = ", ".join("?" for _ in TICKET_LIVE)
    with connect() as conn:
        return conn.execute(
            f"""SELECT * FROM tickets
                 WHERE guild_id = ? AND opener_id = ? AND status IN ({placeholders})
                 ORDER BY id DESC LIMIT 1""",
            (guild_id, opener_id, *TICKET_LIVE),
        ).fetchone()


def claim_ticket(ticket_id: int, user_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE tickets SET status = ?, claimed_by = ?
                WHERE id = ? AND status = ?""",
            (TICKET_CLAIMED, user_id, ticket_id, TICKET_OPEN),
        )
        return cur.rowcount == 1


def close_ticket(ticket_id: int, closed_by: int, reason: str | None) -> bool:
    placeholders = ", ".join("?" for _ in TICKET_LIVE)
    with connect() as conn:
        cur = conn.execute(
            f"""UPDATE tickets
                   SET status = ?, closed_at = ?, closed_by = ?, close_reason = ?
                 WHERE id = ? AND status IN ({placeholders})""",
            (TICKET_CLOSED, now_iso(), closed_by, reason, ticket_id, *TICKET_LIVE),
        )
        return cur.rowcount == 1


def reopen_ticket(ticket_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE tickets
                   SET status = ?, closed_at = NULL, closed_by = NULL, close_reason = NULL
                 WHERE id = ? AND status = ?""",
            (TICKET_OPEN, ticket_id, TICKET_CLOSED),
        )
        return cur.rowcount == 1


def delete_ticket(ticket_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        return cur.rowcount == 1


def list_tickets(guild_id: int, status: str | None = None) -> list[sqlite3.Row]:
    with connect() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = ? ORDER BY id",
                (guild_id, status),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM tickets WHERE guild_id = ? ORDER BY id", (guild_id,)
        ).fetchall()


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

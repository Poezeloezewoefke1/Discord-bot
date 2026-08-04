"""Welcome/goodbye rendering, and the guild_config migration.

The migration test is the important one here: the live database predates these
columns, and getting it wrong takes down the whole bot, not just welcomes.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402
import embeds  # noqa: E402

GUILD = 5150

# The guild_config as it shipped, before welcome support existed.
ORIGINAL_SCHEMA = """
CREATE TABLE guild_config (
    guild_id            INTEGER PRIMARY KEY,
    builder_role_id     INTEGER,
    scripter_role_id    INTEGER,
    requests_channel_id INTEGER,
    board_channel_id    INTEGER,
    board_message_id    INTEGER
);
CREATE TABLE builds (
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
CREATE TABLE updates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id   INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    builder_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    note       TEXT,
    file_name  TEXT,
    file_path  TEXT,
    created_at TEXT    NOT NULL
);
"""


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "welcome.db")
    yield
    db.set_db_path(config.DB_PATH)


def build_old_database(path: Path) -> None:
    """A database in the shape the live bot already has, with real data in it."""
    conn = sqlite3.connect(path)
    conn.executescript(ORIGINAL_SCHEMA)
    conn.execute("INSERT INTO guild_config VALUES (?,?,?,?,?,?)", (GUILD, 11, 22, 33, 44, 55))
    conn.execute(
        """INSERT INTO builds (guild_id,title,description,requested_by,status,claimed_by,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (GUILD, "Medieval spawn", "30x30", 201, "claimed", 301, "2026-08-04T00:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO updates (build_id,builder_id,kind,file_name,file_path,created_at)
           VALUES (1,301,'progress','a.schem','/x/a.schem','2026-08-04T00:00:00+00:00')"""
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# the migration
# --------------------------------------------------------------------------

def test_existing_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "welcome.db"
    build_old_database(path)

    db.init_db()

    cfg = db.get_config(GUILD)
    for column in db.LATER_CONFIG_COLUMNS:
        assert column in cfg.keys(), f"{column} was not added to an existing database"
        assert cfg[column] is None


def test_migration_does_not_lose_existing_settings(tmp_path):
    build_old_database(tmp_path / "welcome.db")
    db.init_db()

    cfg = db.get_config(GUILD)
    assert cfg["builder_role_id"] == 11
    assert cfg["scripter_role_id"] == 22
    assert cfg["requests_channel_id"] == 33
    assert cfg["board_channel_id"] == 44
    assert cfg["board_message_id"] == 55, "/setup would have to be run again"


def test_migration_does_not_lose_builds(tmp_path):
    build_old_database(tmp_path / "welcome.db")
    db.init_db()

    build = db.get_build(1)
    assert build["title"] == "Medieval spawn"
    assert build["claimed_by"] == 301, "a claim must survive the migration"
    assert db.schematic_count(1) == 1
    assert db.latest_schematic(1)["file_name"] == "a.schem"


def test_migration_is_safe_to_run_on_every_restart(tmp_path):
    """This bot restarts every ~6 hours, so init_db runs constantly."""
    build_old_database(tmp_path / "welcome.db")
    db.init_db()
    db.save_config(GUILD, welcome_channel_id=777)

    db.init_db()
    db.init_db()

    cfg = db.get_config(GUILD)
    assert cfg["welcome_channel_id"] == 777
    assert cfg["builder_role_id"] == 11


def test_new_columns_are_writable_after_migration(tmp_path):
    build_old_database(tmp_path / "welcome.db")
    db.init_db()

    db.save_config(GUILD, welcome_channel_id=1, applications_channel_id=2,
                   auto_role_id=3, goodbye_channel_id=4)

    cfg = db.get_config(GUILD)
    assert (cfg["welcome_channel_id"], cfg["applications_channel_id"]) == (1, 2)
    assert (cfg["auto_role_id"], cfg["goodbye_channel_id"]) == (3, 4)
    assert cfg["builder_role_id"] == 11, "welcome setup must not disturb the build board"


def test_a_fresh_database_has_the_columns_too():
    db.init_db()
    db.save_config(GUILD, welcome_channel_id=99)
    assert db.get_config(GUILD)["welcome_channel_id"] == 99


def test_welcome_off_clears_only_welcome_settings(tmp_path):
    build_old_database(tmp_path / "welcome.db")
    db.init_db()
    db.save_config(GUILD, welcome_channel_id=1, auto_role_id=3)

    db.save_config(GUILD, welcome_channel_id=None, applications_channel_id=None,
                   auto_role_id=None, goodbye_channel_id=None)

    cfg = db.get_config(GUILD)
    assert cfg["welcome_channel_id"] is None
    assert cfg["auto_role_id"] is None
    assert cfg["builder_role_id"] == 11, "turning welcomes off must not break the board"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class FakeAsset:
    url = "https://example.invalid/icon.png"


class FakeGuild:
    def __init__(self, name="ASTRA Smp Events", member_count=57, icon=True) -> None:
        self.id = GUILD
        self.name = name
        self.member_count = member_count
        self.icon = FakeAsset() if icon else None


class FakeMember:
    def __init__(self, name="★ ⁝ aιdεn! ✟", guild=None) -> None:
        self.id = 4242
        self.display_name = name
        self.mention = f"<@{self.id}>"
        self.display_avatar = FakeAsset()
        self.guild = guild or FakeGuild()


def limits_ok(embed) -> None:
    assert len(embed.description or "") <= 4096
    assert len(embed.author.name or "") <= 256
    assert len(embed.footer.text or "") <= 2048
    assert len(embed) <= 6000


def test_welcome_matches_the_expected_layout():
    member = FakeMember()
    embed = embeds.welcome_card(member, applications_channel_id=987)
    limits_ok(embed)

    assert embed.author.name == "Welcome to ASTRA Smp Events"
    assert member.mention in embed.description
    assert "ASTRA Smp Events" in embed.description
    assert "<#987>" in embed.description
    assert embed.footer.text == "You're our 57th member"


def test_welcome_without_an_applications_channel_omits_that_line():
    embed = embeds.welcome_card(FakeMember(), applications_channel_id=None)
    limits_ok(embed)
    assert "application" not in embed.description.lower()
    assert "Welcome" in embed.description


def test_welcome_works_on_a_server_with_no_icon():
    member = FakeMember(guild=FakeGuild(icon=False))
    embed = embeds.welcome_card(member, 987)
    limits_ok(embed)
    assert embed.author.name == "Welcome to ASTRA Smp Events"


def test_welcome_survives_a_very_long_server_name():
    member = FakeMember(guild=FakeGuild(name="X" * 300))
    limits_ok(embeds.welcome_card(member, 987))


def test_welcome_handles_unicode_display_names():
    """The name in the original screenshot is full of decorative characters."""
    member = FakeMember(name="★ ⁝ aιdεn! ✟")
    embed = embeds.welcome_card(member, 987)
    limits_ok(embed)
    assert member.mention in embed.description


def test_goodbye_renders():
    member = FakeMember()
    embed = embeds.goodbye_card(member, member.guild)
    limits_ok(embed)
    assert "aιdεn" in embed.description
    assert "left" in embed.description


def test_member_count_is_skipped_when_discord_does_not_report_it():
    member = FakeMember(guild=FakeGuild(member_count=None))
    embed = embeds.welcome_card(member, 987)
    limits_ok(embed)
    assert not embed.footer.text


@pytest.mark.parametrize(
    "number,expected",
    [
        (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
        (11, "11th"), (12, "12th"), (13, "13th"),
        (21, "21st"), (22, "22nd"), (23, "23rd"),
        (101, "101st"), (111, "111th"), (1002, "1,002nd"),
    ],
)
def test_ordinals(number, expected):
    assert embeds.ordinal(number) == expected


# --------------------------------------------------------------------------
# /test helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "under a minute"),
        (59, "under a minute"),
        (60, "1m"),
        (90, "1m"),
        (3600, "1h"),
        (3660, "1h 1m"),
        (19800, "5h 30m"),
        (-50, "under a minute"),
    ],
)
def test_human_duration(seconds, expected):
    from cogs.health import human_duration

    assert human_duration(seconds) == expected

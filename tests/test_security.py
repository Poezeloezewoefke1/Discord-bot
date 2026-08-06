"""The rules that can ban someone.

Two of these matter more than the rest: staff must never be actionable, and watch
mode must never act. Everything else is a tuning question; those two are the
difference between a protection and an accident.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import security  # noqa: E402

BOT_ID = 1
OWNER_ID = 2
MEMBER_ID = 3


class FakePerms:
    def __init__(self, **granted) -> None:
        for name in ("administrator", "manage_guild", "ban_members", "manage_messages"):
            setattr(self, name, granted.get(name, False))


class FakeGuild:
    owner_id = OWNER_ID


class FakeMember:
    def __init__(self, user_id=MEMBER_ID, created_at=None, **perms) -> None:
        self.id = user_id
        self.guild = FakeGuild()
        self.guild_permissions = FakePerms(**perms)
        self.created_at = created_at


def ago(**delta):
    return datetime.now(timezone.utc) - timedelta(**delta)


# --------------------------------------------------------------------------
# nobody on staff can be actioned — the test that stops an own goal
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "permission",
    ["administrator", "manage_guild", "ban_members", "manage_messages"],
)
def test_staff_are_exempt(permission):
    member = FakeMember(**{permission: True})
    assert security.is_exempt(member, BOT_ID) is True


def test_the_server_owner_is_exempt():
    assert security.is_exempt(FakeMember(user_id=OWNER_ID), BOT_ID) is True


def test_the_bot_itself_is_exempt():
    """Otherwise the honeypot warning the bot posts would trip the honeypot."""
    assert security.is_exempt(FakeMember(user_id=BOT_ID), BOT_ID) is True


def test_an_ordinary_member_is_not_exempt():
    assert security.is_exempt(FakeMember(), BOT_ID) is False


def test_exemption_survives_a_member_with_no_permissions_attribute():
    class Bare:
        id = 99
    assert security.is_exempt(Bare(), BOT_ID) is False


# --------------------------------------------------------------------------
# watch mode must not act
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reason",
    [
        security.REASON_HONEYPOT,
        security.REASON_NEW_ACCOUNT,
        security.REASON_RAID,
        security.REASON_SCAM,
    ],
)
def test_watch_mode_takes_no_action(reason):
    decision = security.decide(reason, "detail", watch_only=True, action=security.ACTION_BAN)
    assert decision.action == security.ACTION_NONE
    assert decision.intended == security.ACTION_BAN, "it should still say what it would have done"
    assert decision.watch_only is True


@pytest.mark.parametrize(
    "action",
    [security.ACTION_BAN, security.ACTION_KICK, security.ACTION_TIMEOUT, security.ACTION_ALERT],
)
def test_live_mode_acts(action):
    decision = security.decide(security.REASON_HONEYPOT, "d", watch_only=False, action=action)
    assert decision.action == action


def test_action_defaults_to_ban_when_unset():
    assert security.decide("x", "d", watch_only=False, action=None).action == security.ACTION_BAN


# --------------------------------------------------------------------------
# raid window
# --------------------------------------------------------------------------

def test_joins_below_the_threshold_do_not_trip():
    tracker = security.RaidTracker()
    counts = [tracker.record(1, 60, when=1000 + i) for i in range(9)]
    assert max(counts) == 9


def test_the_threshold_join_trips_it():
    tracker = security.RaidTracker()
    for i in range(9):
        tracker.record(1, 60, when=1000 + i)
    assert tracker.record(1, 60, when=1009) == 10


def test_joins_outside_the_window_stop_counting():
    """Slow, legitimate growth must never look like a raid."""
    tracker = security.RaidTracker()
    for i in range(20):
        count = tracker.record(1, 60, when=1000 + i * 120)  # one join every 2 minutes
        assert count == 1, f"a join every 2 minutes should never accumulate, got {count}"


def test_guilds_are_counted_separately():
    tracker = security.RaidTracker()
    for i in range(9):
        tracker.record(1, 60, when=1000 + i)
    assert tracker.record(2, 60, when=1009) == 1


def test_reset_clears_the_window():
    tracker = security.RaidTracker()
    for i in range(5):
        tracker.record(1, 60, when=1000 + i)
    tracker.reset(1)
    assert tracker.record(1, 60, when=1005) == 1


# --------------------------------------------------------------------------
# account age
# --------------------------------------------------------------------------

def test_a_brand_new_account_is_flagged():
    assert security.check_account_age(ago(hours=2), 7) is not None


def test_an_old_account_is_not_flagged():
    assert security.check_account_age(ago(days=400), 7) is None


@pytest.mark.parametrize("days,flagged", [(6, True), (7, False), (8, False)])
def test_account_age_boundary(days, flagged):
    result = security.check_account_age(ago(days=days, hours=1), 7)
    assert (result is not None) is flagged


def test_account_age_check_disabled_by_zero():
    assert security.check_account_age(ago(hours=1), 0) is None


def test_unknown_creation_date_is_not_flagged():
    """Better to miss one than to ban someone over missing data."""
    assert security.check_account_age(None, 7) is None


def test_hours_are_reported_for_very_new_accounts():
    assert "hours" in security.check_account_age(ago(hours=3), 7)


# --------------------------------------------------------------------------
# scam links — false positives are the dangerous direction
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "free nitro here https://dlscord-nitro.ru/claim",
        "check this out discordgift.site/free",
        "https://discrod.gg/hjKl",
        "steamcommunlty.com/trade/offer",
        "grab it at disc0rd-nitro.xyz",
        "http://discord-gift.click/nitro",
        "https://dsicord.com/gift",
        "claim at nitro.gg/free",
    ],
)
def test_phishing_links_are_caught(message):
    assert security.find_scam(message) is not None, f"missed: {message}"


@pytest.mark.parametrize(
    "message",
    [
        "join us at discord.gg/astrasmp",
        "I sent you a gift, https://discord.gift/abc123",
        "free nitro would be nice lol",
        "the spawn is 30x30, e.g. like the old one",
        "read https://discordjs.guide/ for the docs",
        "server ip is play.astrasmp.net",
        "https://www.minecraft.net/en-us/download",
        "https://steamcommunity.com/id/someone",
        "cdn.discord.com/attachments/1/2/spawn.schem",
        "we need 1.5 blocks of space",
        "check minecraft.fandom.com/wiki/Redstone",
        "watch the intro.com video",
        "host is nitrado.net",
        "listed on disboard.org",
        "buy at nitrous.io",
        "the server is on aternos.org",
        "https://planetminecraft.com/project/spawn",
        "",
    ],
)
def test_ordinary_messages_are_not_flagged(message):
    """A false positive bans a real member — this is the direction that hurts."""
    assert security.find_scam(message) is None, f"false positive on: {message!r}"


def test_subdomains_of_allowed_domains_are_fine():
    assert security.find_scam("images-ext-1.discordapp.net/external/x.png") is None


def test_transposed_letters_are_caught():
    """Character substitution alone misses discrod — the letters are swapped."""
    assert security.find_scam("https://discrod.gg/x") is not None


@pytest.mark.parametrize(
    "a,b,expected",
    [("discord", "discord", 0), ("discrod", "discord", 1), ("dsicord", "discord", 1),
     ("discor", "discord", 1), ("disccord", "discord", 1), ("banana", "discord", 7)],
)
def test_edit_distance(a, b, expected):
    assert security.osa_distance(a, b) == expected


# --------------------------------------------------------------------------
# watch-mode default in the cog
# --------------------------------------------------------------------------

def test_unset_watch_mode_means_watching():
    """An unconfigured guild must never be enforcing."""
    from cogs.security import watch_only

    assert watch_only(None) is True
    assert watch_only({"security_watch_only": None}) is True
    assert watch_only({"security_watch_only": 1}) is True
    assert watch_only({"security_watch_only": 0}) is False


# --------------------------------------------------------------------------
# the honeypot must be observable
#
# It was reported as "not working" when in fact it was working exactly as built:
# watch mode plus staff exemption produced no output at all, which looks the same
# as a bot that isn't listening.
# --------------------------------------------------------------------------

class FakeCog:
    """Just the notice rate-limiter, without needing a Bot."""

    from cogs.security import SecurityCog

    ARMED_NOTICE_INTERVAL = SecurityCog.ARMED_NOTICE_INTERVAL
    should_send_armed_notice = SecurityCog.should_send_armed_notice

    def __init__(self):
        self._armed_notice_at = {}


def test_first_staff_trip_is_announced():
    cog = FakeCog()
    assert cog.should_send_armed_notice(1, now=0.0) is True


def test_repeat_staff_trips_are_suppressed():
    """Staff chatting in the honeypot must not flood the log."""
    cog = FakeCog()
    cog.should_send_armed_notice(1, now=0.0)
    assert cog.should_send_armed_notice(1, now=30.0) is False
    assert cog.should_send_armed_notice(1, now=599.0) is False


def test_the_notice_returns_after_the_interval():
    cog = FakeCog()
    cog.should_send_armed_notice(1, now=0.0)
    assert cog.should_send_armed_notice(1, now=601.0) is True


def test_the_rate_limit_is_per_guild():
    cog = FakeCog()
    cog.should_send_armed_notice(1, now=0.0)
    assert cog.should_send_armed_notice(2, now=0.0) is True


# --------------------------------------------------------------------------
# the honeypot's action is its own
#
# It fires on a single message with no second signal, so being wrong means a
# curious member removed. A kick can be undone by inviting them back; a ban
# can't, practically speaking, on a server this size.
# --------------------------------------------------------------------------

def test_the_honeypot_kicks_by_default_even_when_everything_else_bans():
    import security
    from cogs.security import honeypot_action

    cfg = {"security_action": security.ACTION_BAN, "honeypot_action": None}
    assert honeypot_action(cfg) == security.ACTION_KICK
    assert security.HONEYPOT_DEFAULT_ACTION == security.ACTION_KICK


def test_an_unconfigured_guild_still_only_kicks():
    from cogs.security import honeypot_action

    import security

    assert honeypot_action(None) == security.ACTION_KICK


def test_staff_can_still_choose_a_ban_deliberately():
    import security
    from cogs.security import honeypot_action

    assert honeypot_action({"honeypot_action": security.ACTION_BAN}) == security.ACTION_BAN


def test_the_pinned_warning_names_what_actually_happens():
    """A sign saying "banned" over a trap that kicks teaches people to ignore
    the sign."""
    import security
    from cogs.security import honeypot_warning

    kicked = honeypot_warning(security.ACTION_KICK)
    assert "kicked" in kicked and "banned" not in kicked

    banned = honeypot_warning(security.ACTION_BAN)
    assert "banned" in banned

    timed_out = honeypot_warning(security.ACTION_TIMEOUT)
    assert "24 hours" in timed_out

    alert = honeypot_warning(security.ACTION_ALERT)
    assert "staff" in alert and "banned" not in alert


def test_going_live_needs_permission_for_the_honeypot_action_too():
    """Kick Members is a different permission from Ban Members — a honeypot that
    kicks while the bot can only ban would fail silently at the moment it fires."""
    from cogs.security import missing_action_permission

    import security

    class Perms:
        ban_members = True
        kick_members = False
        moderate_members = True

    class Guild:
        class me:
            guild_permissions = Perms()

    assert missing_action_permission(Guild(), security.ACTION_BAN) is None
    blocker = missing_action_permission(Guild(), security.ACTION_KICK)
    assert blocker and "Kick Members" in blocker


# --------------------------------------------------------------------------
# re-running setup must not quietly undo settings
# --------------------------------------------------------------------------

def keep_value(existing, column, given, fallback):
    """Mirrors the keep() helper inside /security-setup."""
    if given is not None:
        return given
    if existing is not None and existing.get(column) is not None:
        return existing[column]
    return fallback


def test_omitted_settings_are_preserved():
    """The live server has min_account_age_days=30; re-running to fix the raid
    window must not silently reset it to the default of 7."""
    existing = {"min_account_age_days": 30, "raid_join_count": 30, "raid_window_seconds": 5}

    assert keep_value(existing, "min_account_age_days", None, 7) == 30
    assert keep_value(existing, "raid_join_count", 10, 10) == 10
    assert keep_value(existing, "raid_window_seconds", 60, 60) == 60


def test_defaults_apply_only_when_nothing_is_stored():
    assert keep_value(None, "min_account_age_days", None, 7) == 7
    assert keep_value({"min_account_age_days": None}, "min_account_age_days", None, 7) == 7


def test_an_explicit_value_always_wins():
    assert keep_value({"min_account_age_days": 30}, "min_account_age_days", 1, 7) == 1


# --------------------------------------------------------------------------
# raid settings that can never fire
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "joins,seconds,warns",
    [
        (30, 5, True),     # what the live server had: 6 joins a second, sustained
        (100, 10, True),
        (10, 60, False),   # the usual shape
        (30, 300, False),
        (5, 5, False),
        (None, 60, False),
        (10, None, False),
    ],
)
def test_impossible_raid_settings_are_flagged(joins, seconds, warns):
    from cogs.security import raid_rate_warning

    assert bool(raid_rate_warning(joins, seconds)) is warns


# --------------------------------------------------------------------------
# live mode must be able to enforce
# --------------------------------------------------------------------------

class FakeMe:
    def __init__(self, **perms):
        self.guild_permissions = FakePerms(**perms)


class PermGuild:
    def __init__(self, **perms):
        self.me = FakeMe(**perms)


def test_live_mode_is_refused_without_the_permission():
    from cogs.security import missing_action_permission

    blocker = missing_action_permission(PermGuild(), security.ACTION_BAN)
    assert blocker is not None and "Ban Members" in blocker


def test_live_mode_is_allowed_with_the_permission():
    from cogs.security import missing_action_permission

    class Perms(FakePerms):
        pass

    guild = PermGuild()
    guild.me.guild_permissions.ban_members = True
    assert missing_action_permission(guild, security.ACTION_BAN) is None


def test_alert_only_never_needs_a_permission():
    from cogs.security import missing_action_permission

    assert missing_action_permission(PermGuild(), security.ACTION_ALERT) is None


@pytest.mark.parametrize(
    "action,attribute",
    [
        (security.ACTION_BAN, "ban_members"),
        (security.ACTION_KICK, "kick_members"),
        (security.ACTION_TIMEOUT, "moderate_members"),
    ],
)
def test_each_action_checks_its_own_permission(action, attribute):
    from cogs.security import missing_action_permission

    guild = PermGuild()
    assert missing_action_permission(guild, action) is not None
    setattr(guild.me.guild_permissions, attribute, True)
    assert missing_action_permission(guild, action) is None

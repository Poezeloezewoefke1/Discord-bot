"""The claim/handoff state machine, tested without touching Discord.

The point of this bot is that two builders can't end up on the same build, so
that guarantee gets tested hardest — including under real thread contention.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402

GUILD = 1111
SCRIPTER = 2001
BUILDER_A = 3001
BUILDER_B = 3002
BUILDER_C = 3003


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "test.db")
    db.init_db()
    yield
    db.set_db_path(config.DB_PATH)


def new_build(title="Medieval spawn") -> int:
    return db.create_build(GUILD, title, "30x30, stone and oak", SCRIPTER)


# --------------------------------------------------------------------------
# the happy path, start to finish
# --------------------------------------------------------------------------

def test_new_build_starts_open():
    build_id = new_build()
    build = db.get_build(build_id)
    assert build["status"] == db.STATUS_OPEN
    assert build["claimed_by"] is None
    assert build["requested_by"] == SCRIPTER
    assert db.schematic_count(build_id) == 0


def test_claim_assigns_the_builder():
    build_id = new_build()
    assert db.claim_build(build_id, BUILDER_A) is True

    build = db.get_build(build_id)
    assert build["status"] == db.STATUS_CLAIMED
    assert build["claimed_by"] == BUILDER_A
    assert build["claimed_at"] is not None


def test_full_lifecycle_open_claim_handoff_claim_complete():
    build_id = new_build()

    assert db.claim_build(build_id, BUILDER_A)
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, "terrain done", "t.schem", "/x/v1_t.schem")
    assert db.get_build(build_id)["status"] == db.STATUS_CLAIMED, "progress must not release"

    db.add_update(build_id, BUILDER_A, db.KIND_HANDOFF, "walls up", "w.schem", "/x/v2_w.schem")
    db.release_build(build_id, None)
    assert db.get_build(build_id)["status"] == db.STATUS_OPEN

    assert db.claim_build(build_id, BUILDER_B)
    db.add_update(build_id, BUILDER_B, db.KIND_COMPLETE, "interior", "i.schem", "/x/v3_i.schem")
    db.complete_build(build_id)

    build = db.get_build(build_id)
    assert build["status"] == db.STATUS_COMPLETE
    assert build["completed_at"] is not None
    assert build["claimed_by"] is None
    assert db.contributors(build_id) == [BUILDER_A, BUILDER_B]


# --------------------------------------------------------------------------
# no two builders on the same build
# --------------------------------------------------------------------------

def test_second_builder_cannot_claim_a_taken_build():
    build_id = new_build()
    assert db.claim_build(build_id, BUILDER_A) is True
    assert db.claim_build(build_id, BUILDER_B) is False, "double-claim must be refused"

    build = db.get_build(build_id)
    assert build["claimed_by"] == BUILDER_A, "the original claimer must keep it"


def test_reclaiming_your_own_build_is_still_refused():
    build_id = new_build()
    assert db.claim_build(build_id, BUILDER_A) is True
    assert db.claim_build(build_id, BUILDER_A) is False


def test_finished_builds_cannot_be_claimed():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    db.complete_build(build_id)
    assert db.claim_build(build_id, BUILDER_B) is False


def test_exactly_one_winner_under_real_contention():
    """Twenty threads race for the same build. Exactly one may win."""
    build_id = new_build()

    def attempt(user_id: int) -> bool:
        return db.claim_build(build_id, user_id)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(9000, 9020)))

    assert sum(results) == 1, f"expected exactly one successful claim, got {sum(results)}"
    assert db.get_build(build_id)["claimed_by"] is not None


# --------------------------------------------------------------------------
# handing off to the next builder
# --------------------------------------------------------------------------

def test_handoff_reopens_and_keeps_the_latest_schematic():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    db.add_update(build_id, BUILDER_A, db.KIND_HANDOFF, "half done", "spawn.schem", "/x/v1.schem")
    db.release_build(build_id, None)

    build = db.get_build(build_id)
    assert build["status"] == db.STATUS_OPEN
    assert build["claimed_by"] is None
    assert build["claimed_at"] is None

    latest = db.latest_schematic(build_id)
    assert latest["file_path"] == "/x/v1.schem"
    assert latest["builder_id"] == BUILDER_A


def test_next_builder_can_pick_up_a_handed_off_build():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    db.add_update(build_id, BUILDER_A, db.KIND_HANDOFF, None, "a.schem", "/x/v1.schem")
    db.release_build(build_id, None)

    assert db.claim_build(build_id, BUILDER_B) is True
    assert db.get_build(build_id)["claimed_by"] == BUILDER_B


def test_latest_schematic_tracks_the_newest_file_only():
    build_id = new_build()
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, None, "a.schem", "/x/v1.schem")
    db.add_update(build_id, BUILDER_B, db.KIND_PROGRESS, None, "b.schem", "/x/v2.schem")
    # A note with no file must not become "the latest schematic".
    db.add_update(build_id, BUILDER_C, db.KIND_PROGRESS, "just a comment")

    assert db.latest_schematic(build_id)["file_path"] == "/x/v2.schem"
    assert db.schematic_count(build_id) == 2


# --------------------------------------------------------------------------
# releasing
# --------------------------------------------------------------------------

def test_builder_can_release_their_own_claim():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    assert db.release_build(build_id, BUILDER_A) is True
    assert db.get_build(build_id)["status"] == db.STATUS_OPEN


def test_builder_cannot_release_someone_elses_claim():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    assert db.release_build(build_id, BUILDER_B) is False
    assert db.get_build(build_id)["claimed_by"] == BUILDER_A


def test_force_release_works_for_admins():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    assert db.release_build(build_id, None) is True
    assert db.get_build(build_id)["status"] == db.STATUS_OPEN


# --------------------------------------------------------------------------
# file validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename", ["spawn.schem", "spawn.schematic", "SPAWN.LITEMATIC", "build.nbt", "pack.zip"]
)
def test_schematic_formats_are_accepted(filename):
    assert config.is_allowed_file(filename) is True


@pytest.mark.parametrize("filename", ["spawn.png", "notes.txt", "script.py", "spawn", "a.exe"])
def test_non_schematic_files_are_rejected(filename):
    assert config.is_allowed_file(filename) is False


def test_unknown_update_kind_is_rejected():
    build_id = new_build()
    with pytest.raises(ValueError):
        db.add_update(build_id, BUILDER_A, "sabotage")
    assert db.list_updates(build_id) == []


# --------------------------------------------------------------------------
# deleting
# --------------------------------------------------------------------------

def test_delete_removes_the_build():
    build_id = new_build()
    assert db.delete_build(build_id) is True
    assert db.get_build(build_id) is None


def test_delete_takes_the_update_history_with_it():
    """Orphaned update rows would keep counting toward other builds' versions."""
    build_id = new_build()
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, "a", "a.schem", "/x/a.schem")
    db.add_update(build_id, BUILDER_B, db.KIND_HANDOFF, "b", "b.schem", "/x/b.schem")
    assert len(db.list_updates(build_id)) == 2

    db.delete_build(build_id)
    assert db.list_updates(build_id) == []


def test_delete_leaves_other_builds_alone():
    keep = new_build("Keep me")
    db.add_update(keep, BUILDER_A, db.KIND_PROGRESS, None, "k.schem", "/x/k.schem")
    remove = new_build("Delete me")
    db.add_update(remove, BUILDER_B, db.KIND_PROGRESS, None, "r.schem", "/x/r.schem")

    db.delete_build(remove)

    assert db.get_build(keep) is not None
    assert db.schematic_count(keep) == 1
    assert db.latest_schematic(keep)["file_path"] == "/x/k.schem"


def test_deleting_a_claimed_build_frees_the_builder():
    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    assert db.busy_builder_ids(GUILD) == {BUILDER_A}

    db.delete_build(build_id)
    assert db.busy_builder_ids(GUILD) == set()


def test_deleting_something_that_is_already_gone_is_false():
    build_id = new_build()
    db.delete_build(build_id)
    assert db.delete_build(build_id) is False


def test_deleted_builds_leave_the_board():
    import embeds

    build_id = new_build("Doomed")
    db.claim_build(build_id, BUILDER_A)
    assert "Doomed" in str(embeds.board(FakeGuild(), FakeRole([])).fields)

    db.delete_build(build_id)
    assert "Doomed" not in str(embeds.board(FakeGuild(), FakeRole([])).fields)


class FakeMemberWithPerms:
    """Enough of discord.Member for the permission helpers."""

    def __init__(self, user_id: int, admin: bool = False) -> None:
        self.id = user_id
        self.guild_permissions = type("Perms", (), {"manage_guild": admin})()
        self.roles = []


def test_only_the_requester_or_an_admin_can_delete():
    import service

    build_id = new_build()
    build = db.get_build(build_id)

    # the script writer who asked for it
    service.check_can_delete(FakeMemberWithPerms(SCRIPTER), build)
    # an admin
    service.check_can_delete(FakeMemberWithPerms(BUILDER_B, admin=True), build)

    # anyone else, including a builder working on it
    with pytest.raises(config.ConfigError):
        service.check_can_delete(FakeMemberWithPerms(BUILDER_A), build)


def test_the_builder_holding_a_build_still_cannot_delete_it():
    """They should /release instead — the request stays alive for someone else."""
    import service

    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    with pytest.raises(config.ConfigError):
        service.check_can_delete(FakeMemberWithPerms(BUILDER_A), db.get_build(build_id))


def test_delete_confirmation_buttons_are_within_discord_limits():
    import discord
    import views

    view = views.ConfirmDeleteView(1, invoker_id=SCRIPTER)
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 2, "expected a confirm and a cancel button"
    for button in buttons:
        assert len(button.label) <= BUTTON_LABEL_LIMIT

    assert view.timeout, "an irreversible confirmation must expire, not linger"


def test_ids_are_not_reused_after_a_delete():
    """Reusing an id would point old buttons and links at the wrong build."""
    first = new_build()
    db.delete_build(first)
    second = new_build()
    assert second != first


# --------------------------------------------------------------------------
# what the board reads
# --------------------------------------------------------------------------

def test_board_queries_split_builds_by_state():
    open_id = new_build("Shop district")
    claimed_id = new_build("Nether hub")
    done_id = new_build("Lobby")

    db.claim_build(claimed_id, BUILDER_A)
    db.claim_build(done_id, BUILDER_B)
    db.complete_build(done_id)

    assert [b["id"] for b in db.list_builds(GUILD, db.STATUS_OPEN)] == [open_id]
    assert [b["id"] for b in db.list_builds(GUILD, db.STATUS_CLAIMED)] == [claimed_id]
    assert [b["id"] for b in db.recently_finished(GUILD, 7)] == [done_id]


def test_busy_builders_are_only_those_holding_a_claim():
    a_id = new_build("A")
    b_id = new_build("B")
    db.claim_build(a_id, BUILDER_A)
    db.claim_build(b_id, BUILDER_B)
    db.release_build(b_id, BUILDER_B)

    assert db.busy_builder_ids(GUILD) == {BUILDER_A}


def test_builds_are_scoped_to_their_guild():
    mine = new_build("Mine")
    db.create_build(9999, "Someone else's", "spec", SCRIPTER)
    assert [b["id"] for b in db.list_builds(GUILD)] == [mine]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_config_upsert_preserves_untouched_fields():
    db.save_config(GUILD, builder_role_id=10, scripter_role_id=20)
    db.save_config(GUILD, board_message_id=30)

    cfg = db.get_config(GUILD)
    assert cfg["builder_role_id"] == 10
    assert cfg["scripter_role_id"] == 20
    assert cfg["board_message_id"] == 30


def test_config_rejects_unknown_fields():
    with pytest.raises(ValueError):
        db.save_config(GUILD, nonsense_id=1)


# --------------------------------------------------------------------------
# embed rendering — catches None handling and Discord's field length limits
# --------------------------------------------------------------------------

class FakeUser:
    """Just enough of discord.Member for the embed builders."""

    display_name = "Dex"

    class display_avatar:
        url = "https://example.invalid/a.png"


def _assert_within_discord_limits(embed):
    assert len(embed.title or "") <= 256
    assert len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert len(field.name) <= 256, f"field name too long: {field.name}"
        assert len(field.value) <= 1024, f"field value too long: {field.name}"
    assert len(embed) <= 6000


def test_build_card_renders_in_every_state():
    import embeds

    build_id = new_build()
    _assert_within_discord_limits(embeds.build_card(db.get_build(build_id)))

    db.claim_build(build_id, BUILDER_A)
    _assert_within_discord_limits(embeds.build_card(db.get_build(build_id)))

    db.add_update(build_id, BUILDER_A, db.KIND_HANDOFF, "half", "a.schem", "/x/v1.schem")
    db.release_build(build_id, None)
    card = embeds.build_card(db.get_build(build_id))
    _assert_within_discord_limits(card)
    assert "Continue from" in [f.name for f in card.fields]

    db.claim_build(build_id, BUILDER_B)
    db.add_update(build_id, BUILDER_B, db.KIND_COMPLETE, "done", "b.schem", "/x/v2.schem")
    db.complete_build(build_id)
    _assert_within_discord_limits(embeds.build_card(db.get_build(build_id)))


def test_build_card_survives_a_maximum_length_request():
    import embeds

    build_id = db.create_build(GUILD, "T" * 100, "D" * 3000, SCRIPTER)
    db.claim_build(build_id, BUILDER_A)
    for i in range(12):
        db.add_update(build_id, 4000 + i, db.KIND_PROGRESS, "x", f"{i}.schem", f"/x/{i}.schem")
    _assert_within_discord_limits(embeds.build_card(db.get_build(build_id)))


def test_update_post_renders_for_each_kind():
    import embeds

    build_id = new_build()
    db.claim_build(build_id, BUILDER_A)
    for kind in (db.KIND_PROGRESS, db.KIND_HANDOFF, db.KIND_COMPLETE):
        embed = embeds.update_post(
            db.get_build(build_id), FakeUser(), kind, "did the walls", "spawn.schem"
        )
        _assert_within_discord_limits(embed)

    # No note and no file is a valid update too.
    embed = embeds.update_post(db.get_build(build_id), FakeUser(), db.KIND_PROGRESS, None, None)
    _assert_within_discord_limits(embed)


class FakeGuild:
    id = GUILD


class FakeMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"


class FakeRole:
    def __init__(self, members) -> None:
        self.members = members


def test_board_renders_and_stays_within_limits_when_busy():
    import embeds

    # Far more builds than a field can hold, to exercise the overflow handling.
    for i in range(40):
        build_id = db.create_build(GUILD, f"Build number {i} with a longish name", "spec", SCRIPTER)
        if i % 2 == 0:
            db.claim_build(build_id, 5000 + i)
            db.add_update(build_id, 5000 + i, db.KIND_PROGRESS, None, "a.schem", "/x/a.schem")

    role = FakeRole([FakeMember(5000 + i) for i in range(40)])
    embed = embeds.board(FakeGuild(), role)
    _assert_within_discord_limits(embed)
    assert "Being built" in " ".join(f.name for f in embed.fields)


def test_board_renders_when_there_is_nothing_to_do():
    import embeds

    embed = embeds.board(FakeGuild(), FakeRole([]))
    _assert_within_discord_limits(embed)
    assert "Nothing queued" in " ".join(f.name for f in embed.fields)


def test_board_renders_without_a_builder_role_configured():
    import embeds

    new_build()
    _assert_within_discord_limits(embeds.board(FakeGuild(), None))


# --------------------------------------------------------------------------
# Discord's UI limits
#
# These are enforced by Discord's API, not by discord.py, so an over-long string
# sails through import and every unit test and only fails as a 400 the moment a
# real user runs the command. That is exactly how the /request modal broke in
# production, so each limit gets pinned here.
# --------------------------------------------------------------------------

MODAL_TITLE_LIMIT = 45
TEXT_INPUT_LABEL_LIMIT = 45
TEXT_INPUT_PLACEHOLDER_LIMIT = 100
BUTTON_LABEL_LIMIT = 80
COMMAND_DESCRIPTION_LIMIT = 100


def test_request_modal_is_within_discord_limits():
    import discord
    import views

    modal = views.RequestModal()
    assert len(modal.title) <= MODAL_TITLE_LIMIT

    fields = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]
    assert fields, "the modal should have text inputs"

    for field in fields:
        assert len(field.label) <= TEXT_INPUT_LABEL_LIMIT, f"label too long: {field.label}"
        if field.placeholder:
            assert len(field.placeholder) <= TEXT_INPUT_PLACEHOLDER_LIMIT, (
                f"placeholder is {len(field.placeholder)} chars, Discord's limit is "
                f"{TEXT_INPUT_PLACEHOLDER_LIMIT} — Discord rejects the entire modal: "
                f"{field.placeholder!r}"
            )


def test_build_card_buttons_are_within_discord_limits():
    import discord
    import views

    build_id = new_build()
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, None, "a.schem", "/x/a.schem")

    # Cover every state, since the card shows a different button set in each.
    # The children are DynamicItem wrappers; the real Button is at `.item`.
    seen = 0
    for setup in (lambda: None,
                  lambda: db.claim_build(build_id, BUILDER_A),
                  lambda: db.complete_build(build_id)):
        setup()
        for wrapper in views.build_view(db.get_build(build_id)).children:
            button = getattr(wrapper, "item", wrapper)
            if not isinstance(button, discord.ui.Button):
                continue
            seen += 1
            assert len(button.label) <= BUTTON_LABEL_LIMIT, f"button label too long: {button.label}"
            assert button.custom_id, "persistent buttons need a custom_id to survive restarts"
            assert views.DYNAMIC_ITEMS, "buttons must be registered for restarts"
    assert seen, "expected buttons on the build card"


def test_slash_command_text_is_within_discord_limits():
    """An over-long description makes the whole command tree fail to sync."""
    import asyncio

    import discord
    from discord.ext import commands as dcommands

    async def build_tree():
        intents = discord.Intents.default()
        intents.members = True
        bot = dcommands.Bot(command_prefix="!", intents=intents)
        for cog in ("cogs.setup", "cogs.builds"):
            await bot.load_extension(cog)
        found = list(bot.tree.get_commands())
        await bot.close()
        return found

    found = asyncio.run(build_tree())
    assert found, "no commands registered"

    for command in found:
        assert len(command.description) <= COMMAND_DESCRIPTION_LIMIT, (
            f"/{command.name} description is {len(command.description)} chars"
        )
        for param in getattr(command, "parameters", []):
            assert len(param.description) <= COMMAND_DESCRIPTION_LIMIT, (
                f"/{command.name} parameter '{param.name}' description is too long"
            )


def test_version_label_counts_only_files():
    import embeds

    build_id = new_build()
    assert embeds.version_label(build_id) is None
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, "note only")
    assert embeds.version_label(build_id) is None
    db.add_update(build_id, BUILDER_A, db.KIND_PROGRESS, None, "a.schem", "/x/a.schem")
    assert embeds.version_label(build_id) == "v1"

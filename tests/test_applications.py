"""Applications: who may apply, what a form may contain, and deciding once.

The tests that matter most here are the refusals. An application system that
lets somebody apply five times, or lets two staff accept and deny the same
person in the same second, produces a mess that has to be sorted out by hand and
an applicant who has been told two different things.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import applications as apply_lib  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

GUILD = 5150
APPLICANT = 900
OTHER = 901
STAFF = 200
STAFF_TWO = 201
ROLE = 3030

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "applications.db")
    db.init_db()
    yield
    db.set_db_path(config.DB_PATH)


def a_form(key="builder", label="Builder", role_id=ROLE, questions=None):
    db.save_form(
        GUILD,
        key,
        label,
        apply_lib.questions_to_json(questions or apply_lib.GENERIC_QUESTIONS),
        emoji="🔨",
        blurb="Build things",
        role_id=role_id,
    )
    return db.get_form(GUILD, key)


def an_application(form=None, applicant=APPLICANT, answers=None):
    form = form or a_form()
    return db.create_application(
        GUILD,
        form["key"],
        form["label"],
        applicant,
        apply_lib.answers_to_json(answers or [("Your Minecraft username", "Dex")]),
    )


# --------------------------------------------------------------------------
# forms
# --------------------------------------------------------------------------

def test_a_form_round_trips():
    form = a_form()
    assert form["label"] == "Builder"
    assert form["role_id"] == ROLE
    assert form["is_open"] == 1
    assert len(apply_lib.questions_from_json(form["questions"])) == 3


def test_saving_the_same_key_edits_rather_than_duplicates():
    """The key is baked into the panel button's custom_id — a second row with the
    same key would leave one of them unreachable."""
    a_form()
    a_form(label="Builder", role_id=4040)

    forms = db.list_forms(GUILD)
    assert len(forms) == 1
    assert forms[0]["role_id"] == 4040


def test_closing_a_form_keeps_it_but_hides_it_from_the_open_list():
    a_form()
    assert db.set_form_open(GUILD, "builder", False) is True
    assert db.list_forms(GUILD, only_open=True) == []
    assert len(db.list_forms(GUILD)) == 1


def test_deleting_a_form_keeps_the_applications_already_made():
    """Those are the record of decisions people were told about."""
    form = a_form()
    application_id = an_application(form)
    db.delete_form(GUILD, form["key"])

    assert db.get_form(GUILD, form["key"]) is None
    assert db.get_application(application_id) is not None


def test_forms_are_scoped_to_their_guild():
    a_form()
    assert db.get_form(9999, "builder") is None
    assert db.list_forms(9999) == []


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,expected",
    [
        ("Builder", "builder"),
        ("Script writer", "script-writer"),
        ("Staff / helper", "staff-helper"),
        ("  Spaced  out  ", "spaced-out"),
        ("Bouwer!!!", "bouwer"),
        ("Mediëval", "medi-val"),
        ("", "form"),
        ("!!!", "form"),
        ("🔨", "form"),
    ],
)
def test_slugs(label, expected):
    assert apply_lib.slug(label) == expected


def test_every_slug_survives_the_button_template():
    """The custom_id is parsed back with a regex; a key outside its charset means
    the button silently stops working after a restart."""
    import re

    import views

    pattern = re.compile(views.ApplyButton.__discord_ui_compiled_template__.pattern)
    for label in ("Builder", "Script writer", "Staff / helper", "Événement", "x" * 200, "!!!"):
        key = apply_lib.slug(label)
        assert apply_lib.is_valid_key(key), key
        assert pattern.fullmatch(f"ap:go:{key}"), key


def test_slugs_are_bounded():
    assert len(apply_lib.slug("a very long position name that nobody would type")) <= apply_lib.MAX_KEY


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------

def test_a_form_may_not_have_more_questions_than_a_modal_holds():
    problems = apply_lib.validate_questions(["one", "two", "three", "four", "five", "six"])
    assert problems and "5" in problems[0]


def test_five_questions_are_fine():
    assert apply_lib.validate_questions(["a", "b", "c", "d", "e"]) == []


def test_an_over_long_question_is_refused_with_the_number_in_it():
    problems = apply_lib.validate_questions(["fine", "x" * 60])
    assert len(problems) == 1
    assert "Question 2" in problems[0]


def test_a_form_needs_at_least_one_question():
    assert apply_lib.validate_questions([]) != []


def test_questions_accept_both_strings_and_dicts():
    normalised = apply_lib.normalise_questions(
        ["plain", {"label": "detailed", "long": True, "placeholder": "hint"}]
    )
    assert normalised[0] == {"label": "plain", "long": False, "placeholder": None, "required": True}
    assert normalised[1]["long"] is True and normalised[1]["placeholder"] == "hint"


def test_corrupt_question_json_does_not_raise():
    """A bad row must not take the whole panel down with it."""
    assert apply_lib.questions_from_json("not json at all") == []
    assert apply_lib.questions_from_json(None) == []
    assert apply_lib.questions_from_json('{"not": "a list"}') == []


def test_every_default_form_is_within_discord_limits():
    for form in apply_lib.DEFAULT_FORMS:
        assert apply_lib.validate_questions(form["questions"]) == []
        assert apply_lib.slug(form["label"]) == form["key"]
        assert len(form["label"]) <= apply_lib.MAX_BUTTON_LABEL
        for question in form["questions"]:
            assert len(question["label"]) <= apply_lib.MAX_QUESTION_LABEL
            placeholder = question["placeholder"]
            assert placeholder is None or len(placeholder) <= apply_lib.MAX_PLACEHOLDER


def test_the_generic_questions_are_valid_too():
    assert apply_lib.validate_questions(apply_lib.GENERIC_QUESTIONS) == []


def test_the_positions_this_server_recruits_for_are_all_there():
    labels = {form["label"] for form in apply_lib.DEFAULT_FORMS}
    assert labels == {
        "Builder", "Script writer", "Staff / helper", "Video editor", "Promotor"
    }


def test_every_default_question_carries_a_hint():
    """The label is capped at 45 characters — too short to say what a good answer
    looks like. The placeholder is where that goes, so a question without one is
    a question the applicant has to guess at."""
    for form in apply_lib.DEFAULT_FORMS:
        for question in form["questions"]:
            assert question["placeholder"], (
                f"{form['label']} → “{question['label']}” has no hint text"
            )


def test_every_position_asks_for_availability():
    """"Are you active?" gets "yes". Hours and a timezone get something you can
    actually plan a build week around."""
    for form in apply_lib.DEFAULT_FORMS:
        labels = " ".join(q["label"].lower() for q in form["questions"])
        assert "timezone" in labels, f"{form['label']} never asks when they're around"


def test_every_position_asks_at_least_two_open_questions():
    """A form of one-line boxes sorts nobody: everybody's answers look the same."""
    for form in apply_lib.DEFAULT_FORMS:
        open_ended = [q for q in form["questions"] if q["long"]]
        assert len(open_ended) >= 2, f"{form['label']} is all one-liners"


def test_no_position_asks_the_same_thing_twice():
    for form in apply_lib.DEFAULT_FORMS:
        labels = [q["label"] for q in form["questions"]]
        assert len(labels) == len(set(labels)), f"{form['label']} repeats a question"


# --------------------------------------------------------------------------
# matching positions to roles that already exist
#
# Taken from the real server's role list, in its real order. A position that
# hands out nothing is the complaint; a position that hands out the wrong thing
# is the accident.
# --------------------------------------------------------------------------

ASTRA_ROLES = [
    "Owner", "Pov", "Admin", "Moderator", "Trainee staff",
    "Lead Script Writer", "Script Writer", "Builder", "Promotors", "Editors",
    "Trusted+", "Trusted", "accepted", "Member",
    "Pyro Fan", "Micro Fan", "Brother of Microman_J", "Dutch",
]


def astra(assignable=None):
    """(id, name, assignable) triples, ids being the position in the list."""
    blocked = set(assignable or ["Owner", "Admin"])  # these hold administrator
    return [
        (index, name, name not in blocked) for index, name in enumerate(ASTRA_ROLES, start=1)
    ]


def role_named(name):
    return ASTRA_ROLES.index(name) + 1


@pytest.mark.parametrize(
    "key,expected",
    [
        (apply_lib.KEY_BUILDER, "Builder"),
        (apply_lib.KEY_SCRIPTER, "Script Writer"),
        (apply_lib.KEY_STAFF, "Trainee staff"),
        (apply_lib.KEY_EDITOR, "Editors"),
        (apply_lib.KEY_PROMOTOR, "Promotors"),
    ],
)
def test_each_position_finds_the_right_role_on_the_real_server(key, expected):
    assert apply_lib.guess_role_id(key, astra()) == role_named(expected)


def test_a_new_script_writer_does_not_get_the_lead_role():
    """"Script Writer" is inside "Lead Script Writer", so a careless substring
    match promotes every applicant on day one."""
    assert apply_lib.guess_role_id(apply_lib.KEY_SCRIPTER, astra()) == role_named("Script Writer")


def test_a_staff_applicant_does_not_land_on_moderator_or_admin():
    picked = apply_lib.guess_role_id(apply_lib.KEY_STAFF, astra())
    assert picked not in (role_named("Moderator"), role_named("Admin"), role_named("Owner"))
    assert picked == role_named("Trainee staff")


def test_a_role_that_runs_the_server_is_never_guessed():
    """However well the name matches. An admin naming it explicitly is a
    different thing and doesn't come through here."""
    roles = [(1, "Builder", False), (2, "Builders", False)]
    assert apply_lib.guess_role_id(apply_lib.KEY_BUILDER, roles) is None


def test_decorated_role_names_still_match():
    roles = [(7, "『🔨』 Builder ✦", True), (8, "Member", True)]
    assert apply_lib.guess_role_id(apply_lib.KEY_BUILDER, roles) == 7


def test_plural_and_singular_both_match():
    for name in ("Editor", "Editors", "Video Editor", "video editors"):
        assert apply_lib.guess_role_id(apply_lib.KEY_EDITOR, [(3, name, True)]) == 3
    for name in ("Promotor", "Promotors", "Promoter", "Promoters"):
        assert apply_lib.guess_role_id(apply_lib.KEY_PROMOTOR, [(4, name, True)]) == 4


def test_a_server_with_no_matching_role_gets_nothing_rather_than_something_wrong():
    roles = [(1, "Member", True), (2, "Dutch", True), (3, "Trusted", True)]
    for key in (apply_lib.KEY_EDITOR, apply_lib.KEY_PROMOTOR, apply_lib.KEY_STAFF):
        assert apply_lib.guess_role_id(key, roles) is None


def test_a_role_set_by_hand_is_never_replaced_by_a_guess():
    """Re-running setup must not undo a deliberate choice."""
    from cogs.applications import ApplicationsCog

    class Role:
        def __init__(self, role_id, name):
            self.id, self.name = role_id, name
            self.managed = False
            self.permissions = type("P", (), {"administrator": False, "manage_guild": False})()

        def is_default(self):
            return False

    class Guild:
        id = GUILD
        roles = [Role(role_named(n), n) for n in ASTRA_ROLES]

        def get_role(self, role_id):
            return None

    a_form(key=apply_lib.KEY_STAFF, label="Staff / helper", role_id=12345)
    ApplicationsCog(None).seed_default_forms(Guild())

    assert db.get_form(GUILD, apply_lib.KEY_STAFF)["role_id"] == 12345


def test_a_position_with_no_role_gets_one_filled_in_on_the_next_setup():
    """The actual complaint: positions created before this existed hand out
    nothing, and nobody wants to retype five questions to fix that."""
    from cogs.applications import ApplicationsCog

    class Role:
        def __init__(self, role_id, name):
            self.id, self.name = role_id, name
            self.managed = False
            self.permissions = type("P", (), {"administrator": False, "manage_guild": False})()

        def is_default(self):
            return False

    class Guild:
        id = GUILD
        roles = [Role(role_named(n), n) for n in ASTRA_ROLES]

        def get_role(self, role_id):
            return None

    a_form(key=apply_lib.KEY_STAFF, label="Staff / helper", role_id=None,
           questions=["A question I wrote myself"])
    ApplicationsCog(None).seed_default_forms(Guild())

    form = db.get_form(GUILD, apply_lib.KEY_STAFF)
    assert form["role_id"] == role_named("Trainee staff")
    assert apply_lib.questions_from_json(form["questions"]) == apply_lib.normalise_questions(
        ["A question I wrote myself"]
    ), "filling in the role must not overwrite the questions"


def test_default_keys_are_unique():
    """Two positions sharing a key would leave one of them unreachable from the
    panel, since the key is what the button carries."""
    keys = [form["key"] for form in apply_lib.DEFAULT_FORMS]
    assert len(keys) == len(set(keys))


def test_setup_adds_positions_a_server_is_missing_without_touching_the_rest():
    """Positions get added to the list over time. A server set up last month
    should pick them up by re-running setup, not by retyping five questions."""
    from cogs.applications import ApplicationsCog

    class Guild:
        id = GUILD

    cog = ApplicationsCog(None)

    # A server that already has an edited Builder and nothing else.
    a_form(label="Builder", role_id=9999, questions=["Only question I want"])

    added = cog.seed_default_forms(Guild())

    assert "Video editor" in added and "Promotor" in added
    assert "Builder" not in added, "an existing position must not be overwritten"

    builder = db.get_form(GUILD, "builder")
    assert builder["role_id"] == 9999
    assert len(apply_lib.questions_from_json(builder["questions"])) == 1

    assert len(db.list_forms(GUILD)) == len(apply_lib.DEFAULT_FORMS)


def test_seeding_twice_adds_nothing_the_second_time():
    from cogs.applications import ApplicationsCog

    class Guild:
        id = GUILD

    cog = ApplicationsCog(None)
    assert len(cog.seed_default_forms(Guild())) == len(apply_lib.DEFAULT_FORMS)
    assert cog.seed_default_forms(Guild()) == []


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------

def test_answers_round_trip_with_their_questions():
    """Stored together, so editing a form later doesn't rewrite what somebody
    already submitted."""
    pairs = [("Your Minecraft username", "Dex"), ("Why?", "I like building")]
    assert apply_lib.answers_from_json(apply_lib.answers_to_json(pairs)) == pairs


def test_answers_survive_awkward_characters():
    pairs = [("Naam?", 'Dexter "the builder" — ëéï 🔨\nsecond line')]
    assert apply_lib.answers_from_json(apply_lib.answers_to_json(pairs)) == pairs


def test_corrupt_answer_json_does_not_raise():
    assert apply_lib.answers_from_json("}{") == []


def test_summary_uses_the_first_non_empty_answer():
    assert apply_lib.summarise([("q", ""), ("q", "Dex")]) == "Dex"
    assert apply_lib.summarise([]) == "—"
    assert len(apply_lib.summarise([("q", "x" * 500)], 60)) == 60


# --------------------------------------------------------------------------
# who may apply
# --------------------------------------------------------------------------

def test_an_open_position_can_be_applied_for():
    assert apply_lib.why_cannot_apply(a_form(), now=NOW) is None


def test_a_closed_position_cannot():
    a_form()
    db.set_form_open(GUILD, "builder", False)
    reason = apply_lib.why_cannot_apply(db.get_form(GUILD, "builder"), now=NOW)
    assert reason and "closed" in reason


def test_a_deleted_position_cannot():
    assert apply_lib.why_cannot_apply(None, now=NOW) is not None


def test_a_pending_application_blocks_a_second_one():
    """Otherwise one bored person fills the review channel on their own."""
    form = a_form()
    an_application(form)
    reason = apply_lib.why_cannot_apply(
        form, pending=db.pending_application_for(GUILD, "builder", APPLICANT), now=NOW
    )
    assert reason and "waiting for a decision" in reason.lower()


def test_someone_who_already_has_the_role_is_told_so():
    reason = apply_lib.why_cannot_apply(a_form(), already_has_role=True, now=NOW)
    assert reason and "already have" in reason


def test_a_denial_starts_a_cooldown():
    form = a_form()
    application_id = an_application(form)
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, "not this time")

    denial = db.last_denial_for(GUILD, "builder", APPLICANT)
    assert denial is not None

    decided = datetime.fromisoformat(denial["decided_at"])
    reason = apply_lib.why_cannot_apply(
        form, last_denial=denial, cooldown_days=7, now=decided + timedelta(days=1)
    )
    assert reason and "wasn't accepted" in reason


def test_the_cooldown_expires():
    form = a_form()
    application_id = an_application(form)
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, None)

    denial = db.last_denial_for(GUILD, "builder", APPLICANT)
    decided = datetime.fromisoformat(denial["decided_at"])
    assert (
        apply_lib.why_cannot_apply(
            form, last_denial=denial, cooldown_days=7, now=decided + timedelta(days=8)
        )
        is None
    )


def test_a_zero_day_cooldown_lets_them_apply_immediately():
    form = a_form()
    application_id = an_application(form)
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, None)
    denial = db.last_denial_for(GUILD, "builder", APPLICANT)

    assert apply_lib.why_cannot_apply(form, last_denial=denial, cooldown_days=0, now=NOW) is None


def test_an_acceptance_does_not_start_a_cooldown():
    """Only denials do — an accepted person is stopped by holding the role."""
    form = a_form()
    application_id = an_application(form)
    db.decide_application(application_id, db.APPLY_ACCEPTED, STAFF, None)
    assert db.last_denial_for(GUILD, "builder", APPLICANT) is None


def test_one_persons_application_does_not_block_another_person():
    form = a_form()
    an_application(form, applicant=APPLICANT)
    assert db.pending_application_for(GUILD, "builder", OTHER) is None


def test_a_pending_application_for_one_position_does_not_block_another():
    builder = a_form()
    scripter = a_form(key="scripter", label="Script writer", role_id=None)
    an_application(builder)
    assert db.pending_application_for(GUILD, scripter["key"], APPLICANT) is None


def test_a_corrupt_decision_timestamp_does_not_block_forever():
    """A row from a hand-edited database must fail open, not lock somebody out."""
    assert apply_lib.reapply_at("not a date", 7) is None
    assert apply_lib.reapply_at(None, 7) is None


# --------------------------------------------------------------------------
# deciding, exactly once
# --------------------------------------------------------------------------

def test_accepting_records_who_and_when():
    application_id = an_application()
    assert db.decide_application(application_id, db.APPLY_ACCEPTED, STAFF, "welcome") is True

    application = db.get_application(application_id)
    assert application["status"] == db.APPLY_ACCEPTED
    assert application["decided_by"] == STAFF
    assert application["decision_note"] == "welcome"
    assert application["decided_at"]


def test_two_staff_deciding_at_once_cannot_both_win():
    """The applicant must never get an acceptance and a rejection."""
    application_id = an_application()
    assert db.decide_application(application_id, db.APPLY_ACCEPTED, STAFF, None) is True
    assert db.decide_application(application_id, db.APPLY_DENIED, STAFF_TWO, None) is False
    assert db.get_application(application_id)["status"] == db.APPLY_ACCEPTED
    assert db.get_application(application_id)["decided_by"] == STAFF


def test_a_decided_application_cannot_be_decided_again():
    application_id = an_application()
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, None)
    assert db.decide_application(application_id, db.APPLY_ACCEPTED, STAFF, None) is False


def test_an_unknown_status_is_refused():
    application_id = an_application()
    with pytest.raises(ValueError):
        db.decide_application(application_id, "maybe", STAFF, None)


def test_listing_by_status_and_position():
    builder = a_form()
    scripter = a_form(key="scripter", label="Script writer", role_id=None)
    pending = an_application(builder)
    an_application(builder, applicant=OTHER)
    an_application(scripter, applicant=OTHER)
    db.decide_application(pending, db.APPLY_ACCEPTED, STAFF, None)

    assert len(db.list_applications(GUILD)) == 3
    assert len(db.list_applications(GUILD, db.APPLY_PENDING)) == 2
    assert len(db.list_applications(GUILD, None, "scripter")) == 1
    assert db.count_applications(GUILD) == {db.APPLY_PENDING: 2, db.APPLY_ACCEPTED: 1}


def test_applications_are_scoped_to_their_guild():
    an_application()
    assert db.list_applications(9999) == []


def test_application_numbers_are_not_reused():
    """Old Accept buttons carry the number in their id; reuse would decide the
    wrong application."""
    first = an_application()
    db.delete_application(first)
    assert an_application() != first


def test_the_review_message_can_be_found_again_after_a_restart():
    application_id = an_application()
    db.attach_application_message(application_id, 12345, 67890)

    application = db.get_application(application_id)
    assert (application["message_id"], application["channel_id"]) == (12345, 67890)


# --------------------------------------------------------------------------
# emoji, which admins type by hand
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["🔨", "📜", "<:astra:123456789012345678>", "<a:spin:1234567890>"])
def test_real_emoji_are_accepted(value):
    assert apply_lib.looks_like_emoji(value) is True


@pytest.mark.parametrize("value", ["builder", "", None, ":hammer:", "a much longer sentence"])
def test_anything_discord_would_reject_is_dropped(value):
    """discord.py accepts these and the API then rejects the whole panel."""
    assert apply_lib.looks_like_emoji(value) is False


# --------------------------------------------------------------------------
# what people actually see
# --------------------------------------------------------------------------

def test_the_review_card_stays_inside_discords_limits():
    import embeds

    long_answers = [(f"Question {n}" * 5, "x" * 4000) for n in range(1, 6)]
    application_id = an_application(answers=long_answers)
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, "y" * 3000)

    embed = embeds.application_card(db.get_application(application_id), None)
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert len(field.name) <= 256
        assert len(field.value) <= 1024


def test_a_blank_answer_still_renders():
    import embeds

    application_id = an_application(answers=[("Optional question", "")])
    embed = embeds.application_card(db.get_application(application_id), None)
    assert any("blank" in field.value for field in embed.fields)


def test_the_panel_renders_with_no_positions_at_all():
    import embeds

    class FakeGuild:
        icon = None
        name = "ASTRA Smp Events"
        id = GUILD

    embed = embeds.application_panel(FakeGuild(), [])
    assert "No positions" in embed.description
    assert len(embed.description) <= 4096


def test_the_panel_survives_far_more_positions_than_anyone_needs():
    import embeds

    class FakeGuild:
        icon = None
        name = "ASTRA Smp Events"
        id = GUILD

    for n in range(30):
        a_form(key=f"role-{n}", label=f"Position {n}", role_id=None)
    forms = db.list_forms(GUILD)

    embed = embeds.application_panel(FakeGuild(), forms)
    assert len(embed.description) <= 4096


def test_the_panel_never_exceeds_discords_button_limit():
    import views

    for n in range(30):
        a_form(key=f"role-{n}", label=f"Position {n}", role_id=None)

    view = views.application_panel_view(db.list_forms(GUILD))
    assert len(view.children) <= 25


def test_panel_buttons_are_within_discord_limits():
    import discord
    import views

    a_form(label="A position with a really quite long name for a button", role_id=None)
    for view in (views.application_panel_view(db.list_forms(GUILD)), views.application_review_view(1)):
        for wrapper in view.children:
            button = getattr(wrapper, "item", wrapper)
            if isinstance(button, discord.ui.Button):
                assert len(button.label) <= 80
                assert button.custom_id


def test_the_form_modal_is_within_discord_limits():
    import discord
    import views

    for spec in apply_lib.DEFAULT_FORMS:
        form = a_form(
            key=spec["key"], label=spec["label"], role_id=None, questions=spec["questions"]
        )
        modal = views.ApplicationModal(form)
        fields = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]

        assert len(modal.title) <= 45
        assert 0 < len(fields) <= apply_lib.MAX_QUESTIONS
        for field in fields:
            assert len(field.label) <= 45
            if field.placeholder:
                assert len(field.placeholder) <= 100, (
                    f"placeholder is {len(field.placeholder)} chars — Discord rejects the modal"
                )


def test_a_form_with_more_questions_than_a_modal_holds_is_truncated_not_crashed():
    """A row edited by hand shouldn't stop anybody applying."""
    import discord
    import views

    form = a_form(questions=[f"Question {n}" for n in range(1, 9)])
    modal = views.ApplicationModal(form)
    fields = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]
    assert len(fields) == apply_lib.MAX_QUESTIONS


def test_the_decision_dm_says_which_position_and_which_server():
    import embeds

    class FakeGuild:
        icon = None
        name = "ASTRA Smp Events"
        id = GUILD

    class FakeRole:
        name = "Builder"

    application_id = an_application()
    db.decide_application(application_id, db.APPLY_ACCEPTED, STAFF, "see you in game")
    embed = embeds.application_decision_dm(
        db.get_application(application_id), FakeGuild(), True, FakeRole()
    )

    assert "Builder" in embed.description
    assert "ASTRA Smp Events" in embed.description
    assert "see you in game" in embed.description
    assert len(embed) <= 6000


def test_a_rejection_dm_carries_the_reason_and_no_role():
    import embeds

    class FakeGuild:
        icon = None
        name = "ASTRA Smp Events"
        id = GUILD

    application_id = an_application()
    db.decide_application(application_id, db.APPLY_DENIED, STAFF, "too new — try again later")
    embed = embeds.application_decision_dm(
        db.get_application(application_id), FakeGuild(), False, None
    )

    assert "too new" in embed.description
    assert "accepted" in embed.description.lower()


# --------------------------------------------------------------------------
# the cog wiring
# --------------------------------------------------------------------------

def test_every_application_button_is_registered_as_persistent():
    """Not registered means the buttons stop responding at the next restart —
    and this host restarts every few hours."""
    import views

    for item in (views.ApplyButton, views.ApplicationAcceptButton, views.ApplicationDenyButton):
        assert item in views.DYNAMIC_ITEMS


def test_the_commands_people_need_exist():
    import asyncio

    import discord
    from discord.ext import commands

    async def load():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        await bot.load_extension("cogs.applications")
        try:
            return {c.name: c for c in bot.tree.get_commands()}
        finally:
            await bot.close()

    names = asyncio.run(load())
    for expected in (
        "apply", "apply-setup", "apply-panel", "apply-form",
        "apply-form-delete", "apply-toggle", "applications", "application",
    ):
        assert expected in names, f"/{expected} is missing"


def test_command_descriptions_fit_discords_limit():
    import asyncio

    import discord
    from discord.ext import commands

    async def load():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        await bot.load_extension("cogs.applications")
        try:
            return [
                (c.name, c.description, list(c.parameters))
                for c in bot.tree.get_commands()
            ]
        finally:
            await bot.close()

    for name, description, parameters in asyncio.run(load()):
        assert 0 < len(description) <= 100, f"/{name} description is {len(description)}"
        for parameter in parameters:
            assert len(parameter.description) <= 100, f"/{name} {parameter.name}"


# --------------------------------------------------------------------------
# deciding, through the cog — the parts that talk to Discord
#
# These use stand-ins rather than a gateway connection. The behaviour worth
# pinning down is what happens when Discord says no: a closed DM, a role the bot
# can't hand out, a review channel it can't post in. Each of those is silent by
# default, and silent is the one outcome that leaves an applicant waiting
# forever.
# --------------------------------------------------------------------------

def sync(test):
    """Run an async test body. pytest-asyncio isn't a dependency of this project
    and isn't worth adding for nine tests."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


class FakeAvatar:
    url = "https://cdn.example.invalid/avatar.png"


class FakePermissions:
    def __init__(self, **flags):
        self.manage_guild = flags.get("manage_guild", False)
        self.manage_roles = flags.get("manage_roles", True)


class FakeRole:
    def __init__(self, role_id=ROLE, name="Builder", position=1):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position

    def is_default(self):
        return False


class FakeMember:
    def __init__(self, user_id=APPLICANT, roles=(), admin=False, dm_error=None):
        self.id = user_id
        self.roles = list(roles)
        self.guild_permissions = FakePermissions(manage_guild=admin)
        self.display_avatar = FakeAvatar()
        self.display_name = f"user-{user_id}"
        self.created_at = NOW
        self.mention = f"<@{user_id}>"
        self.dm_error = dm_error
        self.dms_received = []
        self.roles_added = []
        self.top_role = FakeRole(1, "bot", position=50)

    async def send(self, *, embed=None, **kwargs):
        if self.dm_error is not None:
            raise self.dm_error
        self.dms_received.append(embed)

    async def add_roles(self, role, reason=None):
        self.roles_added.append(role)
        self.roles.append(role)


class FakeMessage:
    def __init__(self, message_id=555):
        self.id = message_id
        self.jump_url = f"https://discord.invalid/{message_id}"
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id=4321, fail=None):
        self.id = channel_id
        self.sent = []
        self.fail = fail
        self.message = FakeMessage()

    async def send(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(kwargs)
        return self.message

    async def fetch_message(self, message_id):
        return self.message


class FakeGuild:
    def __init__(self, channel=None, role=None, members=None):
        self.id = GUILD
        self.name = "ASTRA Smp Events"
        self.icon = None
        self.channel = channel or FakeChannel()
        self.role = role
        self.members = members or {}
        self.me = FakeMember(1, admin=True)
        self.me.guild_permissions = FakePermissions(manage_guild=True, manage_roles=True)

    def get_channel(self, channel_id):
        return self.channel if self.channel and channel_id == self.channel.id else None

    def get_role(self, role_id):
        return self.role if self.role and self.role.id == role_id else None

    def get_member(self, user_id):
        return self.members.get(user_id)


class FakeResponse:
    def __init__(self):
        self.done = False
        self.messages = []
        self.modals = []

    def is_done(self):
        return self.done

    async def send_message(self, **kwargs):
        self.done = True
        self.messages.append(kwargs)

    async def defer(self, **kwargs):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, **kwargs):
        self.messages.append(kwargs)


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.user = user
        user.guild = guild  # a real Member always knows its guild
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def replies(self):
        return [
            m["embed"].description or ""
            for m in (*self.response.messages, *self.followup.messages)
            if m.get("embed") is not None
        ]


class FakeBot:
    def __init__(self, users=None):
        self.users = users or {}

    def get_user(self, user_id):
        return self.users.get(user_id)


def a_cog(bot=None):
    import discord

    from cogs.applications import ApplicationsCog

    # discord.py's Channel type checks are the only thing standing between these
    # fakes and the real code path, so make them accept our stand-ins.
    return ApplicationsCog(bot or FakeBot()), discord


@pytest.fixture
def patched_channel_type(monkeypatch):
    """Let the cog treat FakeChannel/FakeMember as the real thing."""
    import discord

    real_isinstance = isinstance

    def patched(obj, kind):
        if kind is discord.TextChannel and real_isinstance(obj, FakeChannel):
            return True
        return real_isinstance(obj, kind)

    import cogs.applications as cog_module

    monkeypatch.setattr(cog_module, "isinstance", patched, raising=False)
    return patched


def a_configured_guild(role=None, applicant=None, channel=None):
    channel = channel or FakeChannel()
    db.save_config(
        GUILD, apply_review_channel_id=channel.id, apply_reviewer_role_id=None
    )
    applicant = applicant or FakeMember(APPLICANT)
    return FakeGuild(channel=channel, role=role, members={APPLICANT: applicant}), applicant


@sync
async def test_accepting_gives_the_role_and_dms_the_applicant(patched_channel_type):
    role = FakeRole()
    guild, applicant = a_configured_guild(role=role)
    form = a_form()
    application_id = an_application(form)
    db.attach_application_message(application_id, 555, guild.channel.id)

    cog, _ = a_cog()
    staff = FakeMember(STAFF, admin=True)
    interaction = FakeInteraction(guild, staff)

    await cog.accept_application(interaction, application_id, None)

    assert db.get_application(application_id)["status"] == db.APPLY_ACCEPTED
    assert applicant.roles_added == [role], "the role is the whole point of accepting"
    assert applicant.dms_received, "the applicant was never told"
    assert guild.channel.message.edits, "the review post still shows live buttons"
    assert guild.channel.message.edits[-1]["view"] is None


@sync
async def test_a_closed_dm_is_reported_to_the_reviewer(patched_channel_type):
    """Otherwise staff think the person was told and the person thinks they
    were ignored."""
    import discord

    applicant = FakeMember(
        APPLICANT, dm_error=discord.Forbidden(_a_response(403), "DMs closed")
    )
    guild, _ = a_configured_guild(role=FakeRole(), applicant=applicant)
    application_id = an_application(a_form())
    db.attach_application_message(application_id, 555, guild.channel.id)

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, FakeMember(STAFF, admin=True))
    await cog.accept_application(interaction, application_id, None)

    replies = " ".join(interaction.replies())
    assert "DMs closed" in replies or "haven't been told" in replies
    assert db.get_application(application_id)["status"] == db.APPLY_ACCEPTED, (
        "a failed DM must not undo a decision staff already made"
    )


@sync
async def test_a_role_the_bot_cannot_hand_out_is_reported_with_the_fix(patched_channel_type):
    high_role = FakeRole(position=999)
    guild, applicant = a_configured_guild(role=high_role)
    application_id = an_application(a_form())
    db.attach_application_message(application_id, 555, guild.channel.id)

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, FakeMember(STAFF, admin=True))
    await cog.accept_application(interaction, application_id, None)

    replies = " ".join(interaction.replies())
    assert "above my own role" in replies
    assert applicant.roles_added == []
    assert db.get_application(application_id)["status"] == db.APPLY_ACCEPTED


@sync
async def test_someone_who_is_not_staff_cannot_decide(patched_channel_type):
    guild, _ = a_configured_guild(role=FakeRole())
    application_id = an_application(a_form())

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, FakeMember(OTHER, admin=False))
    await cog.accept_application(interaction, application_id, None)

    assert db.get_application(application_id)["status"] == db.APPLY_PENDING
    assert "Only staff" in " ".join(interaction.replies())


@sync
async def test_the_second_reviewer_is_told_who_got_there_first(patched_channel_type):
    guild, _ = a_configured_guild(role=FakeRole())
    application_id = an_application(a_form())
    db.attach_application_message(application_id, 555, guild.channel.id)

    cog, _ = a_cog()
    await cog.accept_application(
        FakeInteraction(guild, FakeMember(STAFF, admin=True)), application_id, None
    )

    second = FakeInteraction(guild, FakeMember(STAFF_TWO, admin=True))
    await cog.deny_application(second, application_id, "no")

    replies = " ".join(second.replies())
    assert "already decided" in replies
    assert str(STAFF) in replies
    assert db.get_application(application_id)["status"] == db.APPLY_ACCEPTED


@sync
async def test_submitting_posts_it_for_review_and_remembers_where(patched_channel_type):
    guild, applicant = a_configured_guild(role=FakeRole())
    form = a_form()

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, applicant)
    await cog.submit_application(interaction, form["key"], [("Username", "Dex")])

    rows = db.list_applications(GUILD, db.APPLY_PENDING)
    assert len(rows) == 1
    assert rows[0]["message_id"] == guild.channel.message.id
    assert rows[0]["channel_id"] == guild.channel.id
    assert guild.channel.sent, "staff were never shown the application"


@sync
async def test_an_application_that_cannot_be_posted_is_not_left_dangling(patched_channel_type):
    """A saved row nobody can see means an applicant waiting for a decision on
    something staff were never shown."""
    import discord

    channel = FakeChannel(fail=discord.HTTPException(_a_response(403), "no perms"))
    guild, applicant = a_configured_guild(role=FakeRole(), channel=channel)
    form = a_form()

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, applicant)
    await cog.submit_application(interaction, form["key"], [("Username", "Dex")])

    assert db.list_applications(GUILD) == []
    assert "hasn't been saved" in " ".join(interaction.replies())


@sync
async def test_applying_is_refused_before_the_form_is_shown(patched_channel_type):
    """Nobody should type five answers and only then be told it's closed."""
    guild, applicant = a_configured_guild(role=FakeRole())
    form = a_form()
    db.set_form_open(GUILD, form["key"], False)

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, applicant)
    blocker = await cog.why_cannot_apply(interaction, db.get_form(GUILD, form["key"]))
    assert blocker and "closed" in blocker


@sync
async def test_applying_before_setup_says_so(patched_channel_type):
    form = a_form()
    guild = FakeGuild(channel=FakeChannel(), members={APPLICANT: FakeMember(APPLICANT)})

    cog, _ = a_cog()
    interaction = FakeInteraction(guild, FakeMember(APPLICANT))
    blocker = await cog.why_cannot_apply(interaction, form)
    assert blocker and "apply-setup" in blocker


def _a_response(status: int):
    """The minimum discord.py needs to construct an HTTP error."""

    class Response:
        def __init__(self):
            self.status = status
            self.reason = "test"

    return Response()


def test_state_survives_a_restart():
    """Forms and pending applications live in the database that syncs to the
    state branch, so a restart mid-review resumes rather than losing it."""
    form = a_form()
    application_id = an_application(form)
    db.attach_application_message(application_id, 111, 222)

    # A fresh connection is what a restarted process gets.
    db.set_db_path(db._db_path)

    assert db.get_form(GUILD, "builder")["label"] == "Builder"
    restored = db.get_application(application_id)
    assert restored["status"] == db.APPLY_PENDING
    assert restored["message_id"] == 111

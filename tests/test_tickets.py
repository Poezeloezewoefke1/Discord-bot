"""Tickets: the one-per-person rule, the lifecycle, and transcripts.

The transcript tests care most about the case where the Message Content intent is
off, because that produces a file that is technically fine and practically empty —
the failure mode most likely to be noticed only when someone needs the record.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402
import tickets  # noqa: E402

GUILD = 7777
OPENER = 100
OTHER = 101
STAFF = 200


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "tickets.db")
    db.init_db()
    yield
    db.set_db_path(config.DB_PATH)


def new_ticket(kind=tickets.KIND_HELP, opener=OPENER, subject="can't connect", about=None):
    return db.create_ticket(GUILD, opener, kind, subject, about)


# --------------------------------------------------------------------------
# one open ticket per person
# --------------------------------------------------------------------------

def test_a_new_person_has_no_open_ticket():
    assert db.open_ticket_for(GUILD, OPENER) is None


def test_an_open_ticket_blocks_a_second_one():
    """Without this one bored member can create fifty channels."""
    first = new_ticket()
    existing = db.open_ticket_for(GUILD, OPENER)
    assert existing is not None and existing["id"] == first


def test_a_claimed_ticket_still_blocks():
    ticket_id = new_ticket()
    db.claim_ticket(ticket_id, STAFF)
    assert db.open_ticket_for(GUILD, OPENER) is not None


def test_closing_frees_the_person_to_open_another():
    ticket_id = new_ticket()
    db.close_ticket(ticket_id, STAFF, "sorted")
    assert db.open_ticket_for(GUILD, OPENER) is None


def test_one_persons_ticket_does_not_block_another_person():
    new_ticket(opener=OPENER)
    assert db.open_ticket_for(GUILD, OTHER) is None


def test_tickets_are_scoped_to_their_guild():
    new_ticket()
    assert db.open_ticket_for(9999, OPENER) is None


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def test_full_lifecycle():
    ticket_id = new_ticket()
    db.attach_ticket_channel(ticket_id, 555)
    assert db.get_ticket(ticket_id)["status"] == db.TICKET_OPEN

    assert db.claim_ticket(ticket_id, STAFF) is True
    assert db.get_ticket(ticket_id)["claimed_by"] == STAFF

    assert db.close_ticket(ticket_id, STAFF, "resolved") is True
    closed = db.get_ticket(ticket_id)
    assert closed["status"] == db.TICKET_CLOSED
    assert closed["closed_by"] == STAFF and closed["close_reason"] == "resolved"

    assert db.reopen_ticket(ticket_id) is True
    reopened = db.get_ticket(ticket_id)
    assert reopened["status"] == db.TICKET_OPEN
    assert reopened["closed_at"] is None and reopened["close_reason"] is None

    assert db.delete_ticket(ticket_id) is True
    assert db.get_ticket(ticket_id) is None


def test_a_ticket_cannot_be_claimed_twice():
    ticket_id = new_ticket()
    assert db.claim_ticket(ticket_id, STAFF) is True
    assert db.claim_ticket(ticket_id, OTHER) is False
    assert db.get_ticket(ticket_id)["claimed_by"] == STAFF


def test_a_closed_ticket_cannot_be_closed_again():
    ticket_id = new_ticket()
    db.close_ticket(ticket_id, STAFF, None)
    assert db.close_ticket(ticket_id, OTHER, None) is False


def test_an_open_ticket_cannot_be_reopened():
    assert db.reopen_ticket(new_ticket()) is False


def test_lookup_by_channel():
    ticket_id = new_ticket()
    db.attach_ticket_channel(ticket_id, 4242)
    assert db.get_ticket_by_channel(4242)["id"] == ticket_id
    assert db.get_ticket_by_channel(9) is None


def test_ticket_numbers_are_not_reused():
    """Old buttons carry the number in their id; reuse would point them at the wrong ticket."""
    first = new_ticket()
    db.delete_ticket(first)
    assert new_ticket() != first


# --------------------------------------------------------------------------
# channel naming
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kind,ticket_id,expected",
    [
        (tickets.KIND_REPORT, 7, "report-0007"),
        (tickets.KIND_HELP, 7, "help-0007"),
        (tickets.KIND_HELP, 1234, "help-1234"),
        (tickets.KIND_HELP, 99999, "help-99999"),
    ],
)
def test_channel_names(kind, ticket_id, expected):
    assert tickets.channel_name(kind, ticket_id) == expected


def test_closed_channels_are_renamed():
    assert tickets.channel_name(tickets.KIND_REPORT, 7, closed=True) == "closed-0007"


def test_channel_names_never_contain_user_input():
    """The subject is free text from a form; it must not reach a channel name."""
    name = tickets.channel_name(tickets.KIND_HELP, 1)
    assert name == "help-0001"
    assert len(name) <= tickets.MAX_CHANNEL_NAME


def test_topics_are_cleaned_and_bounded():
    topic = tickets.safe_topic("line one\n\n   line two   ", "Dex", 7)
    assert "\n" not in topic
    assert "line one line two" in topic
    assert len(topic) <= 1024


def test_a_huge_subject_cannot_overflow_the_topic():
    assert len(tickets.safe_topic("x" * 5000, "Dex", 7)) <= 1024


def test_a_missing_subject_still_gives_a_topic():
    assert "Ticket #0007" in tickets.safe_topic(None, "Dex", 7)


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------

class FakeAuthor:
    def __init__(self, name="Dex"):
        self.display_name = name

    def __str__(self):
        return self.display_name


class FakeAttachment:
    def __init__(self, filename="spawn.schem"):
        self.filename = filename
        self.url = f"https://cdn.example.invalid/{filename}"


class FakeEmbed:
    def __init__(self, title="an embed"):
        self.title = title


class FakeMessage:
    def __init__(self, content="", author=None, attachments=(), embeds=()):
        self.content = content
        self.author = author or FakeAuthor()
        self.created_at = datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
        self.attachments = list(attachments)
        self.embeds = list(embeds)


def a_ticket():
    ticket_id = new_ticket(kind=tickets.KIND_REPORT, subject="griefed my base", about="SomeUser")
    db.claim_ticket(ticket_id, STAFF)
    db.close_ticket(ticket_id, STAFF, "handled")
    return db.get_ticket(ticket_id)


def test_transcript_includes_the_conversation():
    text = tickets.render_transcript(
        a_ticket(),
        [FakeMessage("they broke my chests"), FakeMessage("thanks, dealt with", FakeAuthor("Mod"))],
        have_message_content=True,
        guild_name="ASTRA Smp Events",
    )
    assert "they broke my chests" in text
    assert "thanks, dealt with" in text
    assert "Dex" in text and "Mod" in text
    assert "2 message(s)" in text


def test_transcript_includes_ticket_details():
    text = tickets.render_transcript(a_ticket(), [], have_message_content=True)
    assert "griefed my base" in text
    assert "SomeUser" in text
    assert "handled" in text


def test_transcript_warns_when_message_text_is_unavailable():
    """The intent being off produces a technically-fine, practically-empty file."""
    text = tickets.render_transcript(
        a_ticket(),
        [FakeMessage(""), FakeMessage("")],
        have_message_content=False,
    )
    assert "message text is missing" in text
    assert "ENABLE_MESSAGE_CONTENT" in text
    assert "text unavailable" in text


def test_transcript_does_not_warn_when_content_is_available():
    text = tickets.render_transcript(
        a_ticket(), [FakeMessage("hello")], have_message_content=True
    )
    assert "message text is missing" not in text


def test_transcript_records_attachments():
    text = tickets.render_transcript(
        a_ticket(),
        [FakeMessage("here it is", attachments=[FakeAttachment("proof.png")])],
        have_message_content=True,
    )
    assert "proof.png" in text
    assert "[attachment]" in text


def test_transcript_records_embeds():
    text = tickets.render_transcript(
        a_ticket(), [FakeMessage("", embeds=[FakeEmbed("Ticket #0001")])],
        have_message_content=True,
    )
    assert "[embed]" in text


def test_an_empty_ticket_still_produces_a_readable_transcript():
    text = tickets.render_transcript(a_ticket(), [], have_message_content=True)
    assert "(no messages)" in text
    assert "0 message(s)" in text


def test_multiline_messages_stay_readable():
    text = tickets.render_transcript(
        a_ticket(), [FakeMessage("line one\nline two")], have_message_content=True
    )
    assert "line one" in text and "line two" in text


def test_transcript_filename():
    assert tickets.transcript_filename(7) == "ticket-0007.txt"


# --------------------------------------------------------------------------
# panel definitions
# --------------------------------------------------------------------------

def test_both_ticket_types_exist():
    assert set(tickets.KINDS) == {tickets.KIND_REPORT, tickets.KIND_HELP}


def test_every_kind_has_what_the_panel_needs():
    for kind, info in tickets.KINDS.items():
        assert info["label"] and info["emoji"] and info["prefix"]
        assert len(info["label"]) <= 80, "button labels are capped at 80 characters"


def test_ticket_open_form_is_within_discord_limits():
    import discord
    import views

    for kind in tickets.KINDS:
        modal = views.TicketOpenModal(kind)
        assert len(modal.title) <= 45
        fields = [c for c in modal.children if isinstance(c, discord.ui.TextInput)]
        assert fields
        for field in fields:
            assert len(field.label) <= 45
            if field.placeholder:
                assert len(field.placeholder) <= 100, (
                    f"placeholder is {len(field.placeholder)} chars — Discord rejects the modal"
                )


def test_ticket_buttons_are_within_discord_limits():
    import discord
    import views

    for view in (views.ticket_panel_view(), views.ticket_open_view(1), views.ticket_closed_view(1)):
        for wrapper in view.children:
            button = getattr(wrapper, "item", wrapper)
            if isinstance(button, discord.ui.Button):
                assert len(button.label) <= 80
                assert button.custom_id

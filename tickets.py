"""Ticket helpers with no Discord calls in them, so they can be tested directly.

The cog does the talking to Discord; the naming rules and the transcript format
live here.
"""

from __future__ import annotations

import re
from datetime import datetime

KIND_REPORT = "report"
KIND_HELP = "help"

KINDS = {
    KIND_REPORT: {
        "label": "Report a player",
        "emoji": "🚨",
        "prefix": "report",
        "blurb": "Tell us who and what happened. Only you and staff can see this.",
    },
    KIND_HELP: {
        "label": "Help / support",
        "emoji": "❓",
        "prefix": "help",
        "blurb": "Describe what you need. Only you and staff can see this.",
    },
}

CLOSED_PREFIX = "closed"

# Discord's own limit on a channel name.
MAX_CHANNEL_NAME = 100


def kind_label(kind: str) -> str:
    return KINDS.get(kind, {}).get("label", kind)


def kind_emoji(kind: str) -> str:
    return KINDS.get(kind, {}).get("emoji", "🎫")


def channel_name(kind: str, ticket_id: int, closed: bool = False) -> str:
    """`report-0007`, or `closed-0007` once it's been closed.

    Nothing user-supplied goes in here. The ticket subject is free text from a
    form, and putting that in a channel name invites both mojibake and abuse.
    """
    prefix = CLOSED_PREFIX if closed else KINDS.get(kind, {}).get("prefix", "ticket")
    return f"{prefix}-{ticket_id:04d}"[:MAX_CHANNEL_NAME]


def safe_topic(subject: str | None, opener: str, ticket_id: int) -> str:
    """A channel topic that can't smuggle anything odd out of the form."""
    cleaned = re.sub(r"\s+", " ", (subject or "")).strip()
    topic = f"Ticket #{ticket_id:04d} · opened by {opener}"
    if cleaned:
        topic += f" · {cleaned}"
    return topic[:1024]


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------

CONTENT_MISSING_WARNING = (
    "!! NOTE: message text is missing from this transcript.\n"
    "!! The bot's Message Content intent is turned off, so Discord does not\n"
    "!! send it the text of other people's messages. Who spoke and when is\n"
    "!! recorded below, along with attachments.\n"
    "!! To fix for future transcripts: enable Message Content in the Discord\n"
    "!! Developer Portal, then set ENABLE_MESSAGE_CONTENT: 1 in the workflow.\n"
)


def format_timestamp(when: datetime | None) -> str:
    if when is None:
        return "unknown time"
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


def render_transcript(
    ticket,
    messages,
    *,
    have_message_content: bool,
    guild_name: str = "",
) -> str:
    """Turn a ticket's messages into a plain-text transcript.

    Plain text rather than HTML on purpose: it opens anywhere, it diffs, and it
    doesn't need a template engine to produce or a browser to read.

    `messages` are objects with `author`, `created_at`, `content`, `attachments`
    and `embeds` — i.e. discord.Message, but anything shaped like it works.
    """
    lines = [
        "=" * 68,
        f"TICKET #{ticket['id']:04d} — {kind_label(ticket['kind'])}",
        "=" * 68,
        f"Server:     {guild_name}" if guild_name else "",
        f"Opened by:  {ticket['opener_id']}",
        f"Opened at:  {ticket['created_at']}",
        f"Status:     {ticket['status']}",
    ]
    if ticket["subject"]:
        lines.append(f"Subject:    {ticket['subject']}")
    if ticket["about"]:
        lines.append(f"About:      {ticket['about']}")
    if ticket["claimed_by"]:
        lines.append(f"Claimed by: {ticket['claimed_by']}")
    if ticket["closed_at"]:
        lines.append(f"Closed at:  {ticket['closed_at']}")
        lines.append(f"Closed by:  {ticket['closed_by']}")
    if ticket["close_reason"]:
        lines.append(f"Reason:     {ticket['close_reason']}")

    lines.append("")
    if not have_message_content:
        lines.append(CONTENT_MISSING_WARNING)
    lines.append("-" * 68)

    count = 0
    for message in messages:
        count += 1
        author = getattr(message.author, "display_name", None) or str(message.author)
        stamp = format_timestamp(getattr(message, "created_at", None))
        lines.append(f"[{stamp}] {author}:")

        body = (getattr(message, "content", "") or "").strip()
        if body:
            lines.extend(f"    {line}" for line in body.splitlines())
        elif not have_message_content:
            lines.append("    (text unavailable — see note above)")
        else:
            lines.append("    (no text)")

        for attachment in getattr(message, "attachments", ()) or ():
            lines.append(f"    [attachment] {attachment.filename} — {attachment.url}")

        for embed in getattr(message, "embeds", ()) or ():
            title = getattr(embed, "title", None) or "embed"
            lines.append(f"    [embed] {title}")

        lines.append("")

    if count == 0:
        lines.append("(no messages)")

    lines.append("-" * 68)
    lines.append(f"{count} message(s) in this ticket.")
    return "\n".join(line for line in lines if line is not None)


def transcript_filename(ticket_id: int) -> str:
    return f"ticket-{ticket_id:04d}.txt"

"""Everything the user actually looks at: the build card, the live board, update posts."""

from __future__ import annotations

import sqlite3

import discord

import config
import db


# Discord rejects an embed whose title, description, fields and footer add up to
# more than this, regardless of each part being within its own limit.
EMBED_TOTAL_LIMIT = 6000


def _mention(user_id: int | None) -> str:
    return f"<@{user_id}>" if user_id else "—"


def _relative(iso: str | None) -> str:
    ts = db.to_unix(iso)
    return f"<t:{ts}:R>" if ts else "—"


def version_label(build_id: int) -> str | None:
    """'v3' if the build has received three files, None if it has none yet."""
    count = db.schematic_count(build_id)
    return f"v{count}" if count else None


def build_card(build: sqlite3.Row) -> discord.Embed:
    """The message posted in the requests channel — one per build, edited in place."""
    status = build["status"]
    build_id = build["id"]
    version = version_label(build_id)
    latest = db.latest_schematic(build_id)

    if status == db.STATUS_COMPLETE:
        color, headline = config.COLOR_DONE, "✅ Finished"
    elif status == db.STATUS_CLAIMED:
        color, headline = config.COLOR_CLAIMED, "🔨 Being built"
    else:
        color, headline = config.COLOR_OPEN, "🟡 Open — needs a builder"

    embed = discord.Embed(
        title=f"#{build_id} · {build['title']}",
        description=build["description"],
        color=color,
    )
    embed.add_field(name="Status", value=headline, inline=True)
    embed.add_field(
        name="Requested by", value=_mention(build["requested_by"]), inline=True
    )

    if status == db.STATUS_CLAIMED:
        embed.add_field(
            name="Builder",
            value=f"{_mention(build['claimed_by'])} · since {_relative(build['claimed_at'])}",
            inline=False,
        )
    elif status == db.STATUS_OPEN and latest is not None:
        # A handoff: somebody already did work here. Say so loudly, because the
        # whole point is that the next builder continues instead of restarting.
        embed.add_field(
            name="Continue from",
            value=(
                f"**{version}** by {_mention(latest['builder_id'])} "
                f"({_relative(latest['created_at'])})\n"
                f"Get the file with `/schematic {build_id}`"
            ),
            inline=False,
        )
    elif status == db.STATUS_COMPLETE:
        embed.add_field(
            name="Finished", value=_relative(build["completed_at"]), inline=False
        )

    people = db.contributors(build_id)
    if len(people) > 1:
        shown = people[:20]
        chain = " → ".join(_mention(uid) for uid in shown)
        if len(people) > len(shown):
            chain += f" → +{len(people) - len(shown)} more"
        embed.add_field(name="Builders so far", value=chain, inline=False)

    if version:
        footer = f"Latest file: {version}"
        if latest and latest["file_name"]:
            footer += f" · {latest['file_name']}"
    else:
        footer = "No schematic uploaded yet"
    embed.set_footer(text=footer)
    return embed


def update_post(
    build: sqlite3.Row,
    builder: discord.abc.User,
    kind: str,
    note: str | None,
    file_name: str | None,
) -> discord.Embed:
    """Posted into the build's thread each time a builder reports progress."""
    if kind == db.KIND_COMPLETE:
        color, headline = config.COLOR_DONE, "✅ Build finished"
    elif kind == db.KIND_HANDOFF:
        color, headline = config.COLOR_OPEN, "🔁 Handed off — open for the next builder"
    else:
        color, headline = config.COLOR_CLAIMED, "🔨 Progress saved — still being built"

    embed = discord.Embed(
        title=headline,
        description=note or "*(no note)*",
        color=color,
    )
    embed.set_author(name=builder.display_name, icon_url=builder.display_avatar.url)
    embed.add_field(name="Build", value=f"#{build['id']} · {build['title']}", inline=True)

    # Distinguish "this update carried no file" from "this build has never had one",
    # so an update without an attachment doesn't look like the earlier work vanished.
    version = version_label(build["id"])
    if file_name:
        value = f"**{version}** · `{file_name}`" if version else f"`{file_name}`"
    elif version:
        value = f"no new file — latest is still **{version}**"
    else:
        value = "none uploaded yet"
    embed.add_field(name="Schematic", value=value, inline=True)

    if kind == db.KIND_HANDOFF:
        embed.set_footer(text=f"Anyone with the builder role can now claim #{build['id']}")
    return embed


def _board_line(build: sqlite3.Row) -> str:
    build_id = build["id"]
    version = version_label(build_id)
    suffix = f" · ⬇ {version}" if version else ""

    if build["status"] == db.STATUS_CLAIMED:
        return (
            f"`#{build_id}` **{build['title']}** — {_mention(build['claimed_by'])}"
            f" · {_relative(build['claimed_at'])}{suffix}"
        )

    latest = db.latest_schematic(build_id)
    if latest is not None:
        return (
            f"`#{build_id}` **{build['title']}** — continue from {version}"
            f" by {_mention(latest['builder_id'])}"
        )
    return f"`#{build_id}` **{build['title']}** — from {_mention(build['requested_by'])} · new"


def _fit(lines: list[str], limit: int = 1024) -> str:
    """Join as many lines as fit in a Discord field, then say how many were dropped.

    Slicing the joined string would cut through a <@id> mention and render as raw
    text, so drop whole lines instead.
    """
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit - 24:  # leave room for the "+N more" tail
            break
        kept.append(line)
        used += len(line) + 1

    dropped = len(lines) - len(kept)
    if dropped:
        kept.append(f"*…and {dropped} more*")
    return "\n".join(kept) if kept else "—"


def board(guild: discord.Guild, builder_role: discord.Role | None) -> discord.Embed:
    """The single always-current message: who is building what, right now."""
    claimed = db.list_builds(guild.id, db.STATUS_CLAIMED)
    open_builds = db.list_builds(guild.id, db.STATUS_OPEN)
    finished = db.recently_finished(guild.id, config.FINISHED_WINDOW_DAYS)

    embed = discord.Embed(
        title="📋 Build board",
        description="Who is building what, right now.",
        color=config.COLOR_CLAIMED,
        timestamp=discord.utils.utcnow(),
    )

    if claimed:
        embed.add_field(
            name=f"🔨 Being built ({len(claimed)})",
            value=_fit([_board_line(b) for b in claimed]),
            inline=False,
        )
    if open_builds:
        embed.add_field(
            name=f"🟡 Open — needs a builder ({len(open_builds)})",
            value=_fit([_board_line(b) for b in open_builds]),
            inline=False,
        )
    if not claimed and not open_builds:
        embed.add_field(
            name="Nothing queued",
            value="Script writers: use `/request` to describe a build that needs making.",
            inline=False,
        )

    if finished:
        embed.add_field(
            name=f"✅ Finished in the last {config.FINISHED_WINDOW_DAYS} days ({len(finished)})",
            value=_fit([f"`#{b['id']}` {b['title']}" for b in finished]),
            inline=False,
        )

    if builder_role is not None:
        busy = db.busy_builder_ids(guild.id)
        free = [m for m in builder_role.members if m.id not in busy]
        if free:
            shown = free[:20]
            value = " ".join(m.mention for m in shown)
            if len(free) > len(shown):
                value += f" *+{len(free) - len(shown)} more*"
        else:
            value = "*everyone with the builder role is on something*"
        embed.add_field(name="👷 Free builders", value=value[:1024], inline=False)

    embed.set_footer(text="Updated")
    return embed


# --------------------------------------------------------------------------
# joining and leaving
# --------------------------------------------------------------------------

def ordinal(n: int) -> str:
    """1 -> 1st, 2 -> 2nd, 11 -> 11th, 21 -> 21st."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n:,}{suffix}"


def welcome_card(
    member: discord.Member, applications_channel_id: int | None
) -> discord.Embed:
    """The post a new member gets, laid out like the bot this replaces."""
    guild = member.guild

    lines = [f"Welcome {member.mention} to {guild.name}!"]
    if applications_channel_id:
        lines.append(f"Please make an application in <#{applications_channel_id}>")

    embed = discord.Embed(description="\n".join(lines), color=config.COLOR_CLAIMED)

    # Server icon beside the title, matching the original's layout. A server
    # without an icon still gets the title, just without the picture.
    icon = guild.icon.url if guild.icon else None
    embed.set_author(name=f"Welcome to {guild.name}"[:256], icon_url=icon)
    embed.set_thumbnail(url=member.display_avatar.url)

    if guild.member_count:
        embed.set_footer(text=f"You're our {ordinal(guild.member_count)} member")
    return embed


def membership_length(joined_at) -> str | None:
    """How long someone was in the server, coarsely: "3 days", "2 months", "1 year".

    Deliberately not the h/m formatter used for uptime in cogs/health.py — that
    tops out at hours, which is useless for someone who was here eight months.
    Returns None when Discord didn't tell us when they joined.
    """
    if joined_at is None:
        return None

    seconds = (discord.utils.utcnow() - joined_at).total_seconds()
    if seconds < 3600:
        return "less than an hour"

    def plural(value: int, unit: str) -> str:
        return f"{value} {unit}" if value == 1 else f"{value} {unit}s"

    hours = int(seconds // 3600)
    if hours < 24:
        return plural(hours, "hour")
    days = hours // 24
    if days < 31:
        return plural(days, "day")
    if days < 365:
        return plural(days // 30, "month")
    return plural(days // 365, "year")


def goodbye_card(member: discord.abc.User, guild: discord.Guild) -> discord.Embed:
    """Laid out like the welcome, so joins and leaves look like one pair."""
    lines = [f"**{member.display_name}** left the server."]

    # Typed as User, which has no joined_at at all — and even on a Member it can
    # be None when they weren't cached. Either way, just leave the line out.
    stayed = membership_length(getattr(member, "joined_at", None))
    if stayed:
        lines.append(f"They were here for {stayed}.")

    embed = discord.Embed(description="\n".join(lines), color=config.COLOR_GOODBYE)

    icon = guild.icon.url if guild.icon else None
    embed.set_author(name=f"Goodbye from {guild.name}"[:256], icon_url=icon)
    embed.set_thumbnail(url=member.display_avatar.url)

    if guild.member_count:
        # Not "N members left" — that reads as "N members departed".
        embed.set_footer(text=f"Now {guild.member_count:,} members")
    return embed


# --------------------------------------------------------------------------
# security alerts
# --------------------------------------------------------------------------

def security_alert(
    decision,
    subject: discord.abc.User | None,
    guild: discord.Guild,
    outcome: str,
) -> discord.Embed:
    """One alert per trip. Says what happened, to whom, and what was done."""
    import security  # local: embeds is imported by security's caller, not by it

    watching = decision.watch_only
    title = "🔍 Watch mode — no action taken" if watching else "🛡️ Protection triggered"

    embed = discord.Embed(
        title=title,
        description=f"**{decision.label}**\n{decision.detail}",
        color=config.COLOR_ALERT if watching else config.COLOR_ERROR,
        timestamp=discord.utils.utcnow(),
    )

    if subject is not None:
        embed.add_field(
            name="Member",
            value=f"{subject.mention}\n`{subject}` · `{subject.id}`",
            inline=True,
        )
        age = security.account_age_days(getattr(subject, "created_at", None))
        if age is not None:
            embed.add_field(
                name="Account age",
                value=f"{int(age)} days" if age >= 1 else f"{int(age * 24)} hours",
                inline=True,
            )
        embed.set_thumbnail(url=subject.display_avatar.url)

    if watching:
        embed.add_field(
            name="Would have done",
            value=f"**{decision.intended}**\nNothing was actually done.",
            inline=False,
        )
        embed.set_footer(text="Turn this on for real with /security-mode live")
    else:
        embed.add_field(name="Action taken", value=outcome, inline=False)

    return embed


def trap_armed(member: discord.abc.User, channel_id: int, action: str) -> discord.Embed:
    """Posted when staff trip the honeypot and are (correctly) ignored.

    Without this, testing your own trap produces nothing at all, which looks
    identical to the bot not listening. This is the proof that it is.
    """
    embed = discord.Embed(
        title="🛡️ Trap is armed",
        description=(
            f"{member.mention} posted in <#{channel_id}> and was **ignored** — "
            f"staff are always exempt.\n\n"
            f"A normal member doing that would have been **{action}**."
        ),
        color=config.COLOR_CLAIMED,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="This confirms the honeypot is working. Test with an alt for the real thing.")
    return embed


# --------------------------------------------------------------------------
# tickets
# --------------------------------------------------------------------------

def ticket_panel(guild: discord.Guild, support_role_id: int | None) -> discord.Embed:
    """The always-there message people press to open a ticket."""
    import tickets

    who = f"<@&{support_role_id}>" if support_role_id else "staff"
    lines = [
        f"Pick the option that fits. A private channel opens that only you and {who} can see.",
        "",
    ]
    for kind, info in tickets.KINDS.items():
        lines.append(f"{info['emoji']} **{info['label']}** — {info['blurb']}")

    embed = discord.Embed(
        title="🎫 Need help?",
        description="\n".join(lines),
        color=config.COLOR_CLAIMED,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="One open ticket at a time. Abuse of this will be treated as spam.")
    return embed


def ticket_header(ticket, opener: discord.abc.User, support_role_id: int | None) -> discord.Embed:
    """Posted at the top of a freshly opened ticket."""
    import tickets

    embed = discord.Embed(
        title=f"{tickets.kind_emoji(ticket['kind'])} Ticket #{ticket['id']:04d} · "
              f"{tickets.kind_label(ticket['kind'])}",
        color=config.COLOR_CLAIMED,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Opened by", value=opener.mention, inline=True)
    if ticket["claimed_by"]:
        embed.add_field(name="Handled by", value=f"<@{ticket['claimed_by']}>", inline=True)

    if ticket["about"]:
        embed.add_field(name="About", value=ticket["about"][:1024], inline=False)
    if ticket["subject"]:
        embed.add_field(name="Details", value=ticket["subject"][:1024], inline=False)

    embed.set_footer(text="Staff: claim it so nobody doubles up, then close when it's done.")
    return embed


def ticket_closed(ticket, closed_by: discord.abc.User) -> discord.Embed:
    """Replaces the header once closed. The channel is kept, not deleted."""
    embed = discord.Embed(
        title=f"🔒 Ticket #{ticket['id']:04d} closed",
        description=(
            f"Closed by {closed_by.mention}.\n"
            f"{ticket['close_reason'] or '*no reason given*'}"
        ),
        color=config.COLOR_GOODBYE,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(
        text="The channel is kept so nothing is lost. Take a transcript before deleting it."
    )
    return embed


def ticket_log(ticket, actor: discord.abc.User, event: str) -> discord.Embed:
    import tickets

    colour = {
        "opened": config.COLOR_CLAIMED,
        "claimed": config.COLOR_OPEN,
        "closed": config.COLOR_GOODBYE,
        "reopened": config.COLOR_OPEN,
        "deleted": config.COLOR_ERROR,
    }.get(event, config.COLOR_CLAIMED)

    embed = discord.Embed(
        title=f"Ticket #{ticket['id']:04d} {event}",
        description=(
            f"{tickets.kind_emoji(ticket['kind'])} {tickets.kind_label(ticket['kind'])}\n"
            f"Opened by <@{ticket['opener_id']}> · {event} by {actor.mention}"
        ),
        color=colour,
        timestamp=discord.utils.utcnow(),
    )
    if ticket["close_reason"] and event == "closed":
        embed.add_field(name="Reason", value=ticket["close_reason"][:1024], inline=False)
    return embed


def upload_announcement(video, creator_name: str) -> discord.Embed:
    """Posted when a watched channel uploads."""
    embed = discord.Embed(
        title=video.title[:256],
        url=video.watch_url,
        description=f"**{creator_name}** just uploaded.",
        color=discord.Color.from_str("#ff0000"),  # YouTube red
        timestamp=video.published or discord.utils.utcnow(),
    )
    embed.set_author(name=f"🔴 New video from {creator_name}"[:256])
    if video.thumbnail:
        embed.set_image(url=video.thumbnail)
    embed.set_footer(text="YouTube")
    return embed


# --------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------

def application_panel(guild: discord.Guild, forms) -> discord.Embed:
    """The always-there message people press to apply."""
    lines = []
    for form in forms:
        emoji = form["emoji"] or "📄"
        state = "" if form["is_open"] else "  *(closed)*"
        blurb = form["blurb"] or ""
        lines.append(f"{emoji} **{form['label']}**{state}\n{blurb}".rstrip())

    if not lines:
        lines = ["*No positions are open yet.*"]

    embed = discord.Embed(
        title="📥 Applications",
        description=(
            "Pick what you'd like to apply for. You'll get a short form to fill in, "
            "and a DM when staff have decided.\n\n" + "\n\n".join(lines)
        )[:4096],
        color=config.COLOR_CLAIMED,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(
        text="Make sure your DMs from server members are on, or you won't get the answer."
    )
    return embed


def application_card(application, applicant: discord.abc.User | None) -> discord.Embed:
    """What staff review: the answers, who wrote them, and how old the account is."""
    import applications as apply_lib

    status = application["status"]
    emoji, wording = apply_lib.status_face(status)
    colour = {
        apply_lib.STATUS_ACCEPTED: config.COLOR_DONE,
        apply_lib.STATUS_DENIED: config.COLOR_ERROR,
        apply_lib.STATUS_WITHDRAWN: config.COLOR_GOODBYE,
    }.get(status, config.COLOR_OPEN)

    embed = discord.Embed(
        title=f"{emoji} {application['form_label'] or 'Application'} · #{application['id']:04d}",
        color=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Applicant", value=_mention(application["applicant_id"]), inline=True)
    embed.add_field(name="Status", value=wording, inline=True)

    if applicant is not None:
        embed.set_thumbnail(url=applicant.display_avatar.url)
        created = getattr(applicant, "created_at", None)
        if created is not None:
            embed.add_field(
                name="Account made", value=f"<t:{int(created.timestamp())}:R>", inline=True
            )

    # Every field is individually capped at 1024, but Discord *also* rejects an
    # embed whose parts add up to more than 6000 — and five long answers plus a
    # rejection note clears that easily. So the answers are packed into whatever
    # room is left once the decision note has been set aside.
    decided_note = ""
    if application["decided_by"]:
        decided_note = f"{_mention(application['decided_by'])} · {_relative(application['decided_at'])}"
        if application["decision_note"]:
            decided_note += f"\n> {application['decision_note'][:900]}"

    reserved = len(decided_note) + len("Decided by") + 40  # 40: footer and slack
    answers = apply_lib.answers_from_json(application["answers"])
    left_out = 0

    for index, (question, answer) in enumerate(answers):
        name = (question[:100] or "—")
        room = min(1024, EMBED_TOTAL_LIMIT - len(embed) - len(name) - reserved)
        # Below this there isn't enough of an answer left to be worth showing.
        if room < 64:
            left_out = len(answers) - index
            break
        embed.add_field(name=name, value=(answer[:room] or "*(blank)*"), inline=False)

    if left_out:
        embed.add_field(
            name="…",
            value=f"*{left_out} more answer(s) were too long to show here.*",
            inline=False,
        )

    if decided_note:
        embed.add_field(name="Decided by", value=decided_note, inline=False)
    else:
        embed.set_footer(text="Accept or deny below — the applicant gets a DM either way.")
    return embed


def application_decision_dm(
    application, guild: discord.Guild, accepted: bool, role: discord.Role | None
) -> discord.Embed:
    """The message the applicant actually receives. This is the whole point of
    the feature, so it says what happens next rather than just yes or no."""
    position = application["form_label"] or "your application"

    if accepted:
        lines = [f"Your **{position}** application for **{guild.name}** was **accepted**. 🎉"]
        if role is not None:
            lines.append(f"You've been given the **{role.name}** role — go have a look.")
    else:
        lines = [
            f"Your **{position}** application for **{guild.name}** wasn't accepted this time."
        ]

    if application["decision_note"]:
        lines.append(f"\n**From staff:**\n> {application['decision_note'][:1500]}")

    embed = discord.Embed(
        title="✅ Application accepted" if accepted else "Application decision",
        description="\n".join(lines),
        color=config.COLOR_DONE if accepted else config.COLOR_GOODBYE,
        timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if not accepted:
        embed.set_footer(text="You're welcome to apply again later.")
    return embed


def application_list(guild: discord.Guild, rows, heading: str) -> discord.Embed:
    import applications as apply_lib

    lines = []
    for row in rows:
        emoji, _ = apply_lib.status_face(row["status"])
        answers = apply_lib.answers_from_json(row["answers"])
        lines.append(
            f"{emoji} `#{row['id']:04d}` **{row['form_label'] or row['form_key']}** — "
            f"{_mention(row['applicant_id'])} · {apply_lib.summarise(answers, 60)}"
        )

    embed = discord.Embed(
        title=f"📥 {heading}",
        description=_fit(lines, 4000) if lines else "*Nothing here.*",
        color=config.COLOR_CLAIMED,
    )
    embed.set_footer(text="Open one with /application <number>")
    return embed


def error(message: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {message}", color=config.COLOR_ERROR)


def success(message: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {message}", color=config.COLOR_DONE)


def notice(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=config.COLOR_OPEN)

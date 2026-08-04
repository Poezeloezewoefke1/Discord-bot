"""Everything the user actually looks at: the build card, the live board, update posts."""

from __future__ import annotations

import sqlite3

import discord

import config
import db


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

    version = version_label(build["id"])
    if file_name and version:
        embed.add_field(name="Schematic", value=f"**{version}** · `{file_name}`", inline=True)
    else:
        embed.add_field(name="Schematic", value="none attached", inline=True)

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


def error(message: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {message}", color=config.COLOR_ERROR)


def success(message: str) -> discord.Embed:
    return discord.Embed(description=f"✅ {message}", color=config.COLOR_DONE)


def notice(message: str) -> discord.Embed:
    return discord.Embed(description=message, color=config.COLOR_OPEN)

"""Shared behaviour used by both the buttons and the slash commands.

Keeping claim/release/refresh here means a build's state transitions work
identically whether someone clicked a button or typed a command.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sqlite3

import discord

import config
import db
import embeds

log = logging.getLogger(__name__)

# One pending refresh task per guild. See schedule_board_refresh().
_pending_refresh: dict[int, asyncio.Task] = {}


# --------------------------------------------------------------------------
# configuration lookups
# --------------------------------------------------------------------------

def require_config(guild_id: int) -> sqlite3.Row:
    cfg = db.get_config(guild_id)
    if cfg is None:
        raise config.ConfigError(
            "This server isn't set up yet. An admin needs to run `/setup` first."
        )
    return cfg


def builder_role(guild: discord.Guild) -> discord.Role | None:
    cfg = db.get_config(guild.id)
    if cfg is None or cfg["builder_role_id"] is None:
        return None
    return guild.get_role(cfg["builder_role_id"])


def check_builder(member: discord.Member) -> None:
    """Raise ConfigError with a user-facing message unless the member may build."""
    cfg = db.get_config(member.guild.id)
    role_id = cfg["builder_role_id"] if cfg else None
    if not config.has_role(member, role_id):
        if role_id is None:
            raise config.ConfigError(
                "No builder role is configured yet. An admin needs to run `/setup`."
            )
        raise config.ConfigError(f"Only <@&{role_id}> can do that.")


def check_scripter(member: discord.Member) -> None:
    """Raise ConfigError with a user-facing message unless the member may post requests."""
    cfg = db.get_config(member.guild.id)
    role_id = cfg["scripter_role_id"] if cfg else None
    if not config.has_role(member, role_id):
        if role_id is None:
            raise config.ConfigError(
                "No script writer role is configured yet. An admin needs to run `/setup`."
            )
        raise config.ConfigError(f"Only <@&{role_id}> can post build requests.")


# --------------------------------------------------------------------------
# posting and refreshing the build card
# --------------------------------------------------------------------------

async def post_build_card(
    bot: discord.Client, guild: discord.Guild, build_id: int
) -> discord.Message | None:
    """Post a new build's card in the requests channel and open its discussion thread."""
    from views import build_view  # local import: views imports this module back

    cfg = require_config(guild.id)
    channel = guild.get_channel(cfg["requests_channel_id"]) if cfg["requests_channel_id"] else None
    if not isinstance(channel, discord.TextChannel):
        raise config.ConfigError(
            "The requests channel is missing or isn't a text channel. Re-run `/setup`."
        )

    build = db.get_build(build_id)
    message = await channel.send(embed=embeds.build_card(build), view=build_view(build))

    thread_id = None
    try:
        thread = await message.create_thread(
            name=f"#{build_id} {build['title']}"[:100],
            auto_archive_duration=10080,  # 7 days
        )
        thread_id = thread.id
    except discord.HTTPException:
        # Threads are a nice-to-have; the card still works without one.
        log.warning("could not create thread for build #%s", build_id, exc_info=True)

    db.attach_message(build_id, message.id, thread_id)
    return message


async def refresh_card(bot: discord.Client, build_id: int) -> None:
    """Re-render a build's card in place so it always shows current state."""
    from views import build_view

    build = db.get_build(build_id)
    if build is None or build["message_id"] is None:
        return

    cfg = db.get_config(build["guild_id"])
    if cfg is None or cfg["requests_channel_id"] is None:
        return

    channel = bot.get_channel(cfg["requests_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        message = await channel.fetch_message(build["message_id"])
        await message.edit(embed=embeds.build_card(build), view=build_view(build))
    except discord.NotFound:
        log.warning("build card for #%s no longer exists", build_id)
    except discord.HTTPException:
        log.warning("failed to refresh card for build #%s", build_id, exc_info=True)


async def post_to_thread(bot: discord.Client, build_id: int, **send_kwargs) -> None:
    """Send a message into a build's discussion thread, if it still has one."""
    build = db.get_build(build_id)
    if build is None or build["thread_id"] is None:
        return
    thread = bot.get_channel(build["thread_id"])
    if thread is None:
        try:
            thread = await bot.fetch_channel(build["thread_id"])
        except discord.HTTPException:
            return
    if not isinstance(thread, discord.Thread):
        return
    try:
        if thread.archived:
            await thread.edit(archived=False)
        await thread.send(**send_kwargs)
    except discord.HTTPException:
        log.warning("failed to post in thread for build #%s", build_id, exc_info=True)


async def archive_thread(bot: discord.Client, build_id: int) -> None:
    """Tidy a finished build's thread away so the channel list stays useful."""
    build = db.get_build(build_id)
    if build is None or build["thread_id"] is None:
        return
    thread = bot.get_channel(build["thread_id"])
    if not isinstance(thread, discord.Thread):
        return
    try:
        await thread.edit(archived=True)
    except discord.HTTPException:
        log.warning("failed to archive thread for build #%s", build_id, exc_info=True)


# --------------------------------------------------------------------------
# the live board
# --------------------------------------------------------------------------

async def refresh_board_now(bot: discord.Client, guild: discord.Guild) -> None:
    cfg = db.get_config(guild.id)
    if cfg is None or cfg["board_channel_id"] is None:
        return

    channel = guild.get_channel(cfg["board_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    embed = embeds.board(guild, builder_role(guild))

    if cfg["board_message_id"]:
        try:
            message = await channel.fetch_message(cfg["board_message_id"])
            await message.edit(embed=embed)
            return
        except discord.NotFound:
            pass  # someone deleted it — fall through and post a fresh one
        except discord.HTTPException:
            log.warning("failed to edit board in guild %s", guild.id, exc_info=True)
            return

    try:
        message = await channel.send(embed=embed)
        db.save_config(guild.id, board_message_id=message.id)
    except discord.HTTPException:
        log.warning("failed to post board in guild %s", guild.id, exc_info=True)


def schedule_board_refresh(bot: discord.Client, guild: discord.Guild) -> None:
    """Queue a board refresh, coalescing bursts into a single edit.

    A rush of claims would otherwise mean one message edit each and a fast trip
    into Discord's rate limit. Because the refresh reads current state when it
    finally runs, collapsing several requests into one loses nothing.
    """
    task = _pending_refresh.get(guild.id)
    if task is not None and not task.done():
        return

    async def _run() -> None:
        try:
            await asyncio.sleep(config.BOARD_REFRESH_INTERVAL)
            await refresh_board_now(bot, guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("board refresh failed for guild %s", guild.id)

    _pending_refresh[guild.id] = asyncio.create_task(_run())


# --------------------------------------------------------------------------
# state transitions
# --------------------------------------------------------------------------

async def do_claim(interaction: discord.Interaction, build_id: int) -> None:
    """Take an open build. Refuses if someone already holds it."""
    build = db.get_build(build_id)
    if build is None:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} doesn't exist."), ephemeral=True
        )
        return

    check_builder(interaction.user)

    if build["status"] == db.STATUS_COMPLETE:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} is already finished."), ephemeral=True
        )
        return

    if not db.claim_build(build_id, interaction.user.id):
        # Lost the race, or it was never open. Say exactly who has it.
        current = db.get_build(build_id)
        holder = current["claimed_by"]
        if holder == interaction.user.id:
            msg = f"You already have build #{build_id}."
        else:
            since = db.to_unix(current["claimed_at"])
            when = f" (since <t:{since}:R>)" if since else ""
            msg = f"<@{holder}> is already building #{build_id}{when} — pick another one."
        await interaction.response.send_message(embed=embeds.error(msg), ephemeral=True)
        return

    build = db.get_build(build_id)
    await interaction.response.send_message(
        embed=embeds.success(
            f"You're building **#{build_id} {build['title']}**.\n"
            f"Post your work with `/update build:{build_id}`."
        ),
        ephemeral=True,
    )

    await refresh_card(interaction.client, build_id)
    schedule_board_refresh(interaction.client, interaction.guild)

    latest = db.latest_schematic(build_id)
    if latest is not None:
        note = (
            f"{interaction.user.mention} picked this up — "
            f"continuing from **{embeds.version_label(build_id)}** by <@{latest['builder_id']}>. "
            f"Grab it with `/schematic {build_id}`."
        )
    else:
        note = (
            f"{interaction.user.mention} is building this. "
            f"<@{build['requested_by']}> — any details to add?"
        )
    await post_to_thread(interaction.client, build_id, content=note)


async def store_attachment(build_id: int, attachment: discord.Attachment) -> tuple[str, str]:
    """Save an uploaded schematic next to the build. Returns (stored path, original name).

    Keeping our own copy matters: Discord's CDN links now carry an expiry, so the
    attachment URL on a months-old handoff will eventually stop resolving. The
    local file is what makes /schematic still work later.
    """
    if not config.is_allowed_file(attachment.filename):
        raise config.ConfigError(
            f"`{attachment.filename}` isn't a schematic. "
            f"Accepted files: {config.allowed_extensions_text()}"
        )
    if attachment.size > config.MAX_FILE_BYTES:
        raise config.ConfigError(
            f"That file is {attachment.size / 1_048_576:.1f} MB — the limit is "
            f"{config.MAX_FILE_BYTES // 1_048_576} MB."
        )

    folder = config.build_storage_dir(build_id)
    folder.mkdir(parents=True, exist_ok=True)

    # Prefix with the version number so the handoff history stays readable on disk
    # and re-uploading the same filename never overwrites an earlier version.
    version = db.schematic_count(build_id) + 1
    safe_name = discord.utils.escape_markdown(attachment.filename).replace("/", "_")
    path = folder / f"v{version}_{safe_name}"
    await attachment.save(path)
    return str(path), attachment.filename


async def send_latest_schematic(interaction: discord.Interaction, build_id: int) -> None:
    """Re-send the newest stored file for a build, straight from disk."""
    build = db.get_build(build_id)
    if build is None:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} doesn't exist."), ephemeral=True
        )
        return

    latest = db.latest_schematic(build_id)
    if latest is None:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} has no schematic uploaded yet."),
            ephemeral=True,
        )
        return

    path = pathlib.Path(latest["file_path"])
    if not path.exists():
        await interaction.response.send_message(
            embed=embeds.error(
                f"The stored file for #{build_id} is missing from disk. "
                f"Ask <@{latest['builder_id']}> to upload it again."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=embeds.notice(
            f"**{embeds.version_label(build_id)}** of **#{build_id} {build['title']}** — "
            f"uploaded by <@{latest['builder_id']}> {_relative(latest['created_at'])}"
        ),
        file=discord.File(path, filename=latest["file_name"]),
        ephemeral=True,
    )


def _relative(iso: str | None) -> str:
    ts = db.to_unix(iso)
    return f"<t:{ts}:R>" if ts else ""


async def do_release(interaction: discord.Interaction, build_id: int) -> None:
    """Give a build back without uploading anything."""
    build = db.get_build(build_id)
    if build is None:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} doesn't exist."), ephemeral=True
        )
        return

    if build["status"] != db.STATUS_CLAIMED:
        await interaction.response.send_message(
            embed=embeds.error(f"Build #{build_id} isn't claimed by anyone."), ephemeral=True
        )
        return

    is_holder = build["claimed_by"] == interaction.user.id
    if not is_holder and not config.is_admin(interaction.user):
        await interaction.response.send_message(
            embed=embeds.error(
                f"<@{build['claimed_by']}> has this one — only they or an admin can release it."
            ),
            ephemeral=True,
        )
        return

    db.release_build(build_id, interaction.user.id if is_holder else None)

    await interaction.response.send_message(
        embed=embeds.success(f"Build #{build_id} is open again."), ephemeral=True
    )
    await refresh_card(interaction.client, build_id)
    schedule_board_refresh(interaction.client, interaction.guild)
    await post_to_thread(
        interaction.client,
        build_id,
        content=(
            f"🟡 {interaction.user.mention} released this build — "
            f"it's open for anyone with the builder role."
        ),
    )

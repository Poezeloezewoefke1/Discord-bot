"""Announcing new YouTube uploads, replacing a separate notification bot.

Polls a public Atom feed per channel. Three things this has to get right, all of
which are the difference between working and appearing to work:

  * the first check for a channel announces nothing, or setup dumps the last
    fifteen videos into the server with an @everyone on each
  * seen videos are remembered in the database, so the ~6-hourly restart resumes
    instead of announcing everything again
  * a failure inside the loop is caught, because an unhandled exception stops a
    tasks.loop silently for the rest of the shift
"""

from __future__ import annotations

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import embeds
import notify

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = "DiscordBot (upload notifier)"


class NotifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
        return self.session

    async def fetch(self, url: str) -> str | None:
        try:
            session = await self.http()
            async with session.get(url) as response:
                if response.status != 200:
                    log.warning("fetch %s returned HTTP %s", url, response.status)
                    return None
                return await response.text()
        except Exception:  # noqa: BLE001 — never let a fetch kill the loop
            log.warning("fetch failed for %s", url, exc_info=True)
            return None

    # ----------------------------------------------------------------------
    # polling
    # ----------------------------------------------------------------------

    async def resolve_channel_id(self, handle: str) -> str | None:
        """Handle -> UC… id, cached in the database after the first success."""
        feed = db.get_feed(handle)
        if feed is not None and feed["channel_id"]:
            return feed["channel_id"]

        page = await self.fetch(notify.channel_page_url(handle))
        channel_id = notify.extract_channel_id(page or "")
        if channel_id is None:
            db.save_feed(handle, last_error="could not resolve the channel id from the page")
            return None

        db.save_feed(
            handle,
            channel_id=channel_id,
            title=notify.extract_channel_title(page or "") or handle,
            last_error=None,
        )
        log.info("resolved %s -> %s", handle, channel_id)
        return channel_id

    async def fetch_videos(self, channel_id: str) -> tuple[list, str | None]:
        """Try each feed source until one returns videos.

        The plain channel feed omits Shorts, so a Shorts-only channel reads as
        completely empty through it. The uploads playlist covers both, so it is
        tried first and the channel feed is the fallback.
        """
        for name, url in notify.feed_candidates(channel_id):
            xml = await self.fetch(url)
            if xml is None:
                continue
            videos = notify.parse_feed(xml)
            if videos:
                return videos, name
        return [], None

    async def check_creator(self, creator: dict) -> list:
        """Poll one channel. Returns the videos that should be announced now."""
        handle = creator["handle"]
        channel_id = await self.resolve_channel_id(handle)
        if channel_id is None:
            return []

        videos, source = await self.fetch_videos(channel_id)
        if not videos:
            db.save_feed(
                handle,
                last_error=(
                    "no videos in any feed — if this channel only has Shorts, "
                    "YouTube may not be publishing them yet"
                ),
                last_checked=db.now_iso(),
            )
            return []

        feed = db.get_feed(handle)
        first_time = feed is None or not feed["initialised"]

        if first_time:
            # Record the whole backlog as seen and announce none of it.
            for video in videos:
                db.mark_announced(video.video_id, handle)
            db.save_feed(
                handle,
                initialised=1,
                last_video_id=videos[0].video_id,
                last_checked=db.now_iso(),
                last_error=None,
                feed_kind=source,
            )
            log.info("%s: first check via %s, marked %d existing videos as seen",
                     handle, source, len(videos))
            return []

        fresh = []
        for video in reversed(videos):  # oldest first, so a burst posts in order
            if db.mark_announced(video.video_id, handle):
                fresh.append(video)

        db.save_feed(
            handle,
            last_video_id=videos[0].video_id,
            last_checked=db.now_iso(),
            last_error=None,
            feed_kind=source,
        )
        db.prune_announced(handle)
        return fresh

    async def announce(self, video, creator: dict) -> int:
        """Post one video to every guild that has a notify channel. Returns posts made."""
        posted = 0
        for cfg in db.guilds_with_notify_channel():
            guild = self.bot.get_guild(cfg["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(cfg["notify_channel_id"])
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(
                    content="@everyone",
                    embed=embeds.upload_announcement(video, creator["name"]),
                    # discord.py suppresses @everyone by default, which would give
                    # an announcement that looks right and pings nobody.
                    allowed_mentions=discord.AllowedMentions(everyone=True),
                )
                posted += 1
            except discord.HTTPException:
                log.warning("could not announce in guild %s", guild.id, exc_info=True)
        return posted

    @tasks.loop(minutes=notify.POLL_MINUTES)
    async def poll(self) -> None:
        if not db.guilds_with_notify_channel():
            return  # nowhere to post yet; don't burn requests
        for creator in notify.CREATORS:
            try:
                for video in await self.check_creator(creator):
                    await self.announce(video, creator)
                    log.info("announced %s from %s", video.video_id, creator["handle"])
            except Exception:  # noqa: BLE001
                # One bad channel must not stop the other, nor end the loop.
                log.exception("poll failed for %s", creator["handle"])
                db.save_feed(creator["handle"], last_error="poll raised — see logs")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @poll.error
    async def poll_error(self, error: BaseException) -> None:
        """Restart the loop rather than leaving it dead for the rest of the shift."""
        log.exception("upload poll loop died, restarting", exc_info=error)
        if not self.poll.is_running():
            self.poll.restart()

    # ----------------------------------------------------------------------
    # commands
    # ----------------------------------------------------------------------

    @app_commands.command(
        name="notify-setup", description="Choose where new YouTube uploads are announced"
    )
    @app_commands.describe(channel="Channel the announcements go in")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def notify_setup(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        perms = channel.permissions_for(interaction.guild.me)
        missing = [
            label
            for name, label in (
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
            )
            if not getattr(perms, name)
        ]
        if missing:
            await interaction.followup.send(
                embed=embeds.error(
                    f"I can't post in {channel.mention} — I need {', '.join(missing)}."
                ),
                ephemeral=True,
            )
            return

        db.save_config(interaction.guild.id, notify_channel_id=channel.id)

        warning = ""
        if not perms.mention_everyone:
            warning = (
                "\n\n⚠️ I don't have **Mention Everyone** in that channel, so announcements "
                "will post but **won't actually ping anyone**. Grant it in the channel's "
                "permissions."
            )

        watching = "\n".join(
            f"• **{c['name']}** — <https://www.youtube.com/{c['handle']}>"
            for c in notify.CREATORS
        )
        await interaction.followup.send(
            embed=embeds.success(
                f"**Upload announcements will go to {channel.mention}.**\n\n"
                f"Watching:\n{watching}\n\n"
                f"Checked every {notify.POLL_MINUTES} minutes. Existing videos won't be "
                f"announced — only uploads from now on.{warning}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="notify-test", description="Check the upload watcher is working")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def notify_test(self, interaction: discord.Interaction) -> None:
        cfg = db.get_config(interaction.guild.id)
        lines = []

        if cfg is None or not cfg["notify_channel_id"]:
            lines.append("❌ No announcement channel set — run `/notify-setup`.")
        else:
            channel = interaction.guild.get_channel(cfg["notify_channel_id"])
            if channel is None:
                lines.append("❌ The announcement channel was deleted — re-run `/notify-setup`.")
            else:
                lines.append(f"✅ Announcing in {channel.mention}")
                if not channel.permissions_for(interaction.guild.me).mention_everyone:
                    lines.append("⚠️ No **Mention Everyone** there — posts won't ping.")

        lines.append(f"🔁 Checked every {notify.POLL_MINUTES} minutes · "
                     f"loop {'running' if self.poll.is_running() else '**stopped**'}")
        lines.append("")

        for creator in notify.CREATORS:
            feed = db.get_feed(creator["handle"])
            if feed is None or not feed["channel_id"]:
                lines.append(
                    f"⏳ **{creator['name']}** — not resolved yet "
                    f"(happens on the first check)"
                )
                if feed is not None and feed["last_error"]:
                    lines.append(f"    ❌ {feed['last_error']}")
                continue

            ts = db.to_unix(feed["last_checked"])
            when = f"<t:{ts}:R>" if ts else "never"
            state = "❌" if feed["last_error"] else "✅"
            lines.append(f"{state} **{creator['name']}** · `{feed['channel_id']}` · checked {when}")
            if feed["last_error"]:
                lines.append(f"    {feed['last_error']}")
            source = feed["feed_kind"] or "not determined yet"
            lines.append(
                f"    via *{source}* · {db.count_announced(creator['handle'])} videos known"
            )

        await interaction.response.send_message(
            embed=embeds.notice("\n".join(lines)), ephemeral=True
        )

    @app_commands.command(
        name="notify-latest", description="Announce a channel's newest video right now"
    )
    @app_commands.describe(creator="Which channel's latest video to post")
    @app_commands.choices(
        creator=[
            app_commands.Choice(name=c["name"], value=c["handle"]) for c in notify.CREATORS
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def notify_latest(
        self, interaction: discord.Interaction, creator: app_commands.Choice[str]
    ) -> None:
        """Escape hatch for a video that was already recorded as seen.

        The first check for a channel records its whole backlog silently, so a
        video uploaded just before the bot started watching never announces.
        This posts it on demand rather than leaving it stuck.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        entry = next((c for c in notify.CREATORS if c["handle"] == creator.value), None)
        if entry is None:
            await interaction.followup.send(
                embed=embeds.error("Unknown channel."), ephemeral=True
            )
            return

        channel_id = await self.resolve_channel_id(entry["handle"])
        if channel_id is None:
            await interaction.followup.send(
                embed=embeds.error(
                    f"Couldn't resolve **{entry['name']}** — YouTube didn't return a channel id."
                ),
                ephemeral=True,
            )
            return

        videos, source = await self.fetch_videos(channel_id)
        if not videos:
            await interaction.followup.send(
                embed=embeds.error(
                    f"**{entry['name']}** has no videos in either feed.\n"
                    "If the channel has only Shorts, YouTube may not have published them to "
                    "its feeds yet — that can take a while after upload."
                ),
                ephemeral=True,
            )
            return

        newest = videos[0]
        db.mark_announced(newest.video_id, entry["handle"])
        db.save_feed(entry["handle"], initialised=1, feed_kind=source, last_error=None)
        posted = await self.announce(newest, entry)

        await interaction.followup.send(
            embed=embeds.success(
                f"Posted **{newest.title}** in {posted} channel(s).\n"
                f"Found via the *{source}*."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="notify-check", description="Check for new uploads right now")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def notify_check(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        found = 0
        problems = []
        for creator in notify.CREATORS:
            try:
                for video in await self.check_creator(creator):
                    await self.announce(video, creator)
                    found += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("manual check failed for %s", creator["handle"])
                problems.append(f"{creator['name']}: `{type(exc).__name__}`")

        message = f"Checked both channels. **{found}** new video(s) announced."
        if problems:
            message += "\n\n❌ " + "\n❌ ".join(problems)
        await interaction.followup.send(embed=embeds.success(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NotifyCog(bot))

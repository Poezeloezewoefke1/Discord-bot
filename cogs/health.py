"""`/test` — one command that answers "is this thing actually working?".

Worth having because this bot is hosted somewhere that restarts it every few
hours, and because most of what can go wrong is silent: a channel deleted, a role
reordered so auto-role stops working, a permission removed. Rather than each of
those failing quietly at the moment it matters, this checks them all on demand.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
import service

OK = "✅"
BAD = "❌"
WARN = "⚠️"
INFO = "•"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m" if minutes else "under a minute"


class HealthCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _channel_check(
        self, guild: discord.Guild, channel_id: int | None, label: str, needs: tuple[str, ...]
    ) -> tuple[str, bool]:
        """One line describing whether a configured channel is usable."""
        if not channel_id:
            return f"{INFO} {label}: *not set*", True

        channel = guild.get_channel(channel_id)
        if channel is None:
            return f"{BAD} {label}: channel was deleted — re-run setup", False

        perms = channel.permissions_for(guild.me)
        missing = [n.replace("_", " ") for n in needs if not getattr(perms, n)]
        if missing:
            return f"{BAD} {label}: {channel.mention} — I lack {', '.join(missing)}", False
        return f"{OK} {label}: {channel.mention}", True

    @app_commands.command(
        name="test", description="Check the bot is online and everything is set up correctly"
    )
    @app_commands.guild_only()
    async def test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        cfg = db.get_config(guild.id)
        problems = 0

        # --- connection -------------------------------------------------
        latency_ms = self.bot.latency * 1000
        connection = [f"{OK} Online and responding"]
        if latency_ms == latency_ms and latency_ms > 0:  # NaN before first heartbeat
            connection.append(f"{INFO} Ping: {latency_ms:.0f} ms")

        started = getattr(self.bot, "started_at", None)
        if started:
            running = (discord.utils.utcnow() - started).total_seconds()
            line = f"{INFO} Running for {human_duration(running)}"
            if config.SHIFT_MINUTES:
                left = config.SHIFT_MINUTES * 60 - running
                line += (
                    f", restarts in about {human_duration(left)}"
                    if left > 0
                    else ", restarting shortly"
                )
            connection.append(line)

        # --- build board ------------------------------------------------
        board = []
        if cfg is None:
            board.append(f"{BAD} Not set up — run `/setup`")
            problems += 1
        else:
            for label, role_id in (
                ("Builder role", cfg["builder_role_id"]),
                ("Script writer role", cfg["scripter_role_id"]),
            ):
                if not role_id:
                    board.append(f"{BAD} {label}: not set — run `/setup`")
                    problems += 1
                elif guild.get_role(role_id) is None:
                    board.append(f"{BAD} {label}: role was deleted — re-run `/setup`")
                    problems += 1
                else:
                    board.append(f"{OK} {label}: <@&{role_id}>")

            for label, key, needs in (
                ("Requests channel", "requests_channel_id",
                 ("send_messages", "embed_links", "create_public_threads")),
                ("Board channel", "board_channel_id", ("send_messages", "embed_links")),
            ):
                line, ok = self._channel_check(guild, cfg[key], label, needs)
                if not cfg[key]:
                    line = f"{BAD} {label}: not set — run `/setup`"
                    ok = False
                board.append(line)
                problems += not ok

        # --- welcome ----------------------------------------------------
        welcome = []
        if cfg is None or not cfg["welcome_channel_id"]:
            welcome.append(f"{INFO} Off — set up with `/welcome-setup`")
        else:
            line, ok = self._channel_check(
                guild, cfg["welcome_channel_id"], "Welcome channel",
                ("send_messages", "embed_links"),
            )
            welcome.append(line)
            problems += not ok

            line, ok = self._channel_check(
                guild, cfg["goodbye_channel_id"], "Goodbye channel",
                ("send_messages", "embed_links"),
            )
            welcome.append(line)
            problems += not ok

            if cfg["applications_channel_id"]:
                exists = guild.get_channel(cfg["applications_channel_id"]) is not None
                welcome.append(
                    f"{OK} Points to: <#{cfg['applications_channel_id']}>"
                    if exists
                    else f"{BAD} Applications channel was deleted — re-run `/welcome-setup`"
                )
                problems += not exists

            if cfg["auto_role_id"]:
                role = guild.get_role(cfg["auto_role_id"])
                if role is None:
                    welcome.append(f"{BAD} Auto-role was deleted — re-run `/welcome-setup`")
                    problems += 1
                else:
                    blocker = service.missing_role_permissions(guild, role)
                    if blocker:
                        first = blocker.split("\n")[0]
                        welcome.append(f"{BAD} Auto-role {role.mention} won't work: {first}")
                        problems += 1
                    else:
                        welcome.append(f"{OK} Auto-role: {role.mention}")
            else:
                welcome.append(f"{INFO} Auto-role: *none*")

        # --- protection -------------------------------------------------
        protection = []
        if cfg is None or not cfg["security_log_channel_id"]:
            protection.append(f"{INFO} Off — set up with `/security-setup`")
        else:
            from cogs.security import watch_only

            line, ok = self._channel_check(
                guild, cfg["security_log_channel_id"], "Alert channel",
                ("send_messages", "embed_links"),
            )
            protection.append(line)
            problems += not ok

            if watch_only(cfg):
                protection.append(f"{INFO} 🔍 **Watch mode** — reports only, takes no action")
            else:
                protection.append(f"{OK} 🛡️ Live — action on trip: **{cfg['security_action']}**")
                if not guild.me.guild_permissions.ban_members:
                    protection.append(f"{BAD} I lack **Ban Members**, so I can't enforce it")
                    problems += 1

            if cfg["honeypot_channel_id"]:
                line, ok = self._channel_check(
                    guild, cfg["honeypot_channel_id"], "Honeypot",
                    ("read_messages",),
                )
                protection.append(line)
                problems += not ok
            else:
                protection.append(f"{INFO} Honeypot: *none*")

            if cfg["min_account_age_days"]:
                protection.append(f"{OK} New accounts: under {cfg['min_account_age_days']} days")
            if cfg["raid_join_count"]:
                protection.append(
                    f"{OK} Raid: {cfg['raid_join_count']} joins "
                    f"in {cfg['raid_window_seconds']}s"
                )

            if cfg["scam_scanning"]:
                if self.bot.intents.message_content:
                    protection.append(f"{OK} Scam links: on")
                else:
                    protection.append(
                        f"{BAD} Scam links: on, but the Message Content intent is off "
                        f"— it isn't actually scanning"
                    )
                    problems += 1
            else:
                protection.append(f"{INFO} Scam links: off")

        # --- tickets ----------------------------------------------------
        ticketing = []
        if cfg is None or not cfg["ticket_category_id"]:
            ticketing.append(f"{INFO} Off — set up with `/ticket-setup`")
        else:
            from cogs.tickets import CATEGORY_LIMIT

            category = guild.get_channel(cfg["ticket_category_id"])
            if not isinstance(category, discord.CategoryChannel):
                ticketing.append(f"{BAD} Category was deleted — re-run `/ticket-setup`")
                problems += 1
            else:
                spare = CATEGORY_LIMIT - len(category.channels)
                marker = WARN if spare <= 10 else OK
                ticketing.append(f"{marker} Category: **{category.name}** ({spare} slots left)")

            if not guild.me.guild_permissions.manage_channels:
                ticketing.append(f"{BAD} I lack **Manage Channels** — no ticket can be created")
                problems += 1

            line, ok = self._channel_check(
                guild, cfg["ticket_log_channel_id"], "Ticket log",
                ("send_messages", "embed_links", "attach_files"),
            )
            ticketing.append(line)
            problems += not ok

            open_now = len(db.list_tickets(guild.id, db.TICKET_OPEN))
            claimed = len(db.list_tickets(guild.id, db.TICKET_CLAIMED))
            ticketing.append(f"{INFO} {open_now} open · {claimed} being handled")

            if not self.bot.intents.message_content:
                ticketing.append(
                    f"{WARN} Transcripts won't include message text "
                    f"(Message Content intent is off)"
                )

        # --- upload notifications ---------------------------------------
        uploads = []
        if cfg is None or not cfg["notify_channel_id"]:
            uploads.append(f"{INFO} Off — set up with `/notify-setup`")
        else:
            import notify as notify_lib

            line, ok = self._channel_check(
                guild, cfg["notify_channel_id"], "Announcements",
                ("send_messages", "embed_links"),
            )
            uploads.append(line)
            problems += not ok

            channel = guild.get_channel(cfg["notify_channel_id"])
            if channel is not None and not channel.permissions_for(guild.me).mention_everyone:
                uploads.append(f"{WARN} No **Mention Everyone** — posts won't ping")

            cog = self.bot.get_cog("NotifyCog")
            if cog is None or not cog.poll.is_running():
                uploads.append(f"{BAD} The upload check loop is **not running**")
                problems += 1

            for creator in notify_lib.CREATORS:
                feed = db.get_feed(creator["handle"])
                if feed is None or not feed["channel_id"]:
                    uploads.append(f"{INFO} {creator['name']} — not resolved yet")
                elif feed["last_error"]:
                    uploads.append(f"{BAD} {creator['name']} — {feed['last_error']}")
                    problems += 1
                else:
                    ts = db.to_unix(feed["last_checked"])
                    when = f"<t:{ts}:R>" if ts else "never"
                    uploads.append(f"{OK} {creator['name']} — checked {when}")

        # --- data -------------------------------------------------------
        try:
            counts = {
                status: len(db.list_builds(guild.id, status))
                for status in (db.STATUS_OPEN, db.STATUS_CLAIMED, db.STATUS_COMPLETE)
            }
            data = [
                f"{OK} Database readable",
                f"{INFO} 🟡 {counts[db.STATUS_OPEN]} open · "
                f"🔨 {counts[db.STATUS_CLAIMED]} being built · "
                f"✅ {counts[db.STATUS_COMPLETE]} finished",
            ]
        except Exception as exc:  # noqa: BLE001 — the whole point is to report it
            data = [f"{BAD} Database error: `{type(exc).__name__}: {exc}`"]
            problems += 1

        if problems:
            colour = config.COLOR_ERROR
            heading = f"{problems} thing{'s' if problems != 1 else ''} need attention"
        else:
            colour = config.COLOR_DONE
            heading = "Everything works"

        embed = discord.Embed(title=f"🩺 {heading}", color=colour)
        embed.add_field(name="Connection", value="\n".join(connection), inline=False)
        embed.add_field(name="Build board", value="\n".join(board)[:1024], inline=False)
        embed.add_field(name="Welcome messages", value="\n".join(welcome)[:1024], inline=False)
        embed.add_field(name="Protection", value="\n".join(protection)[:1024], inline=False)
        embed.add_field(name="Tickets", value="\n".join(ticketing)[:1024], inline=False)
        embed.add_field(name="Upload alerts", value="\n".join(uploads)[:1024], inline=False)
        embed.add_field(name="Data", value="\n".join(data)[:1024], inline=False)
        embed.set_footer(text="Only you can see this")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="ping", description="Quick check that the bot is awake")
    @app_commands.guild_only()
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = self.bot.latency * 1000
        suffix = f" · {latency_ms:.0f} ms" if latency_ms == latency_ms and latency_ms > 0 else ""
        await interaction.response.send_message(
            embed=embeds.success(f"I'm awake{suffix}"), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthCog(bot))

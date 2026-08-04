"""Anti-raid and anti-spam protections.

Every protection routes through the same path: work out whether something tripped,
check the person isn't staff, log it, and only then act — and in watch mode the
"act" step is skipped entirely. The rules themselves live in security.py so they
can be tested without Discord.
"""

from __future__ import annotations

import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
import security
from views import security_alert_view

log = logging.getLogger(__name__)

HONEYPOT_WARNING = (
    "# ⛔ DO NOT POST HERE\n"
    "This channel is a trap for spam bots.\n\n"
    "**Posting anything here will get you banned automatically.**\n"
    "There is nothing to see and nothing to do — just leave it alone."
)


def watch_only(cfg) -> bool:
    """Watch mode is the default: unset means watching, not acting."""
    if cfg is None:
        return True
    value = cfg["security_watch_only"]
    return True if value is None else bool(value)


class SecurityCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.raids = security.RaidTracker()

    # ----------------------------------------------------------------------
    # the one path everything goes through
    # ----------------------------------------------------------------------

    async def handle(
        self,
        guild: discord.Guild,
        subject: discord.Member | discord.User | None,
        decision: security.Decision,
    ) -> None:
        outcome = "nothing"

        if not decision.watch_only and subject is not None:
            outcome = await self.enforce(guild, subject, decision)

        cfg = db.get_config(guild.id)
        channel = (
            guild.get_channel(cfg["security_log_channel_id"])
            if cfg and cfg["security_log_channel_id"]
            else None
        )
        if not isinstance(channel, discord.TextChannel):
            log.warning(
                "security trip in guild %s with no log channel: %s — %s",
                guild.id, decision.reason, decision.detail,
            )
            return

        embed = embeds.security_alert(decision, subject, guild, outcome)
        view = (
            security_alert_view(subject.id)
            if decision.watch_only and subject is not None
            else None
        )
        try:
            await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.warning("could not post security alert in guild %s", guild.id, exc_info=True)

    async def enforce(
        self, guild: discord.Guild, subject: discord.Member | discord.User, decision
    ) -> str:
        """Carry out the action. Never raises — a failure is reported, not swallowed."""
        action = decision.action
        reason = f"{decision.label}: {decision.detail}"[:400]

        try:
            if action == security.ACTION_BAN:
                await guild.ban(subject, reason=reason, delete_message_seconds=86400)
                return "🔨 Banned"
            if action == security.ACTION_KICK:
                await guild.kick(subject, reason=reason)
                return "👢 Kicked"
            if action == security.ACTION_TIMEOUT:
                if isinstance(subject, discord.Member):
                    await subject.timeout(datetime.timedelta(hours=24), reason=reason)
                    return "🔇 Timed out for 24h"
                return "could not time out — they already left"
        except discord.Forbidden:
            return (
                "❌ **I lack permission.** I need Ban Members / Kick Members, "
                "and my role must sit above theirs in Server Settings → Roles."
            )
        except discord.HTTPException as exc:
            return f"❌ Failed: `{exc}`"
        return "alert only"

    # ----------------------------------------------------------------------
    # protections
    # ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        cfg = db.get_config(message.guild.id)
        if cfg is None:
            return
        if security.is_exempt(message.author, self.bot.user.id if self.bot.user else None):
            return

        if cfg["honeypot_channel_id"] and message.channel.id == cfg["honeypot_channel_id"]:
            await self.handle(
                message.guild,
                message.author,
                security.decide(
                    security.REASON_HONEYPOT,
                    f"Posted in {message.channel.mention}, which nobody should ever post in.",
                    watch_only(cfg),
                    cfg["security_action"],
                ),
            )
            return

        if cfg["scam_scanning"] and self.bot.intents.message_content:
            detail = security.find_scam(message.content)
            if detail:
                await self.handle(
                    message.guild,
                    message.author,
                    security.decide(
                        security.REASON_SCAM,
                        f"{detail}\nIn {message.channel.mention}",
                        watch_only(cfg),
                        cfg["security_action"],
                    ),
                )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = db.get_config(member.guild.id)
        if cfg is None:
            return

        window = cfg["raid_window_seconds"] or security.DEFAULT_RAID_WINDOW_SECONDS
        threshold = cfg["raid_join_count"] or security.DEFAULT_RAID_JOIN_COUNT
        recent = self.raids.record(member.guild.id, window)
        if recent >= threshold:
            self.raids.reset(member.guild.id)  # one alert per burst, not one per join
            await self.handle(
                member.guild,
                None,
                security.decide(
                    security.REASON_RAID,
                    f"**{recent} members joined in {window} seconds.** "
                    f"That's at or above the limit of {threshold}.",
                    watch_only(cfg),
                    security.ACTION_ALERT,
                ),
            )

        if security.is_exempt(member, self.bot.user.id if self.bot.user else None):
            return

        min_days = cfg["min_account_age_days"]
        if min_days:
            detail = security.check_account_age(member.created_at, min_days)
            if detail:
                await self.handle(
                    member.guild,
                    member,
                    security.decide(
                        security.REASON_NEW_ACCOUNT,
                        detail,
                        watch_only(cfg),
                        cfg["security_action"],
                    ),
                )

    # ----------------------------------------------------------------------
    # commands
    # ----------------------------------------------------------------------

    @app_commands.command(
        name="security-setup", description="Set up anti-raid and anti-spam protection"
    )
    @app_commands.describe(
        log_channel="Where security alerts go (staff only channel)",
        honeypot_channel="A bait channel bots get caught in (optional but the most effective)",
        min_account_age_days="Flag accounts newer than this many days (0 to disable)",
        raid_joins="How many joins counts as a raid",
        raid_seconds="...within how many seconds",
        scam_scanning="Watch messages for fake Nitro/Steam links",
        action="What to do when something trips (starts in watch mode regardless)",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Ban", value=security.ACTION_BAN),
            app_commands.Choice(name="Kick", value=security.ACTION_KICK),
            app_commands.Choice(name="Timeout for 24h", value=security.ACTION_TIMEOUT),
            app_commands.Choice(name="Alert staff only", value=security.ACTION_ALERT),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_setup(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel,
        honeypot_channel: discord.TextChannel | None = None,
        min_account_age_days: app_commands.Range[int, 0, 365] = 7,
        raid_joins: app_commands.Range[int, 2, 100] = 10,
        raid_seconds: app_commands.Range[int, 5, 3600] = 60,
        scam_scanning: bool = False,
        action: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        perms = log_channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                embed=embeds.error(
                    f"I can't post in {log_channel.mention} — I need Send Messages and Embed Links."
                ),
                ephemeral=True,
            )
            return

        if scam_scanning and not self.bot.intents.message_content:
            await interaction.followup.send(
                embed=embeds.error(
                    "**Scam scanning can't be turned on yet.**\n"
                    "It needs the Message Content intent, which is off.\n\n"
                    "1. Developer Portal → your app → Bot → enable **Message Content Intent**\n"
                    "2. Then set `ENABLE_MESSAGE_CONTENT: 1` in `.github/workflows/bot.yml`\n\n"
                    "In that order — asking for the intent before enabling it stops the bot "
                    "starting at all. Everything else can be set up now."
                ),
                ephemeral=True,
            )
            return

        existing = db.get_config(interaction.guild.id)
        first_time = existing is None or existing["security_log_channel_id"] is None

        db.save_config(
            interaction.guild.id,
            security_log_channel_id=log_channel.id,
            honeypot_channel_id=honeypot_channel.id if honeypot_channel else None,
            min_account_age_days=int(min_account_age_days),
            raid_join_count=int(raid_joins),
            raid_window_seconds=int(raid_seconds),
            scam_scanning=1 if scam_scanning else 0,
            security_action=(action.value if action else security.ACTION_BAN),
            # Never silently start enforcing. Watch mode is only ever left
            # deliberately, via /security-mode.
            **({"security_watch_only": 1} if first_time else {}),
        )

        posted = ""
        if honeypot_channel is not None:
            posted = await self.post_honeypot_warning(honeypot_channel)

        cfg = db.get_config(interaction.guild.id)
        if watch_only(cfg):
            mode_line = (
                "🔍 **Watch mode is on** — it will only report, not act. "
                "Leave it like this for a few days, then `/security-mode live`."
            )
        else:
            mode_line = "🛡️ **Live** — it will act on trips."

        honeypot_line = honeypot_channel.mention if honeypot_channel else "*none*"
        await interaction.followup.send(
            embed=embeds.success(
                "**Protection configured.**\n"
                f"📋 Alerts: {log_channel.mention}\n"
                f"🍯 Honeypot: {honeypot_line}\n"
                f"🕑 New accounts: under {min_account_age_days} days\n"
                f"🌊 Raid: {raid_joins} joins in {raid_seconds}s\n"
                f"🎣 Scam links: {'on' if scam_scanning else 'off'}\n"
                f"⚖️ Action: **{cfg['security_action']}**\n\n"
                f"{mode_line}{posted}"
            ),
            ephemeral=True,
        )

    async def post_honeypot_warning(self, channel: discord.TextChannel) -> str:
        """A honeypot with no warning catches curious members instead of bots."""
        try:
            message = await channel.send(HONEYPOT_WARNING)
        except discord.HTTPException:
            return f"\n\n⚠️ Couldn't post the warning in {channel.mention} — post one yourself."
        try:
            await message.pin()
        except discord.HTTPException:
            return f"\n\n⚠️ Posted the warning in {channel.mention} but couldn't pin it."
        return f"\n\n📌 Warning posted and pinned in {channel.mention}."

    @app_commands.command(
        name="security-mode", description="Switch between watch-only and acting for real"
    )
    @app_commands.describe(mode="watch = report only · live = actually act")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Watch only — report, don't act", value="watch"),
            app_commands.Choice(name="Live — actually act", value="live"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        cfg = db.get_config(interaction.guild.id)
        if cfg is None or cfg["security_log_channel_id"] is None:
            await interaction.response.send_message(
                embed=embeds.error("Run `/security-setup` first."), ephemeral=True
            )
            return

        watching = mode.value == "watch"
        db.save_config(interaction.guild.id, security_watch_only=1 if watching else 0)

        if watching:
            message = "🔍 **Watch mode.** Trips get reported to the log, nothing else happens."
        else:
            message = (
                f"🛡️ **Live.** Trips will now result in: **{cfg['security_action']}**.\n"
                "Staff are still never actioned. Switch back any time with "
                "`/security-mode watch`."
            )
        await interaction.response.send_message(embed=embeds.success(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SecurityCog(bot))

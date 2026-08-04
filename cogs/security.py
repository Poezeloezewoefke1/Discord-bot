"""Anti-raid and anti-spam protections.

Every protection routes through the same path: work out whether something tripped,
check the person isn't staff, log it, and only then act — and in watch mode the
"act" step is skipped entirely. The rules themselves live in security.py so they
can be tested without Discord.
"""

from __future__ import annotations

import datetime
import logging
import time

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


def missing_action_permission(guild: discord.Guild, action: str) -> str | None:
    """Why the bot couldn't carry out this action, or None if it can.

    A "live" mode that can't actually enforce is worse than watch mode: it claims
    protection it doesn't have, and Discord only says otherwise with a bare 403 at
    the moment it matters.
    """
    perms = guild.me.guild_permissions
    needed = {
        security.ACTION_BAN: ("ban_members", "Ban Members"),
        security.ACTION_KICK: ("kick_members", "Kick Members"),
        security.ACTION_TIMEOUT: ("moderate_members", "Timeout Members"),
    }.get(action)

    if needed is None:
        return None
    attribute, label = needed
    if getattr(perms, attribute, False):
        return None
    return (
        f"I don't have the **{label}** permission, so I can't {action} anyone.\n"
        f"Add it in **Server Settings → Roles → my role**, and make sure my role sits "
        f"above the people it needs to act on."
    )


def raid_rate_warning(joins: int | None, seconds: int | None) -> str:
    """Flag a raid threshold so steep it can never actually be reached.

    30 joins in 5 seconds asks for six joins a second sustained — a setting that
    looks configured but silently never fires, which is worse than off.
    """
    if not joins or not seconds:
        return ""
    rate = joins / seconds
    if rate <= 1:
        return ""
    return (
        f"\n\n⚠️ **That raid setting will almost never trigger.** "
        f"{joins} joins in {seconds}s means **{rate:.0f} joins every second**, sustained. "
        f"Something like `raid_joins:10 raid_seconds:60` is the usual shape."
    )


def watch_only(cfg) -> bool:
    """Watch mode is the default: unset means watching, not acting."""
    if cfg is None:
        return True
    value = cfg["security_watch_only"]
    return True if value is None else bool(value)


class SecurityCog(commands.Cog):
    # Staff chatting in the honeypot shouldn't flood the log with confirmations.
    ARMED_NOTICE_INTERVAL = 600.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.raids = security.RaidTracker()
        self._armed_notice_at: dict[int, float] = {}

    def should_send_armed_notice(self, guild_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        last = self._armed_notice_at.get(guild_id)
        if last is not None and now - last < self.ARMED_NOTICE_INTERVAL:
            return False
        self._armed_notice_at[guild_id] = now
        return True

    async def announce_trap_armed(self, message: discord.Message, cfg) -> None:
        """Tell the log that the honeypot caught staff and let them go.

        Silence here is what made this look broken: the obvious way to test a
        honeypot is to post in it yourself, and staff are exactly who is exempt.
        """
        if not cfg["security_log_channel_id"]:
            return
        if not self.should_send_armed_notice(message.guild.id):
            return
        channel = message.guild.get_channel(cfg["security_log_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(
                embed=embeds.trap_armed(
                    message.author,
                    message.channel.id,
                    cfg["security_action"] or security.DEFAULT_ACTION,
                )
            )
        except discord.HTTPException:
            log.warning("could not post trap-armed notice", exc_info=True)

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

        in_honeypot = bool(
            cfg["honeypot_channel_id"] and message.channel.id == cfg["honeypot_channel_id"]
        )

        if security.is_exempt(message.author, self.bot.user.id if self.bot.user else None):
            # Exempt — but say so out loud when it's the honeypot, so testing it
            # yourself shows something rather than nothing.
            if in_honeypot:
                await self.announce_trap_armed(message, cfg)
            return

        if in_honeypot:
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
        min_account_age_days: app_commands.Range[int, 0, 365] | None = None,
        raid_joins: app_commands.Range[int, 2, 100] | None = None,
        raid_seconds: app_commands.Range[int, 5, 3600] | None = None,
        scam_scanning: bool | None = None,
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

        def keep(column: str, given, fallback):
            """Omitted parameters keep what's stored, rather than silently resetting it.

            Re-running this to change one setting used to quietly reset the others.
            """
            if given is not None:
                return given
            if existing is not None and existing[column] is not None:
                return existing[column]
            return fallback

        settings = {
            "security_log_channel_id": log_channel.id,
            "min_account_age_days": int(
                keep("min_account_age_days", min_account_age_days,
                     security.DEFAULT_MIN_ACCOUNT_AGE_DAYS)
            ),
            "raid_join_count": int(
                keep("raid_join_count", raid_joins, security.DEFAULT_RAID_JOIN_COUNT)
            ),
            "raid_window_seconds": int(
                keep("raid_window_seconds", raid_seconds, security.DEFAULT_RAID_WINDOW_SECONDS)
            ),
            "scam_scanning": int(bool(keep("scam_scanning", scam_scanning, 0))),
            "security_action": keep(
                "security_action", action.value if action else None, security.ACTION_BAN
            ),
        }
        # The honeypot channel is only changed when one is named, so re-running
        # the command without it doesn't quietly disarm the trap.
        if honeypot_channel is not None:
            settings["honeypot_channel_id"] = honeypot_channel.id

        # Never silently start enforcing. Watch mode is only ever left deliberately.
        if first_time:
            settings["security_watch_only"] = 1

        db.save_config(interaction.guild.id, **settings)

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

        # Report what is actually stored, not what was typed — that difference is
        # the whole point of keeping omitted settings.
        honeypot_line = (
            f"<#{cfg['honeypot_channel_id']}>" if cfg["honeypot_channel_id"] else "*none*"
        )
        warning = raid_rate_warning(cfg["raid_join_count"], cfg["raid_window_seconds"])

        await interaction.followup.send(
            embed=embeds.success(
                "**Protection configured.**\n"
                f"📋 Alerts: {log_channel.mention}\n"
                f"🍯 Honeypot: {honeypot_line}\n"
                f"🕑 New accounts: under {cfg['min_account_age_days']} days\n"
                f"🌊 Raid: {cfg['raid_join_count']} joins in {cfg['raid_window_seconds']}s\n"
                f"🎣 Scam links: {'on' if cfg['scam_scanning'] else 'off'}\n"
                f"⚖️ Action: **{cfg['security_action']}**\n\n"
                f"{mode_line}{posted}{warning}"
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
        name="security-test", description="Check the protections are armed and working"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def security_test(self, interaction: discord.Interaction) -> None:
        cfg = db.get_config(interaction.guild.id)
        if cfg is None or cfg["security_log_channel_id"] is None:
            await interaction.response.send_message(
                embed=embeds.error("Protection isn't set up. Run `/security-setup` first."),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        action = cfg["security_action"] or security.DEFAULT_ACTION
        lines = []

        if watch_only(cfg):
            lines.append(
                "🔍 **Watch mode** — trips are reported to the log, nobody is actioned.\n"
                "Use `/security-mode live` when you're ready for it to act."
            )
        else:
            lines.append(f"🛡️ **Live** — a trip results in: **{action}**.")
            blocker = missing_action_permission(guild, action)
            if blocker:
                lines.append(f"❌ **But it can't act:** {blocker}")

        if cfg["honeypot_channel_id"]:
            channel = guild.get_channel(cfg["honeypot_channel_id"])
            if channel is None:
                lines.append("❌ Honeypot channel was deleted — re-run `/security-setup`.")
            else:
                lines.append(f"🍯 Honeypot: {channel.mention} — armed.")
        else:
            lines.append("• Honeypot: *none set*")

        lines.append(
            f"🕑 New accounts flagged under **{cfg['min_account_age_days']}** days"
            if cfg["min_account_age_days"]
            else "• New-account check: off"
        )
        lines.append(
            f"🌊 Raid: **{cfg['raid_join_count']}** joins in **{cfg['raid_window_seconds']}s**"
        )

        if cfg["scam_scanning"] and not self.bot.intents.message_content:
            lines.append("❌ Scam scanning is on but the Message Content intent is off.")
        elif cfg["scam_scanning"]:
            lines.append("🎣 Scam links: scanning")

        # The thing people actually get wrong when testing.
        exempt = security.is_exempt(
            interaction.user, self.bot.user.id if self.bot.user else None
        )
        if exempt:
            lines.append(
                "\n---\n"
                "⚠️ **You personally are exempt.** Staff are never actioned, so posting in the "
                "honeypot yourself will *not* ban you — the log will just note the trap is armed.\n"
                "To see it work for real, post from an account with no staff permissions."
            )
        else:
            lines.append(
                "\n---\n"
                f"You are **not** exempt — posting in the honeypot would get you **{action}**."
            )

        await interaction.response.send_message(
            embed=embeds.notice("\n".join(lines)), ephemeral=True
        )

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
        action = cfg["security_action"] or security.DEFAULT_ACTION

        # Refuse to claim protection the bot can't deliver.
        if not watching:
            blocker = missing_action_permission(interaction.guild, action)
            if blocker:
                await interaction.response.send_message(
                    embed=embeds.error(
                        f"**Not switching to live — I couldn't enforce it.**\n\n{blocker}\n\n"
                        "Staying in watch mode. Fix that and run this again."
                    ),
                    ephemeral=True,
                )
                return

        db.save_config(interaction.guild.id, security_watch_only=1 if watching else 0)

        if watching:
            message = "🔍 **Watch mode.** Trips get reported to the log, nothing else happens."
        else:
            message = (
                f"🛡️ **Live.** Trips will now result in: **{action}**, and I have the "
                f"permission to do it.\n"
                "Staff are still never actioned. Switch back any time with "
                "`/security-mode watch`."
            )
        await interaction.response.send_message(embed=embeds.success(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SecurityCog(bot))

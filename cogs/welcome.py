"""Greeting new members, giving them a role, and noting when people leave.

This replaces what a separate welcome bot was doing, so the server only needs
one bot. The wording is fixed in code; the channels and role are configured with
/welcome-setup because their IDs differ per server.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import db
import embeds
from service import missing_role_permissions

log = logging.getLogger(__name__)


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------------
    # events
    # ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = db.get_config(member.guild.id)
        if cfg is None:
            return

        # Deliberately separate from the welcome post: giving out the role is the
        # part most likely to fail, and it must not cost the member their greeting.
        if cfg["auto_role_id"]:
            role = member.guild.get_role(cfg["auto_role_id"])
            if role is None:
                log.warning(
                    "auto-role %s no longer exists in guild %s",
                    cfg["auto_role_id"],
                    member.guild.id,
                )
            else:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except discord.Forbidden:
                    log.warning(
                        "cannot assign %s in guild %s — check Manage Roles and role order",
                        role.name,
                        member.guild.id,
                    )
                except discord.HTTPException:
                    log.warning("failed to assign auto-role", exc_info=True)

        if not cfg["welcome_channel_id"]:
            return
        channel = member.guild.get_channel(cfg["welcome_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(
                content=member.mention,  # the ping sits above the embed, as before
                embed=embeds.welcome_card(member, cfg["applications_channel_id"]),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            log.warning("failed to post welcome in guild %s", member.guild.id, exc_info=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = db.get_config(member.guild.id)
        if cfg is None or not cfg["goodbye_channel_id"]:
            return
        channel = member.guild.get_channel(cfg["goodbye_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embeds.goodbye_card(member, member.guild))
        except discord.HTTPException:
            log.warning("failed to post goodbye in guild %s", member.guild.id, exc_info=True)

    # ----------------------------------------------------------------------
    # commands
    # ----------------------------------------------------------------------

    @app_commands.command(
        name="welcome-setup", description="Set up welcome messages, auto-role and goodbyes"
    )
    @app_commands.describe(
        welcome_channel="Where new members get welcomed",
        applications_channel="The channel the welcome message points them to",
        auto_role="Role every new member gets automatically (optional)",
        goodbye_channel="Where to post when someone leaves (optional)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_setup(
        self,
        interaction: discord.Interaction,
        welcome_channel: discord.TextChannel,
        applications_channel: discord.TextChannel,
        auto_role: discord.Role | None = None,
        goodbye_channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        problems = []
        for channel in (welcome_channel, goodbye_channel):
            if channel is None:
                continue
            perms = channel.permissions_for(interaction.guild.me)
            for name in ("send_messages", "embed_links"):
                if not getattr(perms, name):
                    problems.append(f"`{name.replace('_', ' ')}` in {channel.mention}")

        if problems:
            await interaction.followup.send(
                embed=embeds.error(
                    "I'm missing permissions:\n"
                    + "\n".join(f"• {p}" for p in problems)
                    + "\n\nGrant those and run `/welcome-setup` again."
                ),
                ephemeral=True,
            )
            return

        if auto_role is not None:
            blocker = missing_role_permissions(interaction.guild, auto_role)
            if blocker:
                await interaction.followup.send(
                    embed=embeds.error(f"I can't give out {auto_role.mention}.\n\n{blocker}"),
                    ephemeral=True,
                )
                return

        db.save_config(
            interaction.guild.id,
            welcome_channel_id=welcome_channel.id,
            applications_channel_id=applications_channel.id,
            auto_role_id=auto_role.id if auto_role else None,
            goodbye_channel_id=goodbye_channel.id if goodbye_channel else None,
        )

        summary = [
            f"👋 Welcomes: {welcome_channel.mention}",
            f"📜 Points them to: {applications_channel.mention}",
            f"🎭 Auto-role: {auto_role.mention if auto_role else '*none*'}",
            f"🚪 Goodbyes: {goodbye_channel.mention if goodbye_channel else '*off*'}",
        ]
        await interaction.followup.send(
            embed=embeds.success(
                "**Welcome messages are on.**\n"
                + "\n".join(summary)
                + "\n\nUse `/welcome-test` to see exactly what a new member will get."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="welcome-test", description="Preview the welcome message on yourself"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_test(self, interaction: discord.Interaction) -> None:
        cfg = db.get_config(interaction.guild.id)
        if cfg is None or not cfg["welcome_channel_id"]:
            await interaction.response.send_message(
                embed=embeds.error("Welcome messages aren't set up. Run `/welcome-setup` first."),
                ephemeral=True,
            )
            return

        note = f"This is what a new member sees in <#{cfg['welcome_channel_id']}>."
        if cfg["auto_role_id"]:
            role = interaction.guild.get_role(cfg["auto_role_id"])
            if role is None:
                note += "\n\n⚠️ The auto-role was deleted — re-run `/welcome-setup`."
            else:
                blocker = missing_role_permissions(interaction.guild, role)
                note += (
                    f"\n\n⚠️ Auto-role {role.mention} **won't work**: {blocker}"
                    if blocker
                    else f"\n\nThey'll also be given {role.mention}."
                )

        await interaction.response.send_message(
            content=f"{interaction.user.mention}\n*{note}*",
            embed=embeds.welcome_card(interaction.user, cfg["applications_channel_id"]),
            ephemeral=True,
        )

    @app_commands.command(
        name="welcome-off", description="Turn off welcomes, goodbyes and auto-role"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_off(self, interaction: discord.Interaction) -> None:
        db.save_config(
            interaction.guild.id,
            welcome_channel_id=None,
            applications_channel_id=None,
            auto_role_id=None,
            goodbye_channel_id=None,
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Welcome messages, goodbyes and auto-role are off. "
                "The build board is unaffected."
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeCog(bot))

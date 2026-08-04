"""Admin wiring: point the bot at your roles and channels, all from inside Discord."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import db
import embeds
import service


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="setup", description="Connect the bot to your builder/scripter roles and channels"
    )
    @app_commands.describe(
        builder_role="The role your builders have",
        scripter_role="The role your script writers have",
        requests_channel="Where build requests get posted",
        board_channel="Where the live 'who is building what' board lives",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        builder_role: discord.Role,
        scripter_role: discord.Role,
        requests_channel: discord.TextChannel,
        board_channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Fail early with a clear reason rather than half-configuring and breaking later.
        missing = []
        for channel, needed in (
            (requests_channel, ("send_messages", "embed_links", "create_public_threads")),
            (board_channel, ("send_messages", "embed_links")),
        ):
            perms = channel.permissions_for(interaction.guild.me)
            for perm in needed:
                if not getattr(perms, perm):
                    missing.append(f"`{perm.replace('_', ' ')}` in {channel.mention}")

        if missing:
            await interaction.followup.send(
                embed=embeds.error(
                    "I'm missing permissions:\n"
                    + "\n".join(f"• {m}" for m in missing)
                    + "\n\nGrant those and run `/setup` again."
                ),
                ephemeral=True,
            )
            return

        previous = db.get_config(interaction.guild.id)
        moved_board = previous is None or previous["board_channel_id"] != board_channel.id

        db.save_config(
            interaction.guild.id,
            builder_role_id=builder_role.id,
            scripter_role_id=scripter_role.id,
            requests_channel_id=requests_channel.id,
            board_channel_id=board_channel.id,
        )
        if moved_board:
            # Force a fresh board message in the new channel.
            db.save_config(interaction.guild.id, board_message_id=None)

        await service.refresh_board_now(self.bot, interaction.guild)

        await interaction.followup.send(
            embed=embeds.success(
                "**Set up.**\n"
                f"🔨 Builders: {builder_role.mention}\n"
                f"📜 Script writers: {scripter_role.mention}\n"
                f"📥 Requests: {requests_channel.mention}\n"
                f"📋 Board: {board_channel.mention}\n\n"
                f"Script writers can now run `/request` to put a build on the board."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="setup-show", description="Show the bot's current configuration")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup_show(self, interaction: discord.Interaction) -> None:
        cfg = db.get_config(interaction.guild.id)
        if cfg is None:
            await interaction.response.send_message(
                embed=embeds.error("Not set up yet. Run `/setup`."), ephemeral=True
            )
            return

        def role(value):
            return f"<@&{value}>" if value else "*not set*"

        def channel(value):
            return f"<#{value}>" if value else "*not set*"

        embed = discord.Embed(title="Build board configuration", color=discord.Color.blurple())
        embed.add_field(name="🔨 Builders", value=role(cfg["builder_role_id"]), inline=False)
        embed.add_field(name="📜 Script writers", value=role(cfg["scripter_role_id"]), inline=False)
        embed.add_field(
            name="📥 Requests channel", value=channel(cfg["requests_channel_id"]), inline=False
        )
        embed.add_field(
            name="📋 Board channel", value=channel(cfg["board_channel_id"]), inline=False
        )

        embed.add_field(
            name="👋 Welcome messages",
            value=(
                f"Channel: {channel(cfg['welcome_channel_id'])}\n"
                f"Points to: {channel(cfg['applications_channel_id'])}\n"
                f"Auto-role: {role(cfg['auto_role_id'])}\n"
                f"Goodbyes: {channel(cfg['goodbye_channel_id'])}"
            ),
            inline=False,
        )

        counts = {
            status: len(db.list_builds(interaction.guild.id, status))
            for status in (db.STATUS_OPEN, db.STATUS_CLAIMED, db.STATUS_COMPLETE)
        }
        embed.add_field(
            name="Builds",
            value=(
                f"🟡 {counts[db.STATUS_OPEN]} open · "
                f"🔨 {counts[db.STATUS_CLAIMED]} being built · "
                f"✅ {counts[db.STATUS_COMPLETE]} finished"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="board-refresh", description="Force the board to redraw now")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def board_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await service.refresh_board_now(self.bot, interaction.guild)
        await interaction.followup.send(embed=embeds.success("Board redrawn."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))

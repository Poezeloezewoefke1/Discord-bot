"""Build board bot — entry point.

Run with:  python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

import config
import db
import embeds
import service
from views import DYNAMIC_ITEMS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("buildboard")

COGS = ("cogs.setup", "cogs.builds")


class BuildBoardBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Needed to list who holds the builder role for the board's "free builders"
        # section. This is a privileged intent — enable it in the Developer Portal.
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        db.init_db()
        config.SCHEMATIC_DIR.mkdir(parents=True, exist_ok=True)

        # Re-attach the button handlers. Build ids live inside each custom_id, so
        # cards posted before the last restart keep working without being re-sent.
        self.add_dynamic_items(*DYNAMIC_ITEMS)

        for cog in COGS:
            await self.load_extension(cog)
            log.info("loaded %s", cog)

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to guild %s", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info(
                "synced %d commands globally — they can take up to an hour to appear. "
                "Set GUILD_ID in .env for instant sync.",
                len(synced),
            )

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, self.user.id)

        # Redraw every board on connect. Hosts that restart the bot on a schedule
        # would otherwise leave a stale board up until the next claim or update.
        for guild in self.guilds:
            if db.get_config(guild.id) is None:
                log.warning("guild %s (%s) has not run /setup yet", guild.name, guild.id)
                continue
            try:
                await service.refresh_board_now(self, guild)
            except Exception:
                log.exception("could not refresh board for guild %s", guild.id)

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the build board"
            )
        )

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        if isinstance(error, config.ConfigError):
            message = str(error)
        else:
            log.exception("command error", exc_info=error)
            message = "Something went wrong on my end. Check the bot logs."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embeds.error(message), ephemeral=True)
            else:
                await interaction.response.send_message(
                    embed=embeds.error(message), ephemeral=True
                )
        except discord.HTTPException:
            pass


async def main() -> None:
    if not config.TOKEN:
        sys.exit(
            "No DISCORD_TOKEN found.\n"
            "Copy .env.example to .env and paste your bot token into it."
        )

    bot = BuildBoardBot()
    bot.tree.on_error = bot.on_app_command_error

    try:
        await bot.start(config.TOKEN)
    except discord.LoginFailure:
        sys.exit("Discord rejected that token. Check DISCORD_TOKEN in your .env.")
    except discord.PrivilegedIntentsRequired:
        sys.exit(
            "The Server Members intent is off.\n"
            "Turn it on at https://discord.com/developers/applications "
            "-> your app -> Bot -> Privileged Gateway Intents -> Server Members Intent."
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")

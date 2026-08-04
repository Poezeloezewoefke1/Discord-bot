"""Buttons and modals.

The buttons are DynamicItems: the build id is encoded in the custom_id and parsed
back out via a template regex. That is what lets a card posted weeks ago keep
working after the bot restarts — there is no in-memory view to lose.
"""

from __future__ import annotations

import re
import sqlite3

import discord

import config
import db
import embeds


async def _guard(interaction: discord.Interaction, coro) -> None:
    """Run a service call, turning ConfigError into a friendly ephemeral reply."""
    try:
        await coro
    except config.ConfigError as exc:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embeds.error(str(exc)), ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )


class ClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bb:claim:(?P<build_id>\d+)",
):
    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        super().__init__(
            discord.ui.Button(
                label="Claim this build",
                emoji="🔨",
                style=discord.ButtonStyle.primary,
                custom_id=f"bb:claim:{build_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["build_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        import service

        await _guard(interaction, service.do_claim(interaction, self.build_id))


class UpdateButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bb:update:(?P<build_id>\d+)",
):
    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        super().__init__(
            discord.ui.Button(
                label="Post update",
                emoji="📤",
                style=discord.ButtonStyle.success,
                custom_id=f"bb:update:{build_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["build_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Discord modals cannot take file attachments, and the schematic is the
        # whole point of an update — so point the builder at the slash command.
        await interaction.response.send_message(
            embed=embeds.notice(
                f"To post your work on **#{self.build_id}**, run:\n"
                f"```/update build:{self.build_id} status:<choice> file:<your schematic>```\n"
                "**Still building** keeps the build yours · "
                "**Handing off** opens it for the next builder · "
                "**Finished** closes it.\n"
                f"Accepted files: {config.allowed_extensions_text()}"
            ),
            ephemeral=True,
        )


class ReleaseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bb:release:(?P<build_id>\d+)",
):
    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        super().__init__(
            discord.ui.Button(
                label="Release",
                emoji="↩️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bb:release:{build_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["build_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        import service

        await _guard(interaction, service.do_release(interaction, self.build_id))


class SchematicButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bb:schem:(?P<build_id>\d+)",
):
    def __init__(self, build_id: int) -> None:
        self.build_id = build_id
        super().__init__(
            discord.ui.Button(
                label="Get latest schematic",
                emoji="⬇️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bb:schem:{build_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["build_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        import service

        await _guard(interaction, service.send_latest_schematic(interaction, self.build_id))


DYNAMIC_ITEMS = (ClaimButton, UpdateButton, ReleaseButton, SchematicButton)


def build_view(build: sqlite3.Row) -> discord.ui.View:
    """The buttons that belong on a build card, given its current state."""
    view = discord.ui.View(timeout=None)
    build_id = build["id"]
    has_file = db.schematic_count(build_id) > 0

    if build["status"] == db.STATUS_CLAIMED:
        view.add_item(UpdateButton(build_id))
        view.add_item(ReleaseButton(build_id))
    elif build["status"] == db.STATUS_OPEN:
        view.add_item(ClaimButton(build_id))

    if has_file:
        view.add_item(SchematicButton(build_id))

    return view


class RequestModal(discord.ui.Modal, title="Describe the build"):
    """What a script writer fills in to put a build on the board."""

    build_title = discord.ui.TextInput(
        label="Build name",
        placeholder="e.g. Medieval spawn",
        max_length=100,
        required=True,
    )
    description = discord.ui.TextInput(
        label="What needs to be built?",
        style=discord.TextStyle.paragraph,
        # Discord rejects the whole modal if this exceeds 100 characters.
        placeholder="Size, theme, where it goes, what it's for. More detail = fewer questions later.",
        max_length=3000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import service

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            build_id = db.create_build(
                guild_id=interaction.guild.id,
                title=str(self.build_title),
                description=str(self.description),
                requested_by=interaction.user.id,
            )
            message = await service.post_build_card(
                interaction.client, interaction.guild, build_id
            )
            service.schedule_board_refresh(interaction.client, interaction.guild)

            link = f"\n{message.jump_url}" if message else ""
            await interaction.followup.send(
                embed=embeds.success(
                    f"Posted **#{build_id} {self.build_title}** — builders can claim it now.{link}"
                ),
                ephemeral=True,
            )
        except config.ConfigError as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)), ephemeral=True)

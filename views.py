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


class SecurityBanButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sec:ban:(?P<user_id>\d+)",
):
    """Acts on a watch-mode alert. Persistent, because a mod may see the alert
    hours later — and on this host the bot has restarted by then."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="Ban them",
                emoji="🔨",
                style=discord.ButtonStyle.danger,
                custom_id=f"sec:ban:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not config.is_admin(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("Only staff can act on security alerts."), ephemeral=True
            )
            return

        try:
            await interaction.guild.ban(
                discord.Object(id=self.user_id),
                reason=f"Security alert, banned by {interaction.user}",
                delete_message_seconds=86400,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=embeds.error(
                    "I don't have permission to ban them — I need **Ban Members**, "
                    "and my role must sit above theirs."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=embeds.error(f"Ban failed: `{exc}`"), ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content=f"🔨 Banned <@{self.user_id}> — {interaction.user.mention} confirmed.",
            view=None,
        )


class SecurityIgnoreButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"sec:ignore:(?P<user_id>\d+)",
):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label="Ignore",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sec:ignore:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not config.is_admin(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("Only staff can act on security alerts."), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=f"✅ Marked as a false alarm by {interaction.user.mention}.", view=None
        )


def security_alert_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SecurityBanButton(user_id))
    view.add_item(SecurityIgnoreButton(user_id))
    return view


# --------------------------------------------------------------------------
# tickets
# --------------------------------------------------------------------------

def _tickets_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("TicketsCog")


async def _call_cog(interaction: discord.Interaction, method: str, *args) -> None:
    cog = _tickets_cog(interaction)
    if cog is None:
        await interaction.response.send_message(
            embed=embeds.error("Tickets aren't loaded right now. Try again in a minute."),
            ephemeral=True,
        )
        return
    await _guard(interaction, getattr(cog, method)(interaction, *args))


class TicketOpenModal(discord.ui.Modal):
    """The short form shown before a ticket channel is created.

    Asking up front means staff open the channel already knowing what it's about,
    instead of a channel that says nothing until the person types again.
    """

    def __init__(self, kind: str) -> None:
        import tickets

        super().__init__(title=tickets.kind_label(kind)[:45])
        self.kind = kind

        self.about: discord.ui.TextInput | None = None
        if kind == tickets.KIND_REPORT:
            self.about = discord.ui.TextInput(
                label="Who is this about?",
                placeholder="Their Discord name, or Minecraft username",
                max_length=100,
                required=True,
            )
            self.add_item(self.about)

        self.details = discord.ui.TextInput(
            label="What happened?" if kind == tickets.KIND_REPORT else "What do you need?",
            style=discord.TextStyle.paragraph,
            placeholder="As much detail as you can. Screenshots can go in the channel after.",
            max_length=2000,
            required=True,
        )
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _call_cog(
            interaction,
            "open_ticket",
            self.kind,
            str(self.details),
            str(self.about) if self.about is not None else None,
        )


class TicketOpenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tk:open:(?P<kind>[a-z]+)",
):
    def __init__(self, kind: str) -> None:
        import tickets

        self.kind = kind
        info = tickets.KINDS.get(kind, {})
        super().__init__(
            discord.ui.Button(
                label=info.get("label", "Open a ticket"),
                emoji=info.get("emoji", "🎫"),
                style=discord.ButtonStyle.primary,
                custom_id=f"tk:open:{kind}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _tickets_cog(interaction)
        if cog is None:
            await interaction.response.send_message(
                embed=embeds.error("Tickets aren't loaded right now. Try again in a minute."),
                ephemeral=True,
            )
            return
        # Refuse before showing the form, so nobody fills one in for nothing.
        blocker = await cog.why_cannot_open(interaction)
        if blocker:
            await interaction.response.send_message(
                embed=embeds.notice(blocker), ephemeral=True
            )
            return
        await interaction.response.send_modal(TicketOpenModal(self.kind))


def _ticket_button(action: str, ticket_id: int, label: str, emoji: str, style) -> discord.ui.Button:
    return discord.ui.Button(
        label=label, emoji=emoji, style=style, custom_id=f"tk:{action}:{ticket_id}"
    )


class TicketClaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"tk:claim:(?P<ticket_id>\d+)"
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            _ticket_button("claim", ticket_id, "Claim", "🙋", discord.ButtonStyle.primary)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _call_cog(interaction, "claim_ticket", self.ticket_id)


class TicketCloseButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"tk:close:(?P<ticket_id>\d+)"
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            _ticket_button("close", ticket_id, "Close", "🔒", discord.ButtonStyle.secondary)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _call_cog(interaction, "close_ticket", self.ticket_id, None)


class TicketReopenButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"tk:reopen:(?P<ticket_id>\d+)"
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            _ticket_button("reopen", ticket_id, "Reopen", "🔓", discord.ButtonStyle.success)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _call_cog(interaction, "reopen_ticket", self.ticket_id)


class TicketTranscriptButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"tk:script:(?P<ticket_id>\d+)"
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            _ticket_button("script", ticket_id, "Transcript", "📄", discord.ButtonStyle.secondary)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _call_cog(interaction, "send_transcript", self.ticket_id)


class TicketDeleteButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"tk:delete:(?P<ticket_id>\d+)"
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(
            _ticket_button("delete", ticket_id, "Delete channel", "🗑️", discord.ButtonStyle.danger)
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _call_cog(interaction, "delete_ticket", self.ticket_id)


def ticket_panel_view() -> discord.ui.View:
    import tickets

    view = discord.ui.View(timeout=None)
    for kind in tickets.KINDS:
        view.add_item(TicketOpenButton(kind))
    return view


def ticket_open_view(ticket_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketClaimButton(ticket_id))
    view.add_item(TicketCloseButton(ticket_id))
    return view


def ticket_closed_view(ticket_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketReopenButton(ticket_id))
    view.add_item(TicketTranscriptButton(ticket_id))
    view.add_item(TicketDeleteButton(ticket_id))
    return view


DYNAMIC_ITEMS = (
    ClaimButton,
    UpdateButton,
    ReleaseButton,
    SchematicButton,
    SecurityBanButton,
    SecurityIgnoreButton,
    TicketOpenButton,
    TicketClaimButton,
    TicketCloseButton,
    TicketReopenButton,
    TicketTranscriptButton,
    TicketDeleteButton,
)


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


class ConfirmDeleteView(discord.ui.View):
    """Are-you-sure for /delete.

    Unlike the build card buttons this is *not* a persistent DynamicItem: it's a
    throwaway dialog, and a stale confirm button surviving a restart is exactly
    what you don't want on an irreversible action. It expires instead.
    """

    def __init__(self, build_id: int, invoker_id: int) -> None:
        super().__init__(timeout=60)
        self.build_id = build_id
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                embed=embeds.error("That confirmation isn't yours."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Delete it", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        import service

        for child in self.children:
            child.disabled = True
        self.stop()

        await interaction.response.edit_message(
            embed=embeds.notice(f"Deleting build #{self.build_id}…"), view=None
        )
        try:
            await service.delete_build_everywhere(interaction.client, self.build_id)
        except config.ConfigError as exc:
            await interaction.edit_original_response(embed=embeds.error(str(exc)))
            return

        await interaction.edit_original_response(
            embed=embeds.success(f"Build #{self.build_id} is gone.")
        )

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.notice(f"Left build #{self.build_id} alone."), view=None
        )


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

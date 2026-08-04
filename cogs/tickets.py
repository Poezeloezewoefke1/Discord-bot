"""Private support tickets, replacing a separate ticket bot.

A panel with buttons creates one private channel per ticket. Closing locks and
renames the channel rather than deleting it, so nothing is lost before somebody
has deliberately taken a transcript — deletion is its own, separate step.
"""

from __future__ import annotations

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
import tickets as ticket_lib
from views import ticket_closed_view, ticket_open_view, ticket_panel_view

log = logging.getLogger(__name__)

# Discord's own cap on how many channels one category can hold.
CATEGORY_LIMIT = 50


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------------
    # helpers
    # ----------------------------------------------------------------------

    def is_support(self, member: discord.Member, cfg) -> bool:
        if config.is_admin(member):
            return True
        role_id = cfg["ticket_support_role_id"] if cfg else None
        return bool(role_id) and any(r.id == role_id for r in member.roles)

    async def why_cannot_open(self, interaction: discord.Interaction) -> str | None:
        """Reason this person can't open a ticket right now, or None."""
        cfg = db.get_config(interaction.guild.id)
        if cfg is None or not cfg["ticket_category_id"]:
            return "Tickets aren't set up on this server yet."

        existing = db.open_ticket_for(interaction.guild.id, interaction.user.id)
        if existing is not None:
            where = f"<#{existing['channel_id']}>" if existing["channel_id"] else "one already"
            return (
                f"You already have an open ticket: {where}\n"
                "Use that one, or close it before opening another."
            )

        category = interaction.guild.get_channel(cfg["ticket_category_id"])
        if not isinstance(category, discord.CategoryChannel):
            return "The ticket category is missing — an admin needs to re-run `/ticket-setup`."
        if len(category.channels) >= CATEGORY_LIMIT:
            return (
                "The ticket category is full, so I can't make another channel. "
                "Staff need to delete some closed tickets."
            )
        return None

    async def log_event(self, guild: discord.Guild, ticket, actor, event: str, **kwargs) -> None:
        cfg = db.get_config(guild.id)
        if cfg is None or not cfg["ticket_log_channel_id"]:
            return
        channel = guild.get_channel(cfg["ticket_log_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=embeds.ticket_log(ticket, actor, event), **kwargs)
        except discord.HTTPException:
            log.warning("could not write ticket log in guild %s", guild.id, exc_info=True)

    # ----------------------------------------------------------------------
    # opening
    # ----------------------------------------------------------------------

    async def open_ticket(
        self, interaction: discord.Interaction, kind: str, details: str, about: str | None
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        blocker = await self.why_cannot_open(interaction)
        if blocker:
            await interaction.followup.send(embed=embeds.notice(blocker), ephemeral=True)
            return

        guild = interaction.guild
        cfg = db.get_config(guild.id)
        category = guild.get_channel(cfg["ticket_category_id"])
        support_role = (
            guild.get_role(cfg["ticket_support_role_id"])
            if cfg["ticket_support_role_id"]
            else None
        )

        ticket_id = db.create_ticket(guild.id, interaction.user.id, kind, details, about)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                manage_channels=True, read_message_history=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
            ),
        }
        if support_role is not None:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
            )

        try:
            channel = await guild.create_text_channel(
                name=ticket_lib.channel_name(kind, ticket_id),
                category=category,
                overwrites=overwrites,
                topic=ticket_lib.safe_topic(details, str(interaction.user), ticket_id),
                reason=f"Ticket #{ticket_id} opened by {interaction.user}",
            )
        except discord.Forbidden:
            db.delete_ticket(ticket_id)  # don't leave a row pointing at nothing
            await interaction.followup.send(
                embed=embeds.error(
                    "I couldn't create the channel — I need **Manage Channels** in that category."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            db.delete_ticket(ticket_id)
            await interaction.followup.send(
                embed=embeds.error(f"Couldn't create the channel: `{exc}`"), ephemeral=True
            )
            return

        db.attach_ticket_channel(ticket_id, channel.id)
        ticket = db.get_ticket(ticket_id)

        mention = support_role.mention if support_role else ""
        try:
            await channel.send(
                content=f"{interaction.user.mention} {mention}".strip(),
                embed=embeds.ticket_header(ticket, interaction.user, cfg["ticket_support_role_id"]),
                view=ticket_open_view(ticket_id),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
        except discord.HTTPException:
            log.warning("could not post ticket header for #%s", ticket_id, exc_info=True)

        await self.log_event(guild, ticket, interaction.user, "opened")
        await interaction.followup.send(
            embed=embeds.success(
                f"Ticket **#{ticket_id:04d}** opened: {channel.mention}\n"
                "Everything you say in there is private to you and staff."
            ),
            ephemeral=True,
        )

    # ----------------------------------------------------------------------
    # claim / close / reopen / transcript / delete
    # ----------------------------------------------------------------------

    async def _staff_ticket(self, interaction: discord.Interaction, ticket_id: int):
        """Fetch a ticket, enforcing that the caller is support. Returns None on refusal."""
        cfg = db.get_config(interaction.guild.id)
        if not self.is_support(interaction.user, cfg):
            await interaction.response.send_message(
                embed=embeds.error("Only support staff can do that."), ephemeral=True
            )
            return None
        ticket = db.get_ticket(ticket_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.error(f"Ticket #{ticket_id:04d} no longer exists."), ephemeral=True
            )
            return None
        return ticket

    async def claim_ticket(self, interaction: discord.Interaction, ticket_id: int) -> None:
        ticket = await self._staff_ticket(interaction, ticket_id)
        if ticket is None:
            return

        if not db.claim_ticket(ticket_id, interaction.user.id):
            holder = ticket["claimed_by"]
            await interaction.response.send_message(
                embed=embeds.error(
                    f"<@{holder}> already has this one." if holder else "This ticket isn't open."
                ),
                ephemeral=True,
            )
            return

        ticket = db.get_ticket(ticket_id)
        opener = interaction.guild.get_member(ticket["opener_id"]) or interaction.user
        await interaction.response.edit_message(
            embed=embeds.ticket_header(ticket, opener, None), view=ticket_open_view(ticket_id)
        )
        await interaction.followup.send(
            f"🙋 {interaction.user.mention} is handling this ticket."
        )
        await self.log_event(interaction.guild, ticket, interaction.user, "claimed")

    async def close_ticket(
        self, interaction: discord.Interaction, ticket_id: int, reason: str | None
    ) -> None:
        cfg = db.get_config(interaction.guild.id)
        ticket = db.get_ticket(ticket_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.error("That ticket no longer exists."), ephemeral=True
            )
            return

        # The person who opened it may close their own ticket; otherwise staff only.
        if not (self.is_support(interaction.user, cfg) or interaction.user.id == ticket["opener_id"]):
            await interaction.response.send_message(
                embed=embeds.error("Only support staff or whoever opened it can close this."),
                ephemeral=True,
            )
            return

        if not db.close_ticket(ticket_id, interaction.user.id, reason):
            await interaction.response.send_message(
                embed=embeds.error("That ticket is already closed."), ephemeral=True
            )
            return

        ticket = db.get_ticket(ticket_id)
        await interaction.response.send_message(
            embed=embeds.ticket_closed(ticket, interaction.user),
            view=ticket_closed_view(ticket_id),
        )

        # Lock it rather than delete it: the conversation is kept until somebody
        # deliberately removes it, transcript or not.
        channel = interaction.guild.get_channel(ticket["channel_id"]) if ticket["channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            opener = interaction.guild.get_member(ticket["opener_id"])
            try:
                if opener is not None:
                    await channel.set_permissions(
                        opener, view_channel=False, reason="Ticket closed"
                    )
                await channel.edit(
                    name=ticket_lib.channel_name(ticket["kind"], ticket_id, closed=True),
                    reason="Ticket closed",
                )
            except discord.HTTPException:
                log.warning("could not lock ticket channel #%s", ticket_id, exc_info=True)

        await self.log_event(interaction.guild, ticket, interaction.user, "closed")

    async def reopen_ticket(self, interaction: discord.Interaction, ticket_id: int) -> None:
        ticket = await self._staff_ticket(interaction, ticket_id)
        if ticket is None:
            return
        if not db.reopen_ticket(ticket_id):
            await interaction.response.send_message(
                embed=embeds.error("That ticket isn't closed."), ephemeral=True
            )
            return

        ticket = db.get_ticket(ticket_id)
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            opener = interaction.guild.get_member(ticket["opener_id"])
            try:
                if opener is not None:
                    await channel.set_permissions(
                        opener, view_channel=True, send_messages=True,
                        read_message_history=True, attach_files=True,
                        reason="Ticket reopened",
                    )
                await channel.edit(
                    name=ticket_lib.channel_name(ticket["kind"], ticket_id), reason="Reopened"
                )
            except discord.HTTPException:
                log.warning("could not reopen ticket channel #%s", ticket_id, exc_info=True)

        await interaction.response.edit_message(
            embed=embeds.ticket_header(
                ticket, interaction.guild.get_member(ticket["opener_id"]) or interaction.user, None
            ),
            view=ticket_open_view(ticket_id),
        )
        await self.log_event(interaction.guild, ticket, interaction.user, "reopened")

    async def build_transcript(self, channel: discord.TextChannel, ticket) -> discord.File:
        messages = [m async for m in channel.history(limit=None, oldest_first=True)]
        text = ticket_lib.render_transcript(
            ticket,
            messages,
            have_message_content=self.bot.intents.message_content,
            guild_name=channel.guild.name,
        )
        return discord.File(
            io.BytesIO(text.encode("utf-8")),
            filename=ticket_lib.transcript_filename(ticket["id"]),
        )

    async def send_transcript(self, interaction: discord.Interaction, ticket_id: int) -> None:
        ticket = await self._staff_ticket(interaction, ticket_id)
        if ticket is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                embed=embeds.error("Run this inside the ticket channel."), ephemeral=True
            )
            return

        try:
            transcript = await self.build_transcript(channel, ticket)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=embeds.error(
                    "I can't read this channel's history — I need **Read Message History**."
                ),
                ephemeral=True,
            )
            return

        await self.log_event(interaction.guild, ticket, interaction.user, "transcript taken",
                             file=transcript)

        note = ""
        if not self.bot.intents.message_content:
            note = (
                "\n\n⚠️ The transcript records who spoke and when, but **not what they said** — "
                "the Message Content intent is off. The channel itself still has everything, "
                "so don't delete it if you need the words."
            )
        await interaction.followup.send(
            embed=embeds.success(f"Transcript sent to the ticket log.{note}"), ephemeral=True
        )

    async def delete_ticket(self, interaction: discord.Interaction, ticket_id: int) -> None:
        ticket = await self._staff_ticket(interaction, ticket_id)
        if ticket is None:
            return
        if ticket["status"] != db.TICKET_CLOSED:
            await interaction.response.send_message(
                embed=embeds.error("Close the ticket before deleting it."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=embeds.notice("Deleting this channel in a moment…")
        )
        await self.log_event(interaction.guild, ticket, interaction.user, "deleted")
        db.delete_ticket(ticket_id)

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.delete(reason=f"Ticket #{ticket_id} deleted by {interaction.user}")
            except discord.HTTPException:
                log.warning("could not delete ticket channel #%s", ticket_id, exc_info=True)

    # ----------------------------------------------------------------------
    # commands
    # ----------------------------------------------------------------------

    @app_commands.command(name="ticket-setup", description="Set up the ticket system")
    @app_commands.describe(
        category="Category new ticket channels are created in",
        support_role="Role that can see and handle tickets",
        log_channel="Where ticket activity and transcripts go",
        panel_channel="Where the 'open a ticket' panel is posted",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        support_role: discord.Role,
        log_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        me = interaction.guild.me
        problems = []
        if not me.guild_permissions.manage_channels:
            problems.append(
                "**Manage Channels** — without it I can't create a ticket channel at all"
            )
        for channel in (log_channel, panel_channel):
            perms = channel.permissions_for(me)
            for name, label in (("send_messages", "Send Messages"), ("embed_links", "Embed Links")):
                if not getattr(perms, name):
                    problems.append(f"**{label}** in {channel.mention}")
        if not log_channel.permissions_for(me).attach_files:
            problems.append(f"**Attach Files** in {log_channel.mention} — needed for transcripts")

        if problems:
            await interaction.followup.send(
                embed=embeds.error(
                    "I'm missing permissions:\n" + "\n".join(f"• {p}" for p in problems)
                ),
                ephemeral=True,
            )
            return

        db.save_config(
            interaction.guild.id,
            ticket_category_id=category.id,
            ticket_support_role_id=support_role.id,
            ticket_log_channel_id=log_channel.id,
            ticket_panel_channel_id=panel_channel.id,
        )

        posted = await self.post_panel(interaction.guild, panel_channel)

        warning = ""
        remaining = CATEGORY_LIMIT - len(category.channels)
        if remaining <= 10:
            warning = (
                f"\n\n⚠️ **{category.name}** has room for only **{remaining}** more channels "
                f"(Discord's limit is {CATEGORY_LIMIT} per category). Delete some closed "
                f"tickets, or tickets will stop opening."
            )
        if not self.bot.intents.message_content:
            warning += (
                "\n\n⚠️ Transcripts won't include message text — the Message Content intent "
                "is off. Closing keeps the channel, so nothing is lost, but delete carefully."
            )

        await interaction.followup.send(
            embed=embeds.success(
                "**Tickets are set up.**\n"
                f"📁 Category: **{category.name}**\n"
                f"🛠️ Support role: {support_role.mention}\n"
                f"📋 Log: {log_channel.mention}\n"
                f"🎫 Panel: {panel_channel.mention}\n\n"
                f"{posted}{warning}"
            ),
            ephemeral=True,
        )

    async def post_panel(self, guild: discord.Guild, channel: discord.TextChannel) -> str:
        cfg = db.get_config(guild.id)
        try:
            message = await channel.send(
                embed=embeds.ticket_panel(guild, cfg["ticket_support_role_id"]),
                view=ticket_panel_view(),
            )
        except discord.HTTPException:
            return f"⚠️ Couldn't post the panel in {channel.mention}."
        db.save_config(guild.id, ticket_panel_message_id=message.id)
        return f"Panel posted: {message.jump_url}"

    @app_commands.command(name="ticket-panel", description="Post the ticket panel again")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_panel_cmd(self, interaction: discord.Interaction) -> None:
        cfg = db.get_config(interaction.guild.id)
        if cfg is None or not cfg["ticket_panel_channel_id"]:
            await interaction.response.send_message(
                embed=embeds.error("Run `/ticket-setup` first."), ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(cfg["ticket_panel_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=embeds.error("The panel channel is gone — re-run `/ticket-setup`."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.post_panel(interaction.guild, channel)
        await interaction.followup.send(embed=embeds.success(result), ephemeral=True)

    @app_commands.command(name="ticket-add", description="Add someone to this ticket")
    @app_commands.describe(member="Who to bring into the ticket")
    @app_commands.guild_only()
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        ticket = db.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.error("This isn't a ticket channel."), ephemeral=True
            )
            return
        cfg = db.get_config(interaction.guild.id)
        if not self.is_support(interaction.user, cfg):
            await interaction.response.send_message(
                embed=embeds.error("Only support staff can add people to a ticket."),
                ephemeral=True,
            )
            return

        try:
            await interaction.channel.set_permissions(
                member, view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
                reason=f"Added to ticket by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=embeds.error("I don't have permission to change this channel."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=embeds.success(f"{member.mention} has been added to this ticket.")
        )

    @app_commands.command(name="ticket-close", description="Close this ticket")
    @app_commands.describe(reason="Why it's being closed (optional)")
    @app_commands.guild_only()
    async def ticket_close_cmd(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        ticket = db.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                embed=embeds.error("This isn't a ticket channel."), ephemeral=True
            )
            return
        await self.close_ticket(interaction, ticket["id"], reason)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketsCog(bot))

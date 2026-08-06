"""Applications, replacing a separate application bot.

A panel of buttons, one per position. Pressing one opens a form; submitting it
posts the answers into a staff-only review channel with Accept and Deny on it.
Either decision DMs the applicant, and accepting hands out the role.

The three things that quietly go wrong with this kind of feature, and what is
done about each:

* **The applicant never hears back.** Most people have DMs from strangers off,
  so the DM fails and — with no handling — nobody notices. Every failed DM is
  reported straight back to the reviewer who pressed the button.
* **The role doesn't get given.** Discord refuses to let a bot hand out a role
  above its own, with a bare 403. That's checked at setup time and again at
  accept time, and reported with the actual fix.
* **Two staff decide at once.** The decision is a conditional UPDATE, so the
  second one loses and is told who got there first — instead of the applicant
  getting an acceptance and a rejection.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import applications as apply_lib
import config
import db
import embeds
import service
from views import application_panel_view, application_review_view

log = logging.getLogger(__name__)


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------------
    # helpers
    # ----------------------------------------------------------------------

    def is_reviewer(self, member: discord.Member) -> bool:
        if config.is_admin(member):
            return True
        cfg = db.get_config(member.guild.id)
        role_id = cfg["apply_reviewer_role_id"] if cfg else None
        return bool(role_id) and any(role.id == role_id for role in member.roles)

    def cooldown_days(self, guild_id: int) -> int:
        cfg = db.get_config(guild_id)
        value = cfg["apply_cooldown_days"] if cfg else None
        return apply_lib.DEFAULT_COOLDOWN_DAYS if value is None else int(value)

    def review_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        cfg = db.get_config(guild.id)
        if cfg is None or not cfg["apply_review_channel_id"]:
            return None
        channel = guild.get_channel(cfg["apply_review_channel_id"])
        return channel if isinstance(channel, discord.TextChannel) else None

    async def why_cannot_apply(self, interaction: discord.Interaction, form) -> str | None:
        """Every reason someone can't apply right now, in one place.

        Called before the form is shown as well as after it is submitted — the
        second check is what stops a form opened an hour ago from landing after
        applications were closed.
        """
        if self.review_channel(interaction.guild) is None:
            return "Applications aren't set up on this server yet. An admin needs to run `/apply-setup`."
        if form is None:
            return "That position doesn't exist any more."

        has_role = bool(form["role_id"]) and any(
            role.id == form["role_id"] for role in getattr(interaction.user, "roles", ())
        )
        return apply_lib.why_cannot_apply(
            form,
            pending=db.pending_application_for(
                interaction.guild.id, form["key"], interaction.user.id
            ),
            last_denial=db.last_denial_for(
                interaction.guild.id, form["key"], interaction.user.id
            ),
            cooldown_days=self.cooldown_days(interaction.guild.id),
            already_has_role=has_role,
        )

    async def form_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        forms = db.list_forms(interaction.guild.id)
        current = (current or "").lower()
        return [
            app_commands.Choice(
                name=f"{form['label']}{'' if form['is_open'] else ' (closed)'}"[:100],
                value=form["key"],
            )
            for form in forms
            if current in form["label"].lower() or current in form["key"]
        ][:25]

    # ----------------------------------------------------------------------
    # applying
    # ----------------------------------------------------------------------

    async def submit_application(
        self, interaction: discord.Interaction, form_key: str, pairs
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        form = db.get_form(interaction.guild.id, form_key)
        blocker = await self.why_cannot_apply(interaction, form)
        if blocker:
            await interaction.followup.send(embed=embeds.notice(blocker), ephemeral=True)
            return

        channel = self.review_channel(interaction.guild)
        application_id = db.create_application(
            interaction.guild.id,
            form["key"],
            form["label"],
            interaction.user.id,
            apply_lib.answers_to_json(pairs),
        )

        cfg = db.get_config(interaction.guild.id)
        reviewer_role_id = cfg["apply_reviewer_role_id"] if cfg else None
        application = db.get_application(application_id)

        try:
            message = await channel.send(
                content=f"<@&{reviewer_role_id}>" if reviewer_role_id else None,
                embed=embeds.application_card(application, interaction.user),
                view=application_review_view(application_id),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException:
            # Don't leave a row nobody will ever see: the applicant would be
            # waiting for a decision on something staff were never shown.
            db.delete_application(application_id)
            log.warning("could not post application #%s", application_id, exc_info=True)
            await interaction.followup.send(
                embed=embeds.error(
                    "I couldn't post your application to the staff channel, so it hasn't "
                    "been saved. Tell an admin to check my permissions there and try again."
                ),
                ephemeral=True,
            )
            return

        db.attach_application_message(application_id, message.id, channel.id)

        await interaction.followup.send(
            embed=embeds.success(
                f"**{form['label']}** application sent — it's number **#{application_id:04d}**.\n"
                "Staff will look at it and you'll get a DM either way.\n\n"
                "If your DMs are closed you won't receive that, so it's worth turning them on "
                "for this server."
            ),
            ephemeral=True,
        )

    # ----------------------------------------------------------------------
    # deciding
    # ----------------------------------------------------------------------

    async def _decide(
        self, interaction: discord.Interaction, application_id: int, accept: bool, note: str | None
    ) -> None:
        if not self.is_reviewer(interaction.user):
            await self._reply(
                interaction, embeds.error("Only staff can decide on applications.")
            )
            return

        application = db.get_application(application_id)
        if application is None or application["guild_id"] != interaction.guild.id:
            await self._reply(
                interaction, embeds.error(f"Application #{application_id:04d} no longer exists.")
            )
            return

        status = db.APPLY_ACCEPTED if accept else db.APPLY_DENIED
        if not db.decide_application(application_id, status, interaction.user.id, note):
            current = db.get_application(application_id)
            _, wording = apply_lib.status_face(current["status"])
            await self._reply(
                interaction,
                embeds.error(
                    f"Application #{application_id:04d} was already decided — "
                    f"**{wording.lower()}** by <@{current['decided_by']}>."
                ),
            )
            return

        application = db.get_application(application_id)
        form = db.get_form(interaction.guild.id, application["form_key"])
        member = interaction.guild.get_member(application["applicant_id"])

        notes = []
        role = None
        if accept and form is not None and form["role_id"]:
            role, problem = await self._grant_role(interaction.guild, member, form)
            if problem:
                notes.append(problem)

        await self._refresh_card(interaction.guild, application)

        dm_problem = await self._notify_applicant(
            interaction.guild, application, member, accept, role
        )
        if dm_problem:
            notes.append(dm_problem)

        headline = (
            f"Accepted #{application_id:04d} — <@{application['applicant_id']}>"
            if accept
            else f"Turned down #{application_id:04d} — <@{application['applicant_id']}>"
        )
        embed = (
            embeds.success(headline)
            if not notes
            else embeds.notice(f"{headline}\n\n" + "\n\n".join(f"⚠️ {n}" for n in notes))
        )
        await self._reply(interaction, embed)

    async def _grant_role(self, guild: discord.Guild, member, form):
        """Hand out the position's role. Returns (role, problem-to-report)."""
        role = guild.get_role(form["role_id"])
        if role is None:
            return None, f"The **{form['label']}** role has been deleted — nothing was given out."
        if member is None:
            return role, (
                f"They've left the server, so I couldn't give them {role.mention}. "
                "The decision is still recorded."
            )

        problem = service.missing_role_permissions(guild, role)
        if problem:
            return role, f"I couldn't give them {role.mention}.\n{problem}"

        try:
            await member.add_roles(role, reason=f"Application #{form['key']} accepted")
        except discord.HTTPException as exc:
            return role, f"Giving them {role.mention} failed: `{exc}`"
        return role, None

    async def _notify_applicant(
        self, guild: discord.Guild, application, member, accept: bool, role
    ) -> str | None:
        """DM the decision. Returns a warning if it couldn't be delivered.

        A silent failure here is the worst outcome the feature has: staff think
        the person was told, the person thinks they were ignored.
        """
        target = member or self.bot.get_user(application["applicant_id"])
        if target is None:
            return "I couldn't find them to send a DM — they may have left. Tell them yourself."
        try:
            await target.send(
                embed=embeds.application_decision_dm(application, guild, accept, role)
            )
        except discord.Forbidden:
            return (
                f"<@{application['applicant_id']}> has DMs closed, so **they haven't been told**. "
                "Somebody needs to tell them directly."
            )
        except discord.HTTPException:
            log.warning("DM failed for application #%s", application["id"], exc_info=True)
            return f"The DM to <@{application['applicant_id']}> failed. Tell them directly."
        return None

    async def _refresh_card(self, guild: discord.Guild, application) -> None:
        """Redraw the review post so the channel shows the outcome, not a live
        pair of buttons somebody will press again."""
        if not application["message_id"] or not application["channel_id"]:
            return
        channel = guild.get_channel(application["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        applicant = guild.get_member(application["applicant_id"])
        try:
            message = await channel.fetch_message(application["message_id"])
            await message.edit(
                content=None,
                embed=embeds.application_card(application, applicant),
                view=None,
            )
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("could not update application card #%s", application["id"], exc_info=True)

    async def _reply(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def accept_application(
        self, interaction: discord.Interaction, application_id: int, note: str | None
    ) -> None:
        await self._decide(interaction, application_id, True, note)

    async def deny_application(
        self, interaction: discord.Interaction, application_id: int, note: str | None
    ) -> None:
        await self._decide(interaction, application_id, False, note)

    # ----------------------------------------------------------------------
    # panel
    # ----------------------------------------------------------------------

    async def post_panel(self, guild: discord.Guild, channel: discord.TextChannel) -> str:
        forms = db.list_forms(guild.id)
        try:
            message = await channel.send(
                embed=embeds.application_panel(guild, forms),
                view=application_panel_view(forms),
            )
        except discord.HTTPException:
            log.warning("could not post application panel in %s", channel.id, exc_info=True)
            return f"⚠️ Couldn't post the panel in {channel.mention}."
        db.save_config(guild.id, apply_panel_message_id=message.id)
        return f"Panel posted: {message.jump_url}"

    # ----------------------------------------------------------------------
    # commands
    # ----------------------------------------------------------------------

    @app_commands.command(name="apply-setup", description="Set up applications")
    @app_commands.describe(
        review_channel="Staff-only channel where applications land for review",
        panel_channel="Where the 'apply here' buttons are posted",
        reviewer_role="Role allowed to accept and deny (admins always can)",
        cooldown_days="Days somebody must wait after being turned down (default 7)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def apply_setup(
        self,
        interaction: discord.Interaction,
        review_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
        reviewer_role: discord.Role | None = None,
        cooldown_days: app_commands.Range[int, 0, 365] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        me = interaction.guild.me
        problems = []
        for channel in (review_channel, panel_channel):
            perms = channel.permissions_for(me)
            for name, label in (("send_messages", "Send Messages"), ("embed_links", "Embed Links")):
                if not getattr(perms, name):
                    problems.append(f"**{label}** in {channel.mention}")
        if not me.guild_permissions.manage_roles:
            problems.append(
                "**Manage Roles** — without it accepting somebody can't actually give them the role"
            )
        if problems:
            await interaction.followup.send(
                embed=embeds.error(
                    "I'm missing permissions:\n" + "\n".join(f"• {p}" for p in problems)
                ),
                ephemeral=True,
            )
            return

        fields = {
            "apply_review_channel_id": review_channel.id,
            "apply_panel_channel_id": panel_channel.id,
        }
        if reviewer_role is not None:
            fields["apply_reviewer_role_id"] = reviewer_role.id
        if cooldown_days is not None:
            fields["apply_cooldown_days"] = int(cooldown_days)
        db.save_config(interaction.guild.id, **fields)

        seeded = self.seed_default_forms(interaction.guild)
        posted = await self.post_panel(interaction.guild, panel_channel)

        extra = ""
        if seeded:
            extra = (
                f"\n\n📄 Added **{'**, **'.join(seeded)}**. "
                "Change the questions or the role with `/apply-form`, and use "
                "`/apply-toggle` to close one.\n"
                "*(Re-running this adds back any of the standard positions you've deleted — "
                "`/apply-form-delete` them again if you don't want them.)*"
            )
        cfg = db.get_config(interaction.guild.id)
        if cfg["applications_channel_id"] and cfg["applications_channel_id"] != panel_channel.id:
            extra += (
                f"\n\nℹ️ Your welcome message points new members at "
                f"<#{cfg['applications_channel_id']}>, but the panel is in "
                f"{panel_channel.mention}. Re-run `/welcome-setup` to point them at the panel."
            )

        await interaction.followup.send(
            embed=embeds.success(
                "**Applications are set up.**\n"
                f"📥 Review channel: {review_channel.mention}\n"
                f"🖱️ Panel: {panel_channel.mention}\n"
                f"👮 Reviewers: {reviewer_role.mention if reviewer_role else '*admins only*'}\n"
                f"⏳ Reapply after: **{self.cooldown_days(interaction.guild.id)}** days\n\n"
                f"{posted}{extra}"
            ),
            ephemeral=True,
        )

    def seed_default_forms(self, guild: discord.Guild) -> list[str]:
        """Create any standard position this server doesn't have yet.

        Not "only on an empty server": positions get added to the list over
        time, and a server that ran setup last month should pick those up by
        re-running it rather than typing five questions by hand. Existing
        positions are left completely alone, so nobody's edits get overwritten.
        """
        existing = {form["key"] for form in db.list_forms(guild.id)}
        added = []
        for form in apply_lib.DEFAULT_FORMS:
            if form["key"] in existing:
                continue
            db.save_form(
                guild.id,
                form["key"],
                form["label"],
                apply_lib.questions_to_json(form["questions"]),
                emoji=form["emoji"],
                blurb=form["blurb"],
                role_id=self._guess_role(guild, form["key"]),
            )
            added.append(form["label"])
        return added

    def _guess_role(self, guild: discord.Guild, key: str) -> int | None:
        """Wire the default positions to roles this bot already knows about.

        /setup has already been told which role builders and script writers have,
        so asking again would be asking twice for the same fact.
        """
        cfg = db.get_config(guild.id)
        if cfg is None:
            return None
        if key == apply_lib.KEY_BUILDER:
            return cfg["builder_role_id"]
        if key == apply_lib.KEY_SCRIPTER:
            return cfg["scripter_role_id"]
        return None

    @app_commands.command(name="apply-panel", description="Post the applications panel again")
    @app_commands.describe(channel="Post it somewhere else than the configured channel")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def apply_panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        cfg = db.get_config(interaction.guild.id)
        target = channel
        if target is None:
            stored = cfg["apply_panel_channel_id"] if cfg else None
            target = interaction.guild.get_channel(stored) if stored else None
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                embed=embeds.error("No panel channel — run `/apply-setup` first."), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is not None:
            db.save_config(interaction.guild.id, apply_panel_channel_id=channel.id)
        result = await self.post_panel(interaction.guild, target)
        await interaction.followup.send(embed=embeds.success(result), ephemeral=True)

    @app_commands.command(name="apply", description="Apply for a position")
    @app_commands.describe(position="What you'd like to apply for")
    @app_commands.guild_only()
    async def apply(self, interaction: discord.Interaction, position: str) -> None:
        form = db.get_form(interaction.guild.id, position)
        blocker = await self.why_cannot_apply(interaction, form)
        if blocker:
            await interaction.response.send_message(
                embed=embeds.notice(blocker), ephemeral=True
            )
            return

        from views import ApplicationModal

        await interaction.response.send_modal(ApplicationModal(form))

    @app_commands.command(name="apply-form", description="Add or edit a position people can apply for")
    @app_commands.describe(
        label="Name of the position, e.g. Builder",
        role="Role given automatically when somebody is accepted",
        emoji="Emoji for the button",
        blurb="One line under the name on the panel",
        question1="First question (max 45 characters)",
        question2="Second question",
        question3="Third question",
        question4="Fourth question",
        question5="Fifth question — Discord allows no more than five",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def apply_form(
        self,
        interaction: discord.Interaction,
        label: str,
        role: discord.Role | None = None,
        emoji: str | None = None,
        blurb: str | None = None,
        question1: str | None = None,
        question2: str | None = None,
        question3: str | None = None,
        question4: str | None = None,
        question5: str | None = None,
    ) -> None:
        key = apply_lib.slug(label)
        existing = db.get_form(interaction.guild.id, key)

        asked = [q for q in (question1, question2, question3, question4, question5) if q]
        if asked:
            problems = apply_lib.validate_questions(asked)
            if problems:
                await interaction.response.send_message(
                    embed=embeds.error(
                        "That form can't be saved:\n" + "\n".join(f"• {p}" for p in problems)
                    ),
                    ephemeral=True,
                )
                return
            # A short question is a one-line box; a long one gets a paragraph box.
            questions = [
                {"label": text, "long": len(text) > 30} for text in asked
            ]
        elif existing is not None:
            questions = apply_lib.questions_from_json(existing["questions"])
        else:
            questions = apply_lib.GENERIC_QUESTIONS

        if emoji is not None and not apply_lib.looks_like_emoji(emoji):
            await interaction.response.send_message(
                embed=embeds.error(
                    f"`{emoji}` isn't an emoji Discord will accept on a button. "
                    "Use a real emoji (😀) or a custom one from this server."
                ),
                ephemeral=True,
            )
            return

        # Anything not passed keeps its current value — omitting a field must not
        # quietly wipe it, which is exactly how a role or blurb goes missing.
        await interaction.response.defer(ephemeral=True, thinking=True)
        db.save_form(
            interaction.guild.id,
            key,
            label,
            apply_lib.questions_to_json(questions),
            emoji=emoji if emoji is not None else (existing["emoji"] if existing else "📄"),
            blurb=blurb if blurb is not None else (existing["blurb"] if existing else None),
            role_id=role.id if role is not None else (existing["role_id"] if existing else None),
        )

        warning = ""
        if role is not None:
            problem = service.missing_role_permissions(interaction.guild, role)
            if problem:
                warning = f"\n\n⚠️ Accepting somebody won't give them {role.mention} yet:\n{problem}"

        saved = db.get_form(interaction.guild.id, key)
        role_line = f"<@&{saved['role_id']}>" if saved["role_id"] else "*none*"
        listed = "\n".join(f"{n}. {q['label']}" for n, q in enumerate(questions, start=1))

        await interaction.followup.send(
            embed=embeds.success(
                f"{'Updated' if existing else 'Added'} **{label}** (`{key}`).\n"
                f"Role on accept: {role_line}\n\n"
                f"**Questions**\n{listed}\n\n"
                f"Run `/apply-panel` to update the buttons.{warning}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="apply-form-delete", description="Remove a position")
    @app_commands.describe(position="Which position to remove")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def apply_form_delete(self, interaction: discord.Interaction, position: str) -> None:
        form = db.get_form(interaction.guild.id, position)
        if form is None:
            await interaction.response.send_message(
                embed=embeds.error("There's no position with that name."), ephemeral=True
            )
            return
        pending = len(
            db.list_applications(interaction.guild.id, db.APPLY_PENDING, form["key"])
        )
        db.delete_form(interaction.guild.id, form["key"])

        note = ""
        if pending:
            note = (
                f"\n\n⚠️ **{pending}** application(s) for it are still waiting. They're kept and "
                "can still be accepted or denied — but accepting won't hand out a role any more."
            )
        await interaction.response.send_message(
            embed=embeds.success(
                f"Removed **{form['label']}**. Run `/apply-panel` to take the button off the panel.{note}"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="apply-toggle", description="Open or close applications for a position")
    @app_commands.describe(position="Which position", open="On to accept applications, off to stop")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def apply_toggle(
        self, interaction: discord.Interaction, position: str, open: bool
    ) -> None:
        form = db.get_form(interaction.guild.id, position)
        if form is None:
            await interaction.response.send_message(
                embed=embeds.error("There's no position with that name."), ephemeral=True
            )
            return
        db.set_form_open(interaction.guild.id, form["key"], open)
        await interaction.response.send_message(
            embed=embeds.success(
                f"**{form['label']}** applications are now **{'open' if open else 'closed'}**.\n"
                "Run `/apply-panel` so the panel says so too."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="applications", description="List applications")
    @app_commands.describe(status="Which ones to show — defaults to those still waiting")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Waiting for a decision", value=db.APPLY_PENDING),
            app_commands.Choice(name="Accepted", value=db.APPLY_ACCEPTED),
            app_commands.Choice(name="Not accepted", value=db.APPLY_DENIED),
            app_commands.Choice(name="All", value="all"),
        ]
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def applications_cmd(
        self, interaction: discord.Interaction, status: str | None = None
    ) -> None:
        if not self.is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("Only staff can see applications."), ephemeral=True
            )
            return

        wanted = status or db.APPLY_PENDING
        rows = db.list_applications(
            interaction.guild.id, None if wanted == "all" else wanted
        )
        heading = {
            db.APPLY_PENDING: "Waiting for a decision",
            db.APPLY_ACCEPTED: "Accepted",
            db.APPLY_DENIED: "Not accepted",
        }.get(wanted, "All applications")
        await interaction.response.send_message(
            embed=embeds.application_list(interaction.guild, rows, heading), ephemeral=True
        )

    @app_commands.command(name="application", description="Show one application in full")
    @app_commands.describe(number="The application number, e.g. 3")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def application_cmd(self, interaction: discord.Interaction, number: int) -> None:
        if not self.is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=embeds.error("Only staff can see applications."), ephemeral=True
            )
            return

        application = db.get_application(number)
        if application is None or application["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(
                embed=embeds.error(f"There's no application #{number:04d}."), ephemeral=True
            )
            return

        applicant = interaction.guild.get_member(application["applicant_id"])
        view = (
            application_review_view(number)
            if application["status"] == db.APPLY_PENDING
            else None
        )
        await interaction.response.send_message(
            embed=embeds.application_card(application, applicant), view=view, ephemeral=True
        )

    # Autocomplete for every command that takes a position.
    apply.autocomplete("position")(form_autocomplete)
    apply_form_delete.autocomplete("position")(form_autocomplete)
    apply_toggle.autocomplete("position")(form_autocomplete)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ApplicationsCog(bot))

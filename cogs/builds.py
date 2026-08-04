"""The build workflow: request → claim → update → handoff or finish."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
import embeds
import service
from views import ConfirmDeleteView, RequestModal

STATUS_CHOICES = [
    app_commands.Choice(name="Still building — this stays mine", value=db.KIND_PROGRESS),
    app_commands.Choice(name="Handing off — someone else can continue", value=db.KIND_HANDOFF),
    app_commands.Choice(name="Finished — the whole build is done", value=db.KIND_COMPLETE),
]


def _label(build) -> str:
    return f"#{build['id']} {build['title']}"[:100]


class BuildsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ----------------------------------------------------------------------
    # autocomplete
    # ----------------------------------------------------------------------

    async def _autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        mine_first: bool,
        include_finished: bool = False,
    ) -> list[app_commands.Choice[int]]:
        if interaction.guild is None:
            return []
        builds = list(db.list_builds(interaction.guild.id))
        if not include_finished:
            # Finished builds are noise when you're picking something to work on.
            builds = [b for b in builds if b["status"] != db.STATUS_COMPLETE]
        if mine_first:
            mine = [b for b in builds if b["claimed_by"] == interaction.user.id]
            others = [b for b in builds if b["claimed_by"] != interaction.user.id]
            builds = mine + others

        needle = current.lower().lstrip("#")
        if needle:
            builds = [
                b for b in builds if needle in b["title"].lower() or needle in str(b["id"])
            ]
        return [app_commands.Choice(name=_label(b), value=b["id"]) for b in builds[:25]]

    async def my_builds_autocomplete(self, interaction, current: str):
        return await self._autocomplete(interaction, current, mine_first=True)

    async def any_build_autocomplete(self, interaction, current: str):
        return await self._autocomplete(interaction, current, mine_first=False)

    async def deletable_build_autocomplete(self, interaction, current: str):
        # Includes finished builds — clearing out old ones is the main reason to delete.
        return await self._autocomplete(
            interaction, current, mine_first=False, include_finished=True
        )

    # ----------------------------------------------------------------------
    # script writer side
    # ----------------------------------------------------------------------

    @app_commands.command(name="request", description="Describe a build that needs to be made")
    @app_commands.guild_only()
    async def request(self, interaction: discord.Interaction) -> None:
        try:
            service.require_config(interaction.guild.id)
            service.check_scripter(interaction.user)
        except config.ConfigError as exc:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )
            return
        await interaction.response.send_modal(RequestModal())

    # ----------------------------------------------------------------------
    # builder side
    # ----------------------------------------------------------------------

    @app_commands.command(name="claim", description="Take an open build so nobody doubles up on it")
    @app_commands.describe(build="Which build to claim")
    @app_commands.autocomplete(build=any_build_autocomplete)
    @app_commands.guild_only()
    async def claim(self, interaction: discord.Interaction, build: int) -> None:
        try:
            await service.do_claim(interaction, build)
        except config.ConfigError as exc:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )

    @app_commands.command(
        name="update", description="Post your progress on a build, with a schematic"
    )
    @app_commands.describe(
        build="Which build you're updating",
        status="What this update means for the build",
        file="Your schematic (.schem, .schematic, .litematic, .nbt or .zip)",
        note="What you did, or what's left to do",
    )
    @app_commands.choices(status=STATUS_CHOICES)
    @app_commands.autocomplete(build=my_builds_autocomplete)
    @app_commands.guild_only()
    async def update(
        self,
        interaction: discord.Interaction,
        build: int,
        status: app_commands.Choice[str],
        file: discord.Attachment | None = None,
        note: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        row = db.get_build(build)
        if row is None or row["guild_id"] != interaction.guild.id:
            await interaction.followup.send(
                embed=embeds.error(f"Build #{build} doesn't exist."), ephemeral=True
            )
            return

        if row["status"] == db.STATUS_COMPLETE:
            await interaction.followup.send(
                embed=embeds.error(f"Build #{build} is already finished."), ephemeral=True
            )
            return

        # You must hold the build to post on it — that is what stops two people
        # working the same thing and then both uploading over each other.
        is_holder = row["claimed_by"] == interaction.user.id
        if not is_holder and not config.is_admin(interaction.user):
            if row["status"] == db.STATUS_OPEN:
                msg = f"Claim #{build} first with `/claim build:{build}` — then you can post updates."
            else:
                msg = f"<@{row['claimed_by']}> is building #{build}, so only they can post updates."
            await interaction.followup.send(embed=embeds.error(msg), ephemeral=True)
            return

        stored_path = stored_name = None
        if file is not None:
            try:
                stored_path, stored_name = await service.store_attachment(build, file)
            except config.ConfigError as exc:
                await interaction.followup.send(embed=embeds.error(str(exc)), ephemeral=True)
                return

        kind = status.value
        db.add_update(
            build_id=build,
            builder_id=interaction.user.id,
            kind=kind,
            note=note,
            file_name=stored_name,
            file_path=stored_path,
        )

        if kind == db.KIND_HANDOFF:
            db.release_build(build, None)
        elif kind == db.KIND_COMPLETE:
            db.complete_build(build)

        row = db.get_build(build)

        # Re-attach the file in the thread so the history is browsable in Discord,
        # while /schematic serves the copy on disk once these links expire.
        thread_kwargs: dict = {
            "embed": embeds.update_post(row, interaction.user, kind, note, stored_name)
        }
        if stored_path:
            thread_kwargs["file"] = discord.File(stored_path, filename=stored_name)
        if kind == db.KIND_HANDOFF:
            builder_role = service.builder_role(interaction.guild)
            if builder_role is not None:
                thread_kwargs["content"] = f"{builder_role.mention} — this build is open again."
        await service.post_to_thread(self.bot, build, **thread_kwargs)

        if kind == db.KIND_COMPLETE:
            await service.archive_thread(self.bot, build)

        await service.refresh_card(self.bot, build)
        service.schedule_board_refresh(self.bot, interaction.guild)

        if kind == db.KIND_PROGRESS:
            confirmation = f"Progress saved on **#{build}** — it's still yours."
        elif kind == db.KIND_HANDOFF:
            confirmation = f"**#{build}** is open again — the next builder continues from your file."
        else:
            confirmation = f"**#{build}** marked finished. Nice one."

        if stored_name is None and kind != db.KIND_PROGRESS and db.schematic_count(build) == 0:
            confirmation += (
                "\n\n⚠️ No schematic has ever been uploaded for this build, so the next "
                "builder has nothing to continue from. Consider running `/update` again "
                "with a file attached."
            )

        await interaction.followup.send(embed=embeds.success(confirmation), ephemeral=True)

    @app_commands.command(name="release", description="Give a build back without uploading anything")
    @app_commands.describe(build="Which build to release")
    @app_commands.autocomplete(build=my_builds_autocomplete)
    @app_commands.guild_only()
    async def release(self, interaction: discord.Interaction, build: int) -> None:
        try:
            await service.do_release(interaction, build)
        except config.ConfigError as exc:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )

    @app_commands.command(name="delete", description="Delete a build request for good")
    @app_commands.describe(build="Which build to delete")
    @app_commands.autocomplete(build=deletable_build_autocomplete)
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction, build: int) -> None:
        row = db.get_build(build)
        if row is None or row["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(
                embed=embeds.error(f"Build #{build} doesn't exist."), ephemeral=True
            )
            return

        try:
            service.check_can_delete(interaction.user, row)
        except config.ConfigError as exc:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )
            return

        # Spell out what disappears. Deleting a build with uploaded schematics
        # throws away real work, and there is no undo.
        losses = ["the request and its discussion thread"]
        files = db.schematic_count(build)
        if files:
            losses.append(f"**{files} uploaded schematic{'s' if files != 1 else ''}**")
        if row["status"] == db.STATUS_CLAIMED:
            losses.append(f"<@{row['claimed_by']}>'s claim on it")

        if len(losses) == 1:
            summary = losses[0]
        else:
            summary = f"{', '.join(losses[:-1])} and {losses[-1]}"

        warning = embeds.notice(
            f"Delete **#{build} {row['title']}**?\n\n"
            f"This permanently removes {summary}.\n"
            "It cannot be undone."
        )
        await interaction.response.send_message(
            embed=warning,
            view=ConfirmDeleteView(build, interaction.user.id),
            ephemeral=True,
        )

    # ----------------------------------------------------------------------
    # looking things up
    # ----------------------------------------------------------------------

    @app_commands.command(name="builds", description="List builds and who's on them")
    @app_commands.describe(status="Only show builds in this state")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="🟡 Open — needs a builder", value=db.STATUS_OPEN),
            app_commands.Choice(name="🔨 Being built", value=db.STATUS_CLAIMED),
            app_commands.Choice(name="✅ Finished", value=db.STATUS_COMPLETE),
        ]
    )
    @app_commands.guild_only()
    async def builds(
        self, interaction: discord.Interaction, status: app_commands.Choice[str] | None = None
    ) -> None:
        rows = db.list_builds(interaction.guild.id, status.value if status else None)
        if not rows:
            await interaction.response.send_message(
                embed=embeds.notice("Nothing here yet. Script writers can post one with `/request`."),
                ephemeral=True,
            )
            return

        icons = {db.STATUS_OPEN: "🟡", db.STATUS_CLAIMED: "🔨", db.STATUS_COMPLETE: "✅"}
        lines = []
        for row in rows[:40]:
            who = f" — <@{row['claimed_by']}>" if row["claimed_by"] else ""
            version = embeds.version_label(row["id"])
            file_note = f" · ⬇ {version}" if version else ""
            lines.append(f"{icons[row['status']]} `#{row['id']}` **{row['title']}**{who}{file_note}")

        title = f"Builds ({status.name})" if status else "All builds"
        embed = discord.Embed(
            title=title, description="\n".join(lines)[:4000], color=discord.Color.blurple()
        )
        if len(rows) > 40:
            embed.set_footer(text=f"Showing 40 of {len(rows)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="build", description="Show everything about one build")
    @app_commands.describe(build="Which build to show")
    @app_commands.autocomplete(build=any_build_autocomplete)
    @app_commands.guild_only()
    async def build_info(self, interaction: discord.Interaction, build: int) -> None:
        row = db.get_build(build)
        if row is None or row["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(
                embed=embeds.error(f"Build #{build} doesn't exist."), ephemeral=True
            )
            return

        embed = embeds.build_card(row)
        history = db.list_updates(build)
        if history:
            verbs = {
                db.KIND_PROGRESS: "progress",
                db.KIND_HANDOFF: "handed off",
                db.KIND_COMPLETE: "finished",
            }
            lines = []
            for entry in history[-10:]:
                ts = db.to_unix(entry["created_at"])
                when = f"<t:{ts}:R>" if ts else ""
                has_file = " ⬇" if entry["file_path"] else ""
                lines.append(
                    f"• <@{entry['builder_id']}> — {verbs[entry['kind']]}{has_file} {when}"
                )
            embed.add_field(name="History", value="\n".join(lines)[:1024], inline=False)

        if row["message_id"] and row["thread_id"]:
            embed.add_field(
                name="Discussion",
                value=f"<#{row['thread_id']}>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="schematic", description="Download the latest schematic for a build")
    @app_commands.describe(build="Which build's schematic you want")
    @app_commands.autocomplete(build=any_build_autocomplete)
    @app_commands.guild_only()
    async def schematic(self, interaction: discord.Interaction, build: int) -> None:
        try:
            await service.send_latest_schematic(interaction, build)
        except config.ConfigError as exc:
            await interaction.response.send_message(
                embed=embeds.error(str(exc)), ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BuildsCog(bot))

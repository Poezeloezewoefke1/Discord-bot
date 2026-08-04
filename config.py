"""Configuration and shared permission helpers.

Secrets come from the environment (.env). Everything else — which roles count as
builders/scripters, which channels to post in — is configured live in Discord via
/setup and stored in the database, so nobody has to edit files to move a channel.
"""

from __future__ import annotations

import os
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.getenv("DISCORD_TOKEN", "")

# Optional. When set, slash commands sync to just this guild and appear instantly.
# Without it they sync globally and can take up to an hour to show up.
_raw_guild_id = os.getenv("GUILD_ID", "").strip()
GUILD_ID: int | None = int(_raw_guild_id) if _raw_guild_id.isdigit() else None

DB_PATH = Path(os.getenv("DB_PATH") or BASE_DIR / "buildboard.db")
SCHEMATIC_DIR = Path(os.getenv("SCHEMATIC_DIR") or BASE_DIR / "schematics")

# Minecraft schematic formats, plus .zip for builders who bundle several files.
ALLOWED_EXTENSIONS = {".schem", ".schematic", ".litematic", ".nbt", ".zip"}

# Discord's own upload cap for a bot on a non-boosted server.
MAX_FILE_BYTES = 25 * 1024 * 1024

# Board is edited in place; coalesce bursts of updates into one edit per interval.
BOARD_REFRESH_INTERVAL = 5.0

# How long this process is expected to run before its host restarts it. Only used
# to tell people when to expect a gap; the workflow sets it to match its own
# timeout, and a test pins the two together so they can't drift apart.
SHIFT_MINUTES = int(os.getenv("SHIFT_MINUTES") or 0)

# How far back the board's "recently finished" section looks.
FINISHED_WINDOW_DAYS = 7

COLOR_OPEN = discord.Color.from_str("#f0b232")      # yellow — needs a builder
COLOR_CLAIMED = discord.Color.from_str("#5865f2")   # blurple — being built
COLOR_DONE = discord.Color.from_str("#57f287")      # green — finished
COLOR_ERROR = discord.Color.from_str("#ed4245")     # red — rejected action
COLOR_GOODBYE = discord.Color.from_str("#e57373")   # soft red — someone left



class ConfigError(Exception):
    """Raised when the bot is asked to act before /setup has been run."""


def is_admin(member: discord.Member) -> bool:
    """Admins bypass every role gate so a server owner is never locked out."""
    return member.guild_permissions.manage_guild


def has_role(member: discord.Member, role_id: int | None) -> bool:
    """True if the member may act as this kind of user.

    Admins always pass. If the role hasn't been configured yet the gate is closed
    for everyone else — the caller is expected to tell them to run /setup.
    """
    if is_admin(member):
        return True
    if role_id is None:
        return False
    return any(role.id == role_id for role in member.roles)


def extension_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def is_allowed_file(filename: str) -> bool:
    return extension_of(filename) in ALLOWED_EXTENSIONS


def allowed_extensions_text() -> str:
    return ", ".join(f"`{ext}`" for ext in sorted(ALLOWED_EXTENSIONS))


def build_storage_dir(build_id: int) -> Path:
    return SCHEMATIC_DIR / str(build_id)

"""Fetch the bot's data on a host that starts it with a blank folder.

On GitHub Actions a workflow step ran `sync_state.py restore` before the bot.
A panel host (Pella, Pterodactyl, and friends) just runs `python bot.py`, so
there is nowhere to put that step — and a bot that boots with an empty database
looks exactly like a brand-new one: no builds, no tickets, no applications, and
an admin being told to run /setup again.

So the bot does it itself, once, and only when there is nothing to lose:

  * If a database already exists, this does nothing at all. Never overwrite
    live data with a copy from the branch — that copy is a backup, not the
    truth, and on a host with a real disk the file on disk is the truth.
  * If the fetch fails, say so loudly and carry on. Refusing to start would
    leave a panel host restart-looping forever, which is harder to diagnose
    than an empty bot with a clear message in the log.

The state branch is in a public repo, so this needs no token.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SYNC = HERE / "sync_state.py"

# Where the data lives when nothing says otherwise. Public, so cloning it needs
# no credentials — which is what makes this work on a host we don't configure.
DEFAULT_REMOTE = "https://github.com/Poezeloezewoefke1/Discord-bot.git"

TIMEOUT_SECONDS = 120


def _probe(remote: str, branch: str) -> tuple[bool, bool]:
    """(could we reach it, does the branch exist). One cheap network round trip."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", remote, branch],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, False
    if result.returncode != 0:
        return False, False
    return True, bool(result.stdout.strip())


def restore_if_empty(db_path: Path, schematic_dir: Path) -> bool:
    """Populate a blank install from the state branch. True if it fetched anything."""
    if os.getenv("SKIP_STATE_RESTORE"):
        log.info("SKIP_STATE_RESTORE is set — not fetching saved data")
        return False

    if db_path.exists():
        return False

    remote = os.getenv("STATE_REMOTE") or DEFAULT_REMOTE
    branch = os.getenv("STATE_BRANCH") or "bot-data"
    log.info("no database at %s — fetching the saved one from the state branch", db_path)

    # Ask first whether the branch is even there. sync_state treats a failed
    # clone as "no branch yet, this is a normal first run", which is true on a
    # brand-new server and badly wrong during a move to a new host: the bot comes
    # up empty and the log says everything is fine.
    reachable, has_branch = _probe(remote, branch)
    if not reachable:
        log.warning(
            "couldn't reach %s to fetch the saved data. The bot is starting with an "
            "empty database — do NOT run /setup again until someone has checked, or "
            "you may end up with two sets of data.",
            remote,
        )
        return False
    if not has_branch:
        log.info("no '%s' branch there yet — this really is a fresh start", branch)
        return False

    environment = {
        **os.environ,
        "STATE_REMOTE": remote,
        "DB_PATH": str(db_path),
        "SCHEMATIC_DIR": str(schematic_dir),
        "STATE_DIR": str(db_path.parent / ".state"),
    }

    try:
        result = subprocess.run(
            [sys.executable, str(SYNC), "restore"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        log.warning("git isn't installed here, so saved data can't be fetched")
        return False
    except subprocess.TimeoutExpired:
        log.warning("fetching saved data took over %ss — starting without it", TIMEOUT_SECONDS)
        return False

    for line in (result.stdout or "").splitlines():
        log.info("%s", line)

    if result.returncode != 0:
        # Deliberately not fatal. See the module docstring.
        log.warning(
            "could not fetch saved data (git exited %s). The bot is starting with an "
            "empty database — do NOT run /setup again until someone has checked, or "
            "you may end up with two sets of data. Details: %s",
            result.returncode,
            (result.stderr or "").strip().splitlines()[-1:] or "no output",
        )
        return False

    if db_path.exists():
        log.info("restored the saved database (%s bytes)", f"{db_path.stat().st_size:,}")
        return True

    log.info("the state branch has no database yet — this really is a fresh start")
    return False

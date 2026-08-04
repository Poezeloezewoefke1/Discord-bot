"""Keep the bot's database and schematics in a branch of this repo.

GitHub Actions gives every run a clean disk, so without this the bot would forget
every claim and lose every uploaded schematic each time it restarts. This script
copies that state into a dedicated branch (default `bot-data`) and back out again.

    python hosting/sync_state.py restore    # branch -> working files, at startup
    python hosting/sync_state.py save       # working files -> branch, periodically

It deliberately knows nothing about the bot: it moves two paths given to it by
environment variables, so the bot itself stays hosting-agnostic.

Environment:
    DB_PATH          database to sync          (default buildboard.db)
    SCHEMATIC_DIR    schematic folder to sync  (default schematics)
    STATE_BRANCH     branch to store state in  (default bot-data)
    STATE_REMOTE     git remote URL            (default: built from GITHUB_REPOSITORY
                     + GITHUB_TOKEN; overridable so tests can use a local repo)
    STATE_DIR        scratch checkout location (default .state)
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH") or "buildboard.db")
SCHEMATIC_DIR = Path(os.getenv("SCHEMATIC_DIR") or "schematics")
STATE_BRANCH = os.getenv("STATE_BRANCH") or "bot-data"
STATE_DIR = Path(os.getenv("STATE_DIR") or ".state")

# Names inside the state branch.
DB_NAME = "buildboard.db"
SCHEMATIC_NAME = "schematics"
STAMP_NAME = "STATE.txt"

COMMIT_NAME = os.getenv("STATE_COMMIT_NAME") or "build-board-bot"
COMMIT_EMAIL = os.getenv("STATE_COMMIT_EMAIL") or "bot@users.noreply.github.com"


def log(message: str) -> None:
    print(f"[state] {message}", flush=True)


def remote_url() -> str:
    """Where the state branch lives.

    Tests set STATE_REMOTE to a local bare repo. On Actions it is built from the
    token, which is never printed — only the repo slug is.
    """
    explicit = os.getenv("STATE_REMOTE")
    if explicit:
        return explicit

    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        sys.exit(
            "No STATE_REMOTE, and GITHUB_REPOSITORY/GITHUB_TOKEN are not both set.\n"
            "This script expects to run inside GitHub Actions, or to be told a remote."
        )
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").removeprefix("https://")
    return f"https://x-access-token:{token}@{server}/{repo}.git"


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def restore() -> int:
    """Pull the state branch into place. A missing branch is a normal first run."""
    url = remote_url()

    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)

    result = git(
        "clone", "--depth", "1", "--single-branch", "--branch", STATE_BRANCH,
        url, str(STATE_DIR), check=False,
    )
    if result.returncode != 0:
        log(f"no '{STATE_BRANCH}' branch yet — starting with empty state (this is normal "
            f"on the very first run)")
        return 0

    stored_db = STATE_DIR / DB_NAME
    if stored_db.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored_db, DB_PATH)
        log(f"restored database ({stored_db.stat().st_size:,} bytes)")
    else:
        log("branch has no database yet")

    stored_schematics = STATE_DIR / SCHEMATIC_NAME
    if stored_schematics.is_dir():
        if SCHEMATIC_DIR.exists():
            shutil.rmtree(SCHEMATIC_DIR)
        shutil.copytree(stored_schematics, SCHEMATIC_DIR)
        count = sum(1 for p in SCHEMATIC_DIR.rglob("*") if p.is_file())
        log(f"restored {count} schematic file(s)")
    else:
        SCHEMATIC_DIR.mkdir(parents=True, exist_ok=True)
        log("branch has no schematics yet")

    return 0


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------

def snapshot_database(source: Path, destination: Path) -> None:
    """Copy a *live* SQLite database safely.

    The bot is writing to this file while we copy it. A plain file copy can catch
    a half-written page and produce a database that won't open, so use SQLite's
    own backup API, which takes a consistent snapshot of an in-use database.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def fingerprint() -> str:
    """A hash of everything we sync, so unchanged state produces no commit."""
    digest = hashlib.sha256()

    if DB_PATH.exists():
        # Hash a consistent snapshot rather than the live file, whose page layout
        # can differ between reads without any actual data having changed.
        tmp = STATE_DIR.parent / ".state-fingerprint.db"
        try:
            snapshot_database(DB_PATH, tmp)
            digest.update(tmp.read_bytes())
        finally:
            tmp.unlink(missing_ok=True)

    if SCHEMATIC_DIR.is_dir():
        for path in sorted(SCHEMATIC_DIR.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(SCHEMATIC_DIR)).encode())
                digest.update(path.read_bytes())

    return digest.hexdigest()


def save() -> int:
    """Push current state to the state branch, unless nothing changed."""
    if not DB_PATH.exists() and not SCHEMATIC_DIR.exists():
        log("nothing to save yet")
        return 0

    url = remote_url()
    current = fingerprint()

    # The previous fingerprint travels in the branch itself, so this works even
    # though every run starts on a fresh machine.
    previous = None
    stamp = STATE_DIR / STAMP_NAME
    if stamp.exists():
        for line in stamp.read_text().splitlines():
            if line.startswith("fingerprint: "):
                previous = line.removeprefix("fingerprint: ").strip()

    if previous == current:
        log("no changes since last save")
        return 0

    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    STATE_DIR.mkdir(parents=True)

    if DB_PATH.exists():
        snapshot_database(DB_PATH, STATE_DIR / DB_NAME)

    if SCHEMATIC_DIR.is_dir():
        shutil.copytree(SCHEMATIC_DIR, STATE_DIR / SCHEMATIC_NAME)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    file_count = sum(1 for p in (STATE_DIR / SCHEMATIC_NAME).rglob("*") if p.is_file()) \
        if (STATE_DIR / SCHEMATIC_NAME).is_dir() else 0
    stamp.write_text(
        "Build board bot state. Written automatically — do not edit by hand.\n"
        f"saved: {now}\n"
        f"schematics: {file_count}\n"
        f"fingerprint: {current}\n"
    )

    # Rewrite the branch as a single commit every time. Keeping history would mean
    # storing the whole binary database again every few minutes, forever; the
    # history of a state blob is worth nothing, only the latest copy is.
    git("init", "--quiet", "--initial-branch", STATE_BRANCH, cwd=STATE_DIR)
    git("config", "user.name", COMMIT_NAME, cwd=STATE_DIR)
    git("config", "user.email", COMMIT_EMAIL, cwd=STATE_DIR)
    git("add", "-A", cwd=STATE_DIR)
    git("commit", "--quiet", "-m", f"Bot state {now}", cwd=STATE_DIR)
    git("remote", "add", "origin", url, cwd=STATE_DIR)

    push = git("push", "--force", "origin", f"{STATE_BRANCH}:{STATE_BRANCH}",
               cwd=STATE_DIR, check=False)
    if push.returncode != 0:
        # Never echo the remote URL — it carries the token.
        log("push failed:")
        log((push.stderr or push.stdout).replace(url, "<remote>").strip()[:500])
        return 1

    log(f"saved state ({file_count} schematic file(s)) to '{STATE_BRANCH}'")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("restore", "save"):
        sys.exit("usage: sync_state.py [restore|save]")
    return restore() if sys.argv[1] == "restore" else save()


if __name__ == "__main__":
    sys.exit(main())

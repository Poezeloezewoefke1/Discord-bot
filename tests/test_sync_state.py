"""State persistence, tested against a real git repo in a temp directory.

The bot restarts every few hours on GitHub Actions, so "state survives a restart"
is the property that has to hold. These tests do the actual round trip through a
real bare repo rather than mocking git.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "hosting" / "sync_state.py"

sys.path.insert(0, str(ROOT))

import db  # noqa: E402

GUILD = 4242
SCRIPTER = 7001
BUILDER_A = 8001
BUILDER_B = 8002


@pytest.fixture
def workspace(tmp_path):
    """A bare 'remote', a working directory, and the environment tying them together."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)

    work = tmp_path / "work"
    work.mkdir()

    env = {
        **os.environ,
        "DB_PATH": str(work / "buildboard.db"),
        "SCHEMATIC_DIR": str(work / "schematics"),
        "STATE_DIR": str(work / ".state"),
        "STATE_REMOTE": str(remote),
        "STATE_BRANCH": "bot-data",
    }
    return type("Workspace", (), {"remote": remote, "work": work, "env": env})()


def sync(mode: str, workspace, expect_success: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(SYNC), mode],
        env=workspace.env,
        capture_output=True,
        text=True,
        cwd=workspace.work,
    )
    if expect_success:
        assert result.returncode == 0, f"{mode} failed:\n{result.stdout}\n{result.stderr}"
    return result


def seed_database(workspace) -> tuple[int, int]:
    """Create the kind of state a real server would have: a claim and a handoff."""
    db.set_db_path(Path(workspace.env["DB_PATH"]))
    db.init_db()
    db.save_config(GUILD, builder_role_id=11, scripter_role_id=22, requests_channel_id=33)

    claimed = db.create_build(GUILD, "Medieval spawn", "30x30 stone and oak", SCRIPTER)
    db.claim_build(claimed, BUILDER_A)

    handed_off = db.create_build(GUILD, "Shop district", "10 stalls", SCRIPTER)
    db.claim_build(handed_off, BUILDER_A)
    db.add_update(handed_off, BUILDER_A, db.KIND_HANDOFF, "stalls 1-4", "s.schem", "/x/v1.schem")
    db.release_build(handed_off, None)

    return claimed, handed_off


def seed_schematics(workspace) -> dict[str, bytes]:
    """Schematics are binary; write real bytes so a byte-for-byte check means something."""
    folder = Path(workspace.env["SCHEMATIC_DIR"])
    (folder / "2").mkdir(parents=True, exist_ok=True)
    files = {
        "2/v1_spawn.schem": bytes(range(256)) * 40,
        "2/v2_spawn.schem": b"\x00\x01\x02NBT-ish payload\xff\xfe" * 100,
    }
    for name, payload in files.items():
        (folder / name).write_bytes(payload)
    return files


# --------------------------------------------------------------------------
# the property that matters
# --------------------------------------------------------------------------

def test_state_survives_a_full_restart(workspace):
    claimed, handed_off = seed_database(workspace)
    files = seed_schematics(workspace)

    sync("save", workspace)

    # Wipe everything the way a fresh Actions runner would.
    Path(workspace.env["DB_PATH"]).unlink()
    shutil.rmtree(workspace.env["SCHEMATIC_DIR"])
    shutil.rmtree(workspace.env["STATE_DIR"])

    sync("restore", workspace)

    db.set_db_path(Path(workspace.env["DB_PATH"]))

    still_claimed = db.get_build(claimed)
    assert still_claimed["status"] == db.STATUS_CLAIMED
    assert still_claimed["claimed_by"] == BUILDER_A, "a claim must survive a restart"
    assert still_claimed["title"] == "Medieval spawn"

    reopened = db.get_build(handed_off)
    assert reopened["status"] == db.STATUS_OPEN
    assert db.latest_schematic(handed_off)["file_path"] == "/x/v1.schem"

    cfg = db.get_config(GUILD)
    assert cfg["builder_role_id"] == 11, "/setup must not need re-running after a restart"

    folder = Path(workspace.env["SCHEMATIC_DIR"])
    for name, payload in files.items():
        assert (folder / name).read_bytes() == payload, f"{name} came back corrupted"


def test_restore_with_no_branch_yet_is_not_an_error(workspace):
    """The very first run has nothing to restore. That must not be a failure."""
    result = sync("restore", workspace)
    assert "no 'bot-data' branch yet" in result.stdout
    assert not Path(workspace.env["DB_PATH"]).exists()


def test_save_with_nothing_to_save_is_not_an_error(workspace):
    result = sync("save", workspace)
    assert "nothing to save" in result.stdout


# --------------------------------------------------------------------------
# not thrashing the repo
# --------------------------------------------------------------------------

def _remote_commit_count(workspace) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "bot-data"],
        cwd=workspace.remote, capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


def test_unchanged_state_produces_no_new_commit(workspace):
    seed_database(workspace)
    seed_schematics(workspace)

    sync("save", workspace)
    second = sync("save", workspace)

    assert "no changes since last save" in second.stdout


def test_branch_keeps_exactly_one_commit(workspace):
    """History of a state blob is worthless; storing it would bloat the repo."""
    seed_database(workspace)
    sync("save", workspace)
    assert _remote_commit_count(workspace) == 1

    db.set_db_path(Path(workspace.env["DB_PATH"]))
    for i in range(3):
        db.create_build(GUILD, f"Build {i}", "spec", SCRIPTER)
        sync("save", workspace)

    assert _remote_commit_count(workspace) == 1, "the state branch must not accumulate history"


def test_changes_actually_reach_the_remote(workspace):
    seed_database(workspace)
    sync("save", workspace)

    db.set_db_path(Path(workspace.env["DB_PATH"]))
    new_id = db.create_build(GUILD, "Nether hub", "6 portals", SCRIPTER)
    db.claim_build(new_id, BUILDER_B)
    sync("save", workspace)

    Path(workspace.env["DB_PATH"]).unlink()
    shutil.rmtree(workspace.env["STATE_DIR"])
    sync("restore", workspace)

    db.set_db_path(Path(workspace.env["DB_PATH"]))
    assert db.get_build(new_id)["claimed_by"] == BUILDER_B


# --------------------------------------------------------------------------
# copying a database that is being written to
# --------------------------------------------------------------------------

def test_snapshot_of_a_database_under_write_is_valid(workspace):
    """The bot writes while the sync runs, so the copy must never be torn."""
    seed_database(workspace)
    seed_schematics(workspace)

    live = sqlite3.connect(workspace.env["DB_PATH"])
    try:
        # Hold an open write transaction across the save.
        live.execute("BEGIN")
        live.execute(
            "INSERT INTO builds (guild_id, title, description, requested_by, status, created_at)"
            " VALUES (?, ?, ?, ?, 'open', '2026-01-01T00:00:00+00:00')",
            (GUILD, "Uncommitted", "should not appear", SCRIPTER),
        )
        sync("save", workspace)
    finally:
        live.rollback()
        live.close()

    Path(workspace.env["DB_PATH"]).unlink()
    shutil.rmtree(workspace.env["STATE_DIR"])
    sync("restore", workspace)

    restored = sqlite3.connect(workspace.env["DB_PATH"])
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        titles = [r[0] for r in restored.execute("SELECT title FROM builds")]
        assert "Medieval spawn" in titles
        assert "Uncommitted" not in titles, "a rolled-back write must not be in the snapshot"
    finally:
        restored.close()


def test_token_is_never_printed_on_failure(workspace):
    """A push failure must not leak the credential embedded in the remote URL."""
    seed_database(workspace)
    workspace.env["STATE_REMOTE"] = "https://x-access-token:supersecret@example.invalid/x/y.git"

    result = sync("save", workspace, expect_success=False)
    assert result.returncode != 0
    assert "supersecret" not in result.stdout
    assert "supersecret" not in result.stderr


# --------------------------------------------------------------------------
# the workflows themselves
# --------------------------------------------------------------------------

def _workflow(name: str) -> dict:
    text = (ROOT / ".github" / "workflows" / name).read_text()
    return yaml.safe_load(text)


def test_workflows_are_valid_yaml():
    for name in ("bot.yml", "tests.yml"):
        assert isinstance(_workflow(name), dict), f"{name} did not parse"


def test_bot_workflow_never_runs_two_bots_at_once():
    workflow = _workflow("bot.yml")
    concurrency = workflow["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False, (
        "cancelling the running bot to start a new one would drop its unsaved state"
    )


def test_bot_workflow_timeouts_stay_in_order():
    """bot timeout < job timeout < GitHub's 360m kill, or state never gets saved."""
    workflow = _workflow("bot.yml")
    job = workflow["jobs"]["run"]

    job_timeout = job["timeout-minutes"]
    assert job_timeout < 360, "the job must finish before GitHub's 6h hard kill"

    script = "\n".join(
        step["run"] for step in job["steps"] if isinstance(step.get("run"), str)
    )
    match = [line for line in script.splitlines() if "timeout --signal=INT" in line]
    assert match, "the bot should be launched under `timeout` so shutdown is graceful"

    minutes = int(match[0].split("--kill-after=60s")[1].split("m")[0].strip())
    assert minutes < job_timeout, (
        f"bot timeout ({minutes}m) must leave room before the job timeout ({job_timeout}m) "
        "for the final state save"
    )


def test_shift_length_matches_the_actual_timeout():
    """/test tells people when the restart is due — that number must be real."""
    workflow = _workflow("bot.yml")
    job = workflow["jobs"]["run"]

    declared = int(job["env"]["SHIFT_MINUTES"])

    script = "\n".join(
        step["run"] for step in job["steps"] if isinstance(step.get("run"), str)
    )
    line = [ln for ln in script.splitlines() if "timeout --signal=INT" in ln][0]
    actual = int(line.split("--kill-after=60s")[1].split("m")[0].strip())

    assert declared == actual, (
        f"SHIFT_MINUTES says {declared} but the bot is killed after {actual}m — "
        "/test would report the wrong time until restart"
    )


def test_bot_workflow_can_write_the_state_branch():
    workflow = _workflow("bot.yml")
    assert workflow["permissions"]["contents"] == "write"


def test_state_is_always_saved_even_if_the_bot_crashes():
    workflow = _workflow("bot.yml")
    save_steps = [
        step for step in workflow["jobs"]["run"]["steps"]
        if "sync_state.py save" in str(step.get("run", ""))
    ]
    assert any(step.get("if") == "always()" for step in save_steps), (
        "a crash must still push whatever state was reached"
    )

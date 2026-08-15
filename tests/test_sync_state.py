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


def _triggers(workflow) -> dict:
    """YAML 1.1 parses a bare `on:` key as the boolean True, not the string "on"."""
    return workflow.get("on") or workflow.get(True) or {}


def test_a_normal_shift_end_is_not_reported_as_a_failure():
    """`timeout --signal=INT` ends most shifts with 130, not 124. Reporting that
    as a failure paints every healthy handover red, and real failures then have
    nowhere to stand out — which is exactly how six hours of downtime went
    unnoticed once."""
    workflow = _workflow("bot.yml")
    script = "\n".join(
        step["run"] for step in workflow["jobs"]["run"]["steps"]
        if isinstance(step.get("run"), str)
    )
    assert "130" in script, "SIGINT's exit code is not treated as a clean shift end"
    for code in ("0", "124", "130"):
        assert code in script, f"exit {code} is not handled"


def test_the_bot_starts_its_own_successor():
    """The cron is the only thing that keeps this 24/7 — without it the bot stops
    after one shift and stays stopped until somebody presses a button."""
    workflow = _workflow("bot.yml")
    crons = [entry["cron"] for entry in _triggers(workflow)["schedule"]]
    assert crons, "no schedule means the bot never restarts itself"

    for cron in crons:
        minute, hour, *_ = cron.split()
        assert hour == "*", f"{cron} fires less often than hourly"
        assert minute.isdigit(), f"{cron} should fire at a fixed minute"


def test_a_dropped_fire_costs_less_than_an_hour():
    """GitHub delays and sometimes skips scheduled runs. One fire an hour makes
    that a full hour of downtime; two makes it half."""
    crons = [entry["cron"] for entry in _triggers(_workflow("bot.yml"))["schedule"]]
    minutes = sorted(int(cron.split()[0]) for cron in crons)
    assert len(minutes) >= 2, "a single fire an hour has no redundancy"

    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [60 - minutes[-1] + minutes[0]]
    assert max(gaps) <= 30, f"longest gap between fires is {max(gaps)} minutes"


def test_manual_dispatch_is_still_possible():
    """The fallback has to be startable, or it isn't a fallback."""
    assert "workflow_dispatch" in _triggers(_workflow("bot.yml"))


# --------------------------------------------------------------------------
# starting on a host that hands the bot a blank folder
#
# A panel host just runs `python bot.py` — there is no step before it to fetch
# the data. A bot that boots empty looks exactly like a brand-new one, and the
# obvious reaction is to run /setup again, which is how you end up with two sets
# of everything.
# --------------------------------------------------------------------------

def _bootstrap():
    sys.path.insert(0, str(ROOT))
    from hosting import bootstrap

    return bootstrap


def test_a_blank_install_fetches_the_saved_data(workspace, monkeypatch):
    seed_database(workspace)
    seed_schematics(workspace)
    sync("save", workspace)

    db_path = Path(workspace.env["DB_PATH"])
    schematics = Path(workspace.env["SCHEMATIC_DIR"])
    db_path.unlink()
    shutil.rmtree(schematics)

    monkeypatch.setenv("STATE_REMOTE", str(workspace.remote))
    monkeypatch.delenv("SKIP_STATE_RESTORE", raising=False)

    assert _bootstrap().restore_if_empty(db_path, schematics) is True

    db.set_db_path(db_path)
    assert db.get_config(GUILD)["builder_role_id"] == 11, "/setup would have to be re-run"
    assert any(schematics.rglob("*.schem")), "uploaded schematics did not come back"


def test_an_existing_database_is_never_overwritten(workspace, monkeypatch):
    """The file on disk is the truth; the branch is only a backup. Restoring
    over live data would silently roll the server back."""
    seed_database(workspace)
    sync("save", workspace)

    db.set_db_path(Path(workspace.env["DB_PATH"]))
    fresh = db.create_build(GUILD, "Added after the backup", "spec", SCRIPTER)

    monkeypatch.setenv("STATE_REMOTE", str(workspace.remote))
    assert _bootstrap().restore_if_empty(
        Path(workspace.env["DB_PATH"]), Path(workspace.env["SCHEMATIC_DIR"])
    ) is False

    db.set_db_path(Path(workspace.env["DB_PATH"]))
    assert db.get_build(fresh) is not None, "a newer build was thrown away"


def test_an_unreachable_remote_does_not_stop_the_bot_starting(workspace, monkeypatch, caplog):
    """Refusing to start would leave a panel host restart-looping forever, which
    is far harder to work out than an empty bot saying why."""
    db_path = Path(workspace.env["DB_PATH"])
    monkeypatch.setenv("STATE_REMOTE", "https://example.invalid/nope/nothing.git")

    with caplog.at_level("WARNING"):
        assert _bootstrap().restore_if_empty(
            db_path, Path(workspace.env["SCHEMATIC_DIR"])
        ) is False

    assert not db_path.exists()
    assert "empty database" in caplog.text
    assert "do NOT run /setup again" in caplog.text, "the log must say what not to do"


def test_the_fetch_can_be_turned_off(workspace, monkeypatch):
    monkeypatch.setenv("SKIP_STATE_RESTORE", "1")
    monkeypatch.setenv("STATE_REMOTE", str(workspace.remote))
    assert _bootstrap().restore_if_empty(
        Path(workspace.env["DB_PATH"]), Path(workspace.env["SCHEMATIC_DIR"])
    ) is False


def _start(entry: str) -> subprocess.CompletedProcess:
    """Actually start the bot, with no token so it stops at the first check.

    Cheap, and it imports every cog, view and helper on the way — so a typo
    anywhere fails here rather than on the host at 2am.
    """
    return subprocess.run(
        [sys.executable, entry],
        cwd=ROOT,
        env={**os.environ, "DISCORD_TOKEN": "", "SKIP_STATE_RESTORE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("entry", ["main.py", "bot.py"])
def test_both_entry_points_start_the_same_bot(entry):
    """Panels differ on which file they run — Pella defaults to main.py, the
    workflow uses bot.py. Neither may be the one that quietly rots."""
    result = _start(entry)
    assert "No DISCORD_TOKEN found" in (result.stdout + result.stderr), (
        f"{entry} did not get as far as the token check:\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 1


def test_main_says_what_to_do_when_packages_are_missing():
    """A panel gives you a log window. A bare ModuleNotFoundError traceback is a
    poor way to be told the install step didn't run."""
    source = (ROOT / "main.py").read_text()
    assert "pip install -r requirements.txt" in source
    assert "audioop" in source, "the 3.13 failure needs naming — it's the likely one"


def test_the_bot_can_be_installed_on_a_modern_python():
    """discord.py does `import audioop` at import time and audioop was removed
    from the standard library in 3.13 — so on a host offering 3.13 or 3.14 the
    bot doesn't misbehave later, it never starts."""
    requirements = (ROOT / "requirements.txt").read_text()
    assert "audioop-lts" in requirements
    assert 'python_version >= "3.13"' in requirements, (
        "the backport must be conditional, or it installs where it isn't wanted"
    )


def test_the_data_branch_is_not_mistaken_for_the_code_branch():
    """A host asked to clone `bot-data` gets a database and no bot.py. The name
    is one dropdown away from the code branch, so say so where it's read."""
    assert "bot-data" in (ROOT / "README.md").read_text()
    hosting = (ROOT / "README.md").read_text()
    assert "not the code" in hosting or "no code" in hosting, (
        "the README must warn that bot-data holds only data"
    )


def test_the_default_remote_is_public_so_it_needs_no_token():
    """The whole point is that it works on a host nobody configured."""
    remote = _bootstrap().DEFAULT_REMOTE
    assert remote.startswith("https://github.com/")
    assert "@" not in remote, "a token in the default URL would be committed to the repo"


def test_the_bot_fetches_state_before_touching_the_database():
    """Ordering matters: init_db() on a missing file creates an empty one, and
    then the fetch would decline to overwrite it — leaving the server blank."""
    source = (ROOT / "bot.py").read_text()
    assert source.index("restore_if_empty") < source.index("db.init_db()")


# --------------------------------------------------------------------------
# the real host
#
# A unit file is configuration that only fails at 4am on the night it matters,
# so the settings that make it survive a crash are checked here rather than
# discovered later.
# --------------------------------------------------------------------------

DEPLOY = ROOT / "deploy"


def _unit(name: str) -> dict[str, str]:
    """Flatten a unit file to {key: value}. Good enough — no key repeats here."""
    settings = {}
    for line in (DEPLOY / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        key, _, value = line.partition("=")
        settings[key.strip()] = value.strip()
    return settings


def test_the_bot_restarts_itself_after_a_crash():
    """The whole reason for leaving Actions was the bot staying down."""
    unit = _unit("astra-bot.service")
    assert unit["Restart"] == "always"
    assert int(unit["RestartSec"]) <= 30, "a slow restart is a long outage"


def test_it_never_gives_up_restarting():
    """systemd's default stops trying after 5 restarts in 10 seconds — which is
    exactly the situation you don't want it to give up on."""
    assert _unit("astra-bot.service")["StartLimitIntervalSec"] == "0"


def test_it_comes_back_after_a_reboot():
    assert _unit("astra-bot.service")["WantedBy"] == "multi-user.target"


def test_it_waits_for_real_networking():
    """`network.target` only means an interface exists. The bot opens a socket to
    Discord immediately, so it needs DNS to actually work."""
    unit = _unit("astra-bot.service")
    assert unit["After"] == "network-online.target"
    assert unit["Wants"] == "network-online.target"


def test_the_token_is_not_in_the_unit_file():
    """Unit files under /etc/systemd/system are world-readable; .env is 600."""
    text = (DEPLOY / "astra-bot.service").read_text()
    assert "DISCORD_TOKEN" not in text
    assert "Environment=" not in text


def test_the_installer_makes_the_env_file_unreadable_to_others():
    assert "chmod 600" in (DEPLOY / "install.sh").read_text()


def test_the_installer_stops_on_the_first_error():
    """Half an install is worse than none — it leaves a service pointing at a
    venv that was never built."""
    for script in ("install.sh", "update.sh"):
        assert "set -euo pipefail" in (DEPLOY / script).read_text(), (
            f"{script} would carry on after a failed step"
        )


def test_the_installer_brings_the_existing_data_across():
    """Everything the server knows is on the bot-data branch. An install that
    starts from an empty database silently loses every build and application."""
    text = (DEPLOY / "install.sh").read_text()
    assert "sync_state.py" in text and "restore" in text


def test_the_installer_says_to_turn_the_workflow_off():
    """Two bots online answer every command twice."""
    assert "Disable workflow" in (DEPLOY / "install.sh").read_text()


def test_backups_survive_the_machine_being_off():
    unit = _unit("astra-bot-backup.timer")
    assert unit["Persistent"] == "true", "a machine that was off would skip the day"


def test_a_snapshot_of_a_live_database_is_valid(workspace):
    """Copying the file mid-write gives a torn database. The backup script uses
    sqlite's own backup API, so a snapshot taken while the bot writes is sound."""
    seed_database(workspace)

    result = subprocess.run(
        [sys.executable, str(ROOT / "hosting" / "backup_db.py"),
         "--db", workspace.env["DB_PATH"],
         "--into", str(workspace.work / "backups"), "--keep", "3"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    snapshots = list((workspace.work / "backups").glob("buildboard-*.db"))
    assert len(snapshots) == 1

    restored = sqlite3.connect(snapshots[0])
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        titles = [row[0] for row in restored.execute("SELECT title FROM builds")]
        assert "Medieval spawn" in titles
    finally:
        restored.close()


def test_a_missing_database_is_not_a_backup_failure(workspace):
    """The timer fires whether or not the bot has ever run."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "hosting" / "backup_db.py"),
         "--db", str(workspace.work / "nothing.db"),
         "--into", str(workspace.work / "backups")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "nothing to do" in result.stdout


def test_old_snapshots_are_pruned(workspace):
    """Otherwise a daily backup fills a free-tier disk in a year."""
    from datetime import date, timedelta

    backups = workspace.work / "backups"
    backups.mkdir()
    for days in range(10):
        stamp = (date(2026, 8, 6) - timedelta(days=days)).isoformat()
        (backups / f"buildboard-{stamp}.db").write_bytes(b"old")

    sys.path.insert(0, str(ROOT / "hosting"))
    import backup_db

    dropped = backup_db.prune(backups, keep=7)
    assert len(dropped) == 3
    assert len(list(backups.glob("buildboard-*.db"))) == 7

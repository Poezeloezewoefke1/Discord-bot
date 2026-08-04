# Build Board Bot

A Discord bot that keeps builders and script writers on the same page.

Script writers describe what needs building. Builders claim a build — and the claim is a lock, so
**two builders can never start the same thing**. When a builder is done with their part they upload
a schematic and hand it off, and the next builder continues from that file instead of starting over.
A single board message always shows who is building what.

```
📋 Build board

🔨 Being built (3)
  #1 Medieval spawn — @Dex · 3 hours ago · ⬇ v2
  #2 Nether hub — @Jonas · 25 minutes ago
  #3 PvP arena — @Kai · 2 days ago

🟡 Open — needs a builder (2)
  #4 Shop district — continue from v1 by @Dex
  #5 Tutorial island — from @Mila · new

✅ Finished in the last 7 days (1)
  #6 Lobby rework

👷 Free builders
  @Ana @Tom
```

## How it works

1. **A script writer runs `/request`** and fills in what needs to be built. The bot posts a card in
   your requests channel with a **Claim** button, and opens a thread on it where the builder and
   script writer can talk.
2. **A builder clicks Claim.** Anyone else who clicks gets told who already has it.
3. **The builder runs `/update`** with their schematic and picks one of three things:
   - **Still building** — a checkpoint. The build stays theirs.
   - **Handing off** — their part is done. The build reopens with their schematic attached, and the
     next builder picks up where they left off.
   - **Finished** — the whole build is done. The card goes green and the thread is archived.
4. **The board updates itself** every time any of that happens.

## Setup

### 1. Create the bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy it.
3. Still on the Bot tab, scroll to **Privileged Gateway Intents** and turn on
   **Server Members Intent**. The bot needs this to list who's free — it won't start without it.
4. **OAuth2 → URL Generator**: tick scopes `bot` and `applications.commands`, then tick these bot
   permissions: *Send Messages*, *Embed Links*, *Attach Files*, *Read Message History*,
   *Create Public Threads*, *Send Messages in Threads*, *Manage Threads*.
5. Open the generated URL and invite the bot to your server.

### 2. Run it

```bash
git clone <this repo>
cd Discord-bot

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then paste your token into .env
python bot.py
```

Put your server's ID in `GUILD_ID` in `.env` — with it, the slash commands show up immediately.
Without it they sync globally and can take up to an hour to appear. (To get the ID: Discord
Settings → Advanced → Developer Mode on, then right-click your server → Copy Server ID.)

### 3. Point it at your roles and channels

In Discord, an admin runs:

```
/setup builder_role:@Builder scripter_role:@Script Writer
       requests_channel:#build-requests board_channel:#build-board
```

That's the only configuration step, and it all lives in Discord — you never edit a file to move a
channel. The bot checks it has the permissions it needs in both channels and tells you exactly
what's missing if not.

Use a dedicated, otherwise-empty channel for the board. The bot keeps one message there and edits
it in place.

## Hosting it 24/7 on GitHub Actions

The bot can host itself out of this repo — no server of your own. **Read the two trade-offs below
before you set this up**, because neither is reversible after the fact.

### What you're accepting

**Your build data becomes public.** State lives in a branch called `bot-data`: the database (build
descriptions, Discord user IDs) and every schematic anyone uploads. This repo is public, so anyone
can browse and download all of it. If that's not okay, make the repo private — Actions then bills
against your 2,000 free minutes/month, which 24/7 running will exhaust in about three days.

**GitHub's acceptable-use rules don't cover running a service on Actions.** Actions is for building
and testing code, and repos have been flagged for using it as free hosting. Nothing here hides the
usage. If that risk isn't acceptable, run the bot on the machine that already runs your Minecraft
server instead — the "Run it" steps above are all it takes, and no code changes are needed.

### Setting it up

1. **Settings → Secrets and variables → Actions → New repository secret**, twice:
   - `DISCORD_TOKEN` — your bot token
   - `GUILD_ID` — your server ID

   Never put the token in a file in this repo. It would be public within seconds of pushing.
2. **Actions** tab → **Run bot** → **Run workflow**.

That's it. It re-launches itself from then on.

### How it stays up

Actions kills any job at 6 hours, so the bot runs in shifts:

- Each run hosts the bot for **5h30m**, then shuts down gracefully.
- The workflow is scheduled every 2 hours, but those fires **don't start a second bot** — a
  concurrency group makes each one wait its turn. That means a successor is nearly always parked
  and ready, so when a shift ends the next starts **within seconds**.
- Firing every 2 hours is the redundancy: if GitHub skips a scheduled run under load, another is
  along shortly rather than the bot being down for a whole shift.

**Cancelled runs in the Actions tab are normal.** Each new scheduled fire replaces the previous
waiting one, and the replaced run shows as cancelled. Only a failed **Run the bot** step means
something is actually wrong.

Expect a few seconds' gap at each handover, and rarely up to ~2 hours if GitHub drops a schedule.
During a gap, commands fail with Discord's "application did not respond".

### How your data survives restarts

Every runner starts with a blank disk, so `hosting/sync_state.py` copies the database and schematics
to the `bot-data` branch every 5 minutes and once more at shutdown. The next run restores them
before the bot starts. Worst case — a hard crash — loses the last 5 minutes of activity.

The database is copied with SQLite's backup API rather than a plain file copy, because the bot is
writing to it at the same time and a naive copy can produce a file that won't open. The branch is
rewritten as a single commit each time; keeping history would mean re-storing the whole database
every few minutes forever.

To back up or reset everything, that one branch is all your data.

> GitHub disables scheduled workflows after **60 days without repository activity**. The state
> commits should keep that timer alive, but if the bot ever goes quiet for a long stretch, check the
> Actions tab for a "this workflow was disabled" banner and click Enable.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/request` | Script writers | Opens a form to describe a build that needs making |
| `/claim <build>` | Builders | Takes an open build so nobody doubles up |
| `/update <build> <status> [file] [note]` | The builder holding it | Posts progress, with a schematic |
| `/release <build>` | The builder holding it | Gives it back without uploading |
| `/delete <build>` | Whoever requested it, or admins | Deletes it for good, after a confirmation |
| `/builds [status]` | Anyone | Lists builds and who's on them |
| `/build <build>` | Anyone | Full detail on one build, including its handoff history |
| `/schematic <build>` | Anyone | Downloads the latest schematic |
| `/setup` | Admins | Connects roles and channels |
| `/setup-show` | Admins | Shows current configuration |
| `/board-refresh` | Admins | Forces the board to redraw |

Everything the bot says back to you is ephemeral — only you see it, so channels stay clean.
Build numbers autocomplete as you type, and `/update` and `/release` list *your* builds first.

## Notes

**Accepted files:** `.schem`, `.schematic`, `.litematic`, `.nbt`, `.zip` — up to 25 MB.

**Schematics are stored on disk** in `schematics/<build id>/`, not just linked. Discord's attachment
links now expire, so a handoff from a few months ago would otherwise become undownloadable.
`/schematic` always serves the stored copy. Back this folder up along with `buildboard.db` — those
two are all your data.

**Admins bypass every role check**, so you can't lock yourself out, and admins can force-release a
build if a builder goes inactive.

**Deleting is permanent.** `/delete` removes the request, its thread, its whole history and every
schematic uploaded to it — the confirmation tells you exactly how many files that is before you
commit. Only the person who requested the build or an admin can do it; a builder who no longer
wants a build should `/release` it instead, so the request stays alive for someone else.

**Buttons keep working after a restart.** Build IDs are encoded into the buttons themselves, so old
cards stay live and you never need to re-post them.

## Development

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

The tests need no Discord connection. They cover the whole claim/handoff state machine — including
a 20-thread race on one build to prove exactly one claim can win — and the state round trip, using
a real git repo in a temp directory to prove a restart doesn't lose claims or schematics.

They also run automatically on every push and pull request via `.github/workflows/tests.yml`.

| File | What's in it |
|---|---|
| `bot.py` | Entry point: intents, cog loading, command sync |
| `config.py` | Environment loading, permission helpers, constants |
| `db.py` | SQLite schema and every query — the only file with SQL in it |
| `service.py` | Shared logic behind both buttons and commands |
| `embeds.py` | The build card, the board, update posts |
| `views.py` | Buttons and the request form |
| `cogs/setup.py` | `/setup`, `/setup-show`, `/board-refresh` |
| `cogs/builds.py` | The build workflow commands |
| `hosting/sync_state.py` | Saves/restores state to the `bot-data` branch (GitHub hosting only) |
| `.github/workflows/` | `bot.yml` runs the bot, `tests.yml` runs the tests |

The bot itself knows nothing about GitHub — it just reads `DB_PATH` and `SCHEMATIC_DIR` from the
environment. All the hosting-specific logic sits in `hosting/`, so moving to a normal server later
means deleting a workflow file, not rewriting the bot.

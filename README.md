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
   *Create Public Threads*, *Send Messages in Threads*, *Manage Threads*, and — if you want
   auto-role on join — *Manage Roles*.
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
| `/test` | Anyone | Checks the bot is online and everything is set up correctly |
| `/ping` | Anyone | Quick "are you awake?" check |
| `/welcome-setup` | Admins | Sets up welcome messages, auto-role and goodbyes |
| `/welcome-test` | Admins | Previews the welcome message on yourself |
| `/welcome-off` | Admins | Turns welcomes, goodbyes and auto-role back off |
| `/security-setup` | Admins | Sets up anti-raid and anti-spam protection |
| `/security-test` | Admins | Checks the protections are armed, and whether *you* are exempt |
| `/security-mode` | Admins | Switches between watch-only and acting for real |
| `/notify-setup` | Admins | Chooses where new YouTube uploads are announced |
| `/notify-test` | Admins | Checks the upload watcher is alive and both feeds resolve |
| `/notify-check` | Admins | Checks for new uploads right now |
| `/notify-latest` | Admins | Posts a channel's newest video on demand |
| `/notify-resolve` | Admins | Re-checks which YouTube channel each handle points at |
| `/ticket-setup` | Admins | Sets up the ticket system and posts the panel |
| `/ticket-panel` | Admins | Posts the panel again if it gets deleted |
| `/ticket-add` | Support | Adds someone to the ticket you're in |
| `/ticket-close` | Support / opener | Closes the ticket you're in |

Everything the bot says back to you is ephemeral — only you see it, so channels stay clean.
Build numbers autocomplete as you type, and `/update` and `/release` list *your* builds first.

## Checking it's working

`/test` is the "is this thing on?" command. It reports whether the bot is online, how long until it
restarts, and then actually re-checks every piece of setup rather than assuming it's still valid:

```
🩺 Everything works

Connection
  ✅ Online and responding
  • Ping: 82 ms
  • Running for 2h 14m, restarts in about 3h 16m

Build board
  ✅ Builder role: @Builder
  ✅ Script writer role: @Script Writer
  ✅ Requests channel: #build-requests
  ✅ Board channel: #build-board

Welcome messages
  ✅ Welcome channel: #welcome
  ✅ Points to: #applications
  ✅ Auto-role: @Member

Data
  ✅ Database readable
  • 🟡 2 open · 🔨 3 being built · ✅ 4 finished
```

It's worth running after any change to your server, because most of what breaks this bot breaks
silently: a channel deleted, a permission removed, or roles reordered so auto-role quietly stops
working. `/test` turns each of those into a red line naming the fix, instead of you finding out when
a new member gets no role.

`/ping` is the instant version — just confirms the bot is awake, which is the usual question during
a restart gap.

## Welcoming new members

The bot also greets people who join, so you don't need a separate welcome bot.

```
/welcome-setup welcome_channel:#welcome applications_channel:#applications
               auto_role:@Member goodbye_channel:#goodbye
```

`auto_role` and `goodbye_channel` are optional. Then `/welcome-test` shows you exactly what a new
member will see, without needing anyone to actually join.

A new member gets pinged, then an embed:

```
@newmember
┌ 🖼  Welcome to ASTRA Smp Events
│    Welcome @newmember to ASTRA Smp Events!
│    Please make an application in #applications
└    You're our 57th member
```

The server name and member count fill in automatically. The wording itself lives in
`welcome_card()` in `embeds.py` — change it there if you want it to read differently.

### Auto-role needs one thing set up right

For the bot to hand out a role, **its own role must sit above the role it's giving out** in
**Server Settings → Roles**, and it needs the **Manage Roles** permission. This is the usual reason
auto-role silently does nothing.

`/welcome-setup` checks both up front and refuses with the specific fix rather than letting it fail
on every join, and `/welcome-test` re-checks in case roles get reordered later.

If the role assignment fails at join time, the member still gets their welcome message — the two are
deliberately independent.

## Upload announcements

Announces new YouTube uploads with an `@everyone` ping.

```
/notify-setup channel:#announcements
```

The channels being watched are fixed in `CREATORS` at the top of `notify.py`:

- [@Pyro_Blits](https://www.youtube.com/@Pyro_Blits)
- [@Microman_J](https://www.youtube.com/@Microman_J)

It uses YouTube's public Atom feeds — no API key, no scraping, nothing to expire. The `@handle` is
resolved to the underlying `UC…` channel id once and cached, so renaming a channel doesn't need a
code change. Checked every 5 minutes.

### Shorts

**The plain channel feed leaves Shorts out**, so a channel that has only posted Shorts looks
completely empty through it. The bot therefore tries the channel's *uploads playlist* feed first
(`playlist_id=UU…`, the same id with the prefix swapped), which covers both, and falls back to the
channel feed. `/notify-test` shows which source each channel is being read through.

If Shorts still don't appear, YouTube simply hasn't published them to any feed yet — that can lag
well behind the upload. `/notify-latest` posts the newest video on demand in the meantime.

### If an upload is announced under the wrong name

A channel page mentions dozens of channel ids — one for every recommended video and featured
channel — so looking one up has to use the page's *own* markers (`rel="canonical"`,
`itemprop="identifier"`, `externalId`) rather than the first id that appears. Getting that wrong
points a handle at a collaborator's channel, and their uploads then get announced under the wrong
creator.

Two things guard against it now. The announcement takes the creator name from the **feed's own
author field**, so the video itself says who made it regardless of the mapping. And `/notify-test`
shows the uploader each feed reports, so a mismatch is visible.

If one is wrong, `/notify-resolve` redoes the lookup. A channel that changes is re-baselined, so its
back catalogue doesn't all announce at once.

**Existing videos are never announced.** The first check for a channel records the whole backlog as
seen and posts nothing — otherwise setup would fire fifteen `@everyone` pings at once. Seen videos
are stored in the database, so the ~6-hourly restart resumes rather than re-announcing everything.

`@everyone` needs the **Mention Everyone** permission in that channel. `/notify-setup` warns if it's
missing, because without it the announcement still posts and simply pings nobody — which looks like
it worked.

`/notify-test` shows whether the loop is running, whether each handle resolved, and when each feed
was last checked successfully. Worth a look if uploads stop appearing.

### Why there's no TikTok

TikTok has no public feed or API, and actively blocks scraping. Anything built against it would work
for a while and then quietly stop — which is worse than not having it, because you'd stop checking.
Post those manually.

## Support tickets

A panel with buttons. Pressing one opens a private channel only that person and your support role
can see.

```
/ticket-setup category:TICKETS support_role:@Staff
              log_channel:#ticket-log panel_channel:#support
```

```
🎫 Need help?
Pick the option that fits. A private channel opens that only you and @Staff can see.

🚨 Report a player — Tell us who and what happened.
❓ Help / support — Describe what you need.

[🚨 Report a player]   [❓ Help / support]
```

A short form asks what's wrong before the channel is made, so staff arrive already knowing the
problem. The channel is named `report-0007` / `help-0007`, and inside it staff get **Claim** and
**Close**.

**One open ticket per person.** A second press points them at the one they already have — without
that, one bored member can fill the category in a minute.

### Closing keeps the channel

**Close** locks the ticket: the opener loses access and the channel is renamed `closed-0007`. It is
*not* deleted. Staff then get **Reopen**, **Transcript** and **Delete channel** — deletion is always
a deliberate, separate step, so nothing is lost by accident.

### Transcripts and the Message Content intent

A transcript records who spoke, when, and any attachments. **Message text needs the Message Content
intent**, which is currently off — so until you enable it, transcripts carry a header saying the text
is missing and why, rather than looking complete but empty.

This is the second reason to enable that intent (scam-link scanning is the other). Same order as
always: **Developer Portal first**, then `ENABLE_MESSAGE_CONTENT: 1`. Reversed, the bot won't start.

Because closing keeps the channel, the missing intent costs nothing day to day — the conversation is
still there in Discord. It only matters if you delete a channel and later want the words back.

### Permissions

**Manage Channels** is required — without it no ticket can be created at all. The log channel also
needs **Attach Files** for transcripts. `/ticket-setup` checks both up front, and warns when the
category is near Discord's 50-channel limit.

## Anti-raid and anti-spam protection

Four protections, all of which start in **watch mode** — they report what they *would* have done
and take no action until you say so.

```
/security-setup log_channel:#mod-log honeypot_channel:#⛔-do-not-post
                min_account_age_days:7 raid_joins:10 raid_seconds:60
```

| Protection | What it catches |
|---|---|
| **Honeypot channel** | Spam bots blast every channel they can see. This one is labelled "posting here = ban", so only a bot walks in. The most effective of the four. |
| **Anti-raid** | A burst of joins — 10 in 60 seconds by default — alerts staff instead of you finding out from the spam. |
| **New-account gate** | Flags accounts created in the last 7 days. Raid accounts are nearly always fresh. |
| **Scam links** | Fake Nitro/Steam domains. Needs an extra intent — see below. |

`/security-setup` posts and pins the warning in the honeypot channel itself. Don't remove it: an
unlabelled honeypot catches curious members instead of bots.

### Watch mode

Everything starts in watch mode and stays there until you run `/security-mode live`. Alerts look
like this, with buttons so staff can act on one directly:

```
🔍 Watch mode — no action taken
Posted in the honeypot channel
Posted in #⛔-do-not-post, which nobody should ever post in.

Member        @SomeUser · 847...291
Account age   2 hours
Would have done   ban  (nothing was actually done)

[🔨 Ban them]  [Ignore]
```

Run it this way for a few days and read the log. If anything in the setup is wrong, you find out
from a log line instead of from your members being banned.

**Staff are never actioned** — the owner, and anyone with Administrator, Manage Server, Ban Members
or Manage Messages, is skipped before any check runs. You cannot lock yourself out with this.

### Testing it without wondering whether it's broken

Because staff are exempt, **posting in the honeypot yourself will never ban you**. To stop that
looking like a dead bot, two things exist:

- **`/security-test`** — reports what's armed, whether the bot can actually enforce the action, and
  states plainly whether *you personally* are exempt.
- **The "trap is armed" notice** — when staff post in the honeypot, the log says so:

  > 🛡️ **Trap is armed.** @Owner posted in #⛔-do-not-post and was ignored — staff are always
  > exempt. A normal member doing that would have been **banned**.

  Rate-limited to once every 10 minutes so staff chat can't flood the log.

The only real proof is posting from an account with no staff permissions. Everything above is there
to make that test interpretable when it doesn't go how you expect.

### Changing one setting at a time

`/security-setup` keeps any setting you don't mention. Re-running it with just
`raid_joins:10 raid_seconds:60` leaves your account-age threshold and honeypot channel alone, and
the reply shows the values actually stored — not the ones you typed.

It also warns about thresholds that can never fire: `30 joins in 5 seconds` means six joins a second
sustained, which looks configured but never triggers.

### Going live

`/security-mode live` checks the bot actually holds the permission for the configured action before
switching. A "live" mode that can't enforce is worse than watch mode, because it claims protection
it doesn't have and Discord only says otherwise with a silent 403 at the moment it matters.

### Turning on scam-link scanning

This one needs Discord's **Message Content** intent, and the order matters:

1. Developer Portal → your app → Bot → enable **Message Content Intent**
2. *Then* set `ENABLE_MESSAGE_CONTENT: 1` in `.github/workflows/bot.yml`
3. `/security-setup ... scam_scanning:True`

**Do step 2 before step 1 and the bot won't start at all** — Discord refuses the connection, and the
workflow would restart it into the same failure every couple of hours. That's why it ships off.

Detection works by brand impersonation rather than a list of known-bad domains: a link that reads
like `discord` but isn't one of Discord's real domains is the signal, and that keeps working when
scammers register a new domain tomorrow. `discrod.gg` and `disc0rd-nitro.xyz` are caught;
`discord.gift`, `discordjs.guide` and `nitrado.net` are not. If something legitimate does get
flagged, add it to `ALLOWED_DOMAINS` in `security.py`.

### What this isn't

Honeypot and bots like it draw on a shared database of known raiders across thousands of servers.
That's a network effect, not code, and it isn't reproduced here. This catches accounts that
misbehave *on your server* — it doesn't know an account was banned elsewhere.

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

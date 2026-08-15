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

## Hosting it 24/7

The bot hosts itself out of this repo on GitHub Actions, with no server of your own.

Actions kills any job at 6 hours and wipes the disk between runs, so it works in shifts: each run
hosts the bot for **5h30m** while syncing its database to the `bot-data` branch, and the schedule
keeps a successor parked so it takes over the moment a shift ends. Nobody presses anything.

The schedule fires **twice an hour**. Those fires don't start a second bot — the concurrency group
makes each one wait its turn. Firing often is the redundancy: GitHub delays and sometimes skips
scheduled runs, and twice an hour means a dropped fire costs 30 minutes rather than 60.

**This is not what Actions is for, and it has bitten once.** On 6 August GitHub simply stopped
handing out runners: the queued restarts sat waiting until they were killed, and the bot was down
for about six hours. Nothing in this repo can prevent that. If the bot matters enough that half a
day offline is unacceptable, `deploy/` sets it up on a machine that stays on — same code, same
database, and the migration is one command.

**If you ever do move it, delete the `schedule:` block first.** A fire while another host is running
puts two bots online, and every command gets answered twice.

### Moving it to a machine that stays on

If half a day offline is ever unacceptable, run it somewhere built for it. Anything with systemd
that stays on, in rough order of least effort:

| | |
|---|---|
| **The box your Minecraft server is on** | Already online, already paid for. The bot needs a few MB of RAM. |
| **Oracle Cloud Always Free** | A genuinely free VM, no time limit. Sign-up asks for a card to verify and doesn't charge it. |
| **Google Cloud e2-micro** | Also always-free, US regions only. |
| **A Raspberry Pi at home** | Free if you own one; as reliable as your internet and power. |

Avoid anything that "sleeps when idle" — Render's and Replit's free tiers do, and a sleeping bot is
the problem you're trying to solve.

### On a bot panel (Pella, Pterodactyl and similar)

A panel gives you a folder and runs one command. Nothing else is needed:

| Setting | Value |
|---|---|
| **Repository** | this repo |
| **Branch** | `main` — **not** `bot-data` |
| **Runtime** | Python 3.11 or newer |
| **Start command** | `python main.py` (`python bot.py` works too) |
| **Install** | `pip install -r requirements.txt` (most panels do this for you) |

> **`bot-data` is not the code branch.** It holds the database and schematics and
> nothing else — no `bot.py`. A host pointed at it clones successfully and then
> has nothing to run. It's one line down in most branch dropdowns, so it's worth
> checking twice.

Upload the code — or point the panel at this repo and branch — and set two
environment variables, either in the panel's variables screen or in a `.env` file
next to `bot.py`:

```
DISCORD_TOKEN=your-bot-token
GUILD_ID=your-server-id
```

**Your existing data comes across on its own.** The first time the bot starts and
finds no database, it fetches the saved one from the `bot-data` branch — builds,
tickets, applications, every setting. No files to copy, no `/setup` to redo. It
only ever does this when there is no database at all, so it can never overwrite
anything live, and if it can't reach the branch it says so loudly in the log
rather than starting up pretending everything is fine.

Two things to check on any panel:

- **Storage has to persist between restarts.** `buildboard.db` and `schematics/`
  live in the bot's folder. A panel that wipes the folder on restart would send
  the bot back to the branch copy each time and lose anything since.
- **Turn the GitHub workflow off** — *Actions → "Run bot" → ⋯ → Disable workflow* —
  or two bots answer every command twice.

Set `SKIP_STATE_RESTORE=1` if you ever want a genuinely empty start.

#### Panels with no GitHub option

Some panels (bot-hosting.net among them) only give you a file manager and a start
command — there is no repo field to fill in. Put the fetch in the start command
instead, and every restart picks up the newest code:

```sh
cd /home/container && git init -q && { git remote add origin https://github.com/Poezeloezewoefke1/Discord-bot.git 2>/dev/null || git remote set-url origin https://github.com/Poezeloezewoefke1/Discord-bot.git; } && git fetch -q --depth 1 origin main && git reset -q --hard FETCH_HEAD && if [ -f requirements.txt ]; then pip install --disable-pip-version-check -U --prefix .local -r requirements.txt; fi && python3 -u ${STARTUP_FILE}
```

Two things worth knowing about it:

- `git reset --hard` only touches files git tracks, so `buildboard.db`,
  `schematics/` and `.env` are left alone. The flip side is that edits made in
  the panel's file manager are undone on the next restart.
- It has to be idempotent, because it runs on *every* start rather than once.
  `git init` on an existing repo is harmless, but `git remote add` is not — it
  exits non-zero when the remote is already there, which takes the whole chain
  down with it. Hence the `||` fallback to `set-url`.

### On your own machine (VPS, Oracle Cloud, a Pi)

On a fresh Ubuntu machine, one command:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Poezeloezewoefke1/Discord-bot/main/deploy/install.sh)"
```

It asks for the bot token and the server ID, then does the rest: installs Python, clones the repo,
builds a virtualenv, **restores the existing database from the `bot-data` branch** so nothing is
lost in the move, and installs the service so the bot starts on boot.

Then turn the old workflow off, or two bots answer every command twice:
**GitHub → Actions → "Run bot" → ⋯ → Disable workflow**.

No inbound ports are needed. The bot only makes outgoing connections, so the firewall can stay shut.

### Running it

```bash
sudo journalctl -u astra-bot -f          # watch it live
systemctl status astra-bot               # is it up?
sudo systemctl restart astra-bot         # restart
sudo bash /opt/astra-bot/deploy/update.sh  # pull the latest code and restart
```

`Restart=always` with `StartLimitIntervalSec=0` means a crash at 4am is back in ten seconds, and
systemd never gives up trying — the default gives up after five quick restarts, which is exactly the
case you don't want it to.

### Backups

The database is a single file, so the risk on a machine that keeps its disk is that the file quietly
goes bad. A timer takes a dated snapshot daily and keeps the last seven, in `/opt/astra-bot/backups`.
Snapshots use SQLite's backup API rather than copying the file, so one taken while the bot is
mid-write is still a valid database, and each is integrity-checked before the old ones are pruned.

To restore, stop the bot, copy a snapshot over `buildboard.db`, start it again.

For an off-machine copy as well, `hosting/sync_state.py save` still pushes everything to the
`bot-data` branch — it needs `GITHUB_TOKEN` and `GITHUB_REPOSITORY` set. Worth it if losing the VM
would matter; note the branch is public, so build data and schematics are readable by anyone.

### If the machine dies

Start the fallback: **Actions → Run bot → Run workflow**. It picks up the last state pushed to
`bot-data` and hosts the bot for 5h30m, once. It will not restart itself; run it again if you need
another shift, and stop it once the real host is back.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/request` | Script writers | Opens a form to describe a build that needs making |
| `/claim <build>` | Builders | Takes an open build so nobody doubles up |
| `/update <build> <status> [file] [note]` | The builder holding it | Posts progress, with a schematic |
| `/release <build>` | The builder holding it | Gives it back without uploading |
| `/delete <build>` | Whoever requested it, or admins | Deletes it for good, after a confirmation |
| `/move <build> <channel>` | Whoever requested it, or admins | Moves the build's card to another channel |
| `/repost [build] [channel]` | Admins | Posts build cards again if they've gone missing |
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
| `/apply` | Anyone | Applies for a position |
| `/apply-setup` | Admins | Sets up applications and posts the panel |
| `/apply-panel` | Admins | Posts the apply panel again |
| `/apply-form` | Admins | Adds or edits a position and its questions |
| `/apply-form-delete` | Admins | Removes a position |
| `/apply-toggle` | Admins | Opens or closes applications for one position |
| `/applications [status]` | Staff | Lists applications |
| `/application <number>` | Staff | Shows one application in full |

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
| **Honeypot channel** | Spam bots blast every channel they can see. This one is labelled "don't post here", so only a bot walks in. The most effective of the four. |
| **Anti-raid** | A burst of joins — 10 in 60 seconds by default — alerts staff instead of you finding out from the spam. |
| **New-account gate** | Flags accounts created in the last 7 days. Raid accounts are nearly always fresh. |
| **Scam links** | Fake Nitro/Steam domains. Needs an extra intent — see below. |

`/security-setup` posts and pins the warning in the honeypot channel itself. Don't remove it: an
unlabelled honeypot catches curious members instead of bots.

### The honeypot kicks, it doesn't ban

The honeypot has its **own** action, separate from the one every other protection uses, and it
defaults to **kick**.

That's deliberate. The honeypot fires on a single message with no second signal — one curious
member clicking into the wrong channel is enough. A kick is recoverable: they can be invited
straight back. A ban, on a server this size, effectively isn't. The other protections have more to
go on before they act, so they keep whatever `action:` you set.

```
/security-setup log_channel:#mod-log honeypot_action:Ban    # if you really want a ban
```

The pinned warning is generated from the action, so it says "kicked" when it kicks and "banned"
when it bans — a sign that names the wrong consequence just teaches people to ignore the sign. If
you change the action, re-run `/security-setup` with `honeypot_channel:` to repost it.

Going live checks the permission for **both** actions. Kick Members and Ban Members are separate
permissions, so a honeypot set to kick on a bot that can only ban would fail silently at the exact
moment it mattered.

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

## Applications

A panel of buttons, one per position. Pressing one opens a form; submitting it drops the answers
into a staff-only channel with **Accept** and **Deny** on them. Either way the applicant gets a DM,
and accepting hands out the role.

```
/apply-setup review_channel:#staff-applications panel_channel:#apply reviewer_role:@Staff
```

That creates five positions to start with, with questions written for a Minecraft server, and
**works out which of your roles each one hands out** by reading the server's role list. Builder and
Script writer come from `/setup`, which was already told them; the rest are matched on name.

The matching is deliberately cautious about two things:

- **It never guesses a role with Administrator or Manage Server**, however well the name matches.
  Those are the whole server, and a name is thin evidence.
- **Shortest match wins.** *Script Writer* is a substring of *Lead Script Writer*, so a careless
  match would promote every applicant on their first day.

The staff position looks for *Trainee staff*, *trial staff* or *helper* — deliberately **not**
*staff*, *moderator* or *admin*. Somebody accepted for a helper role should land on the bottom rung.

`/apply-setup` prints what it matched so you can check it at a glance, and flags any role that has
moderator powers or that the bot can't hand out. If one is wrong:

```
/apply-form label:Video editor role:@Editors
```

A role you set by hand is never replaced by a later guess.

```
📥 Applications

🔨 Builder
Build what the script writers describe. Bring pictures — we go on what you
show us, not what you say.

📜 Script writer
Describe the builds and write the events. Question 2 is the actual job —
that's what we read first.

🛡️ Staff / helper
Keep chat friendly and help people out. We're looking at how you handle
people, not how long you've played.

🎬 Video editor
Cut the SMP footage into videos and shorts. Links to your work matter more
than anything else here.

📣 Promotor
Get more people onto the server. A real plan beats a big following.

[🔨 Builder] [📜 Script writer] [🛡️ Staff / helper] [🎬 Video editor] [📣 Promotor]
```

### How the questions are written

Most application forms sort nobody, because most questions have one obvious answer. *"Would you
abuse staff perms?"* — no. *"Are you a good builder?"* — yes. Everyone passes, and you learn
nothing. The defaults here are built the other way round:

- **Evidence over opinion.** *"Show us your three best builds"* — no links, no application. A
  builder's work is checkable; their self-assessment isn't.
- **What you did, not what you'd do.** *"Biggest thing you finished, and how long"* is hard to
  invent. Intentions are free.
- **Questions with a genuinely bad answer.** *"A friend of yours breaks a rule. What now?"* splits
  the field, which is the entire point of asking.
- **This server's actual work.** Builds get handed between people as schematic files, so Litematica
  and WorldEdit matter more than a favourite block. A script writer is asked to *write a brief*,
  because writing briefs is the job.
- **Availability in numbers.** *"Are you active?"* gets "yes". Age, timezone and hours a week get
  something you can plan around.

Discord caps a question at 45 characters, which is far too short to explain any of that — so every
question also carries **hint text** in the box, which is where an applicant is told what a good
answer looks like. Tests enforce that no default question ships without one.

Your own questions added with `/apply-form` don't get hint text (that would need five more
parameters on an already long command), so write those to stand on their own.

Re-running `/apply-setup` adds any standard position the server is missing and leaves the ones it
already has completely alone — so a new position added to the bot doesn't have to be retyped by
hand. The flip side: a standard position you deliberately deleted comes back, and needs deleting
again.

### Changing the questions

```
/apply-form label:Builder role:@Builder question1:Your Minecraft username
            question2:How old are you? question3:What have you built before?
```

Five questions maximum — that's Discord's cap on a form, not a choice. Each question is capped at
45 characters for the same reason. Both are checked before saving, with the offending question
named, rather than letting Discord reject the form later.

Editing a position by name **keeps** anything you don't mention: `/apply-form label:Builder
question1:...` changes the questions and leaves the role and blurb alone. Positions are matched on
a key derived from the name, so re-using the name edits rather than duplicating.

`/apply-toggle position:Builder open:False` closes one without deleting it — the panel says
*(closed)* and the button refuses politely.

### Three things that go wrong, and what happens instead

**The applicant never hears back.** Most people have DMs from strangers turned off, so the DM fails
and nothing says so. Every failed DM is reported straight back to whoever pressed the button:
*"@Someone has DMs closed, so they haven't been told."*

**The role doesn't get handed out.** Discord refuses to let a bot give out a role above its own,
with a bare 403. That's checked when you set the position up, again in `/test`, and again at the
moment of accepting — and reported with the actual fix, not the error code. The decision is still
recorded either way.

**Two staff decide at once.** Accept and Deny in the same second would otherwise send the applicant
both answers. The decision is a single conditional write, so the second person is told
*"already decided — accepted by @Someone"* and nothing is sent twice.

### Applying twice

One application per position at a time, and after being turned down there's a **7-day wait** before
reapplying for that same position (`cooldown_days:` to change it, `0` to allow it immediately).
Somebody who already holds the role is told so rather than being allowed to apply again.

Everything is checked *before* the form opens, so nobody fills in five answers and only then learns
applications were closed.

## Notes

**Accepted files:** `.schem`, `.schematic`, `.litematic`, `.nbt`, `.zip` — up to 25 MB.

**Schematics are stored on disk** in `schematics/<build id>/`, not just linked. Discord's attachment
links now expire, so a handoff from a few months ago would otherwise become undownloadable.
`/schematic` always serves the stored copy. Back this folder up along with `buildboard.db` — those
two are all your data.

**Admins bypass every role check**, so you can't lock yourself out, and admins can force-release a
build if a builder goes inactive.

**Moving a build** with `/move` posts its card and a fresh thread in the new channel and updates the
board. Discord won't relocate a thread between channels, and deleting a card deletes the thread
hanging off it — so if the old thread has discussion in it, the old card is left behind as a
signpost pointing at the new one rather than taking the conversation with it. If the thread was
empty, the old card is simply removed.

**If cards go missing**, `/repost` puts them back. The usual cause is changing the requests channel
in `/setup`: every existing card stays in the old channel while the bot looks in the new one. Run it
with no arguments to repost everything still to be done — open and in-progress builds, not finished
ones. Build numbers and progress are kept.

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

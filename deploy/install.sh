#!/usr/bin/env bash
#
# Install the bot as a service on an always-on Linux machine (Oracle Cloud
# Always Free, a VPS, a Raspberry Pi — anything with systemd).
#
# Safe to run more than once: it updates in place rather than starting over, so
# it doubles as the "I changed something, apply it" script.
#
#   curl -fsSL https://raw.githubusercontent.com/Poezeloezewoefke1/Discord-bot/claude/builder-script-communication-q6ndma/deploy/install.sh | bash
#
# or, from a clone:   sudo -v && bash deploy/install.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Poezeloezewoefke1/Discord-bot.git}"
BRANCH="${BRANCH:-claude/builder-script-communication-q6ndma}"
DIR="${DIR:-/opt/astra-bot}"
SERVICE="astra-bot"
RUN_USER="${SUDO_USER:-$USER}"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo:  sudo bash deploy/install.sh"
command -v systemctl >/dev/null || die "No systemd here. This script expects Ubuntu/Debian."

# --------------------------------------------------------------------------
say "Installing what's needed"
# --------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip >/dev/null

PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYTHON_VERSION" in
  3.1[1-9]|3.[2-9][0-9]) : ;;
  *) die "Python $PYTHON_VERSION is too old — this needs 3.11 or newer.
     On Ubuntu 22.04+:  sudo apt install python3.11 python3.11-venv" ;;
esac
echo "Python $PYTHON_VERSION"

# --------------------------------------------------------------------------
say "Fetching the bot into $DIR"
# --------------------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --quiet origin "$BRANCH"
  git -C "$DIR" checkout --quiet "$BRANCH"
  git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
  echo "Updated an existing install."
else
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR"
  echo "Cloned fresh."
fi
chown -R "$RUN_USER:$RUN_USER" "$DIR"

# --------------------------------------------------------------------------
say "Python packages"
# --------------------------------------------------------------------------
sudo -u "$RUN_USER" python3 -m venv "$DIR/.venv" 2>/dev/null || true
sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

# --------------------------------------------------------------------------
say "Token"
# --------------------------------------------------------------------------
# The token is the one secret. It lives in .env, readable only by the account
# the bot runs as — never in the unit file, which is world-readable.
if [ ! -f "$DIR/.env" ]; then
  echo "The bot needs its Discord token."
  echo "Get it from https://discord.com/developers/applications → your app → Bot → Reset Token"
  echo
  read -rsp "Paste the bot token: " TOKEN; echo
  read -rp  "Your Discord server ID (right-click the server → Copy Server ID): " GUILD

  [ -n "$TOKEN" ] || die "No token given — nothing was installed."

  cat > "$DIR/.env" <<ENVFILE
DISCORD_TOKEN=$TOKEN
GUILD_ID=$GUILD
# Scam-link scanning needs the Message Content intent enabled in the Developer
# Portal FIRST. Turn it on there, then set this to 1 and restart.
ENABLE_MESSAGE_CONTENT=0
ENVFILE
  echo "Saved to $DIR/.env"
else
  echo "Keeping the existing $DIR/.env"
fi
chown "$RUN_USER:$RUN_USER" "$DIR/.env"
chmod 600 "$DIR/.env"

# --------------------------------------------------------------------------
say "Bringing your data across"
# --------------------------------------------------------------------------
# Everything the bot knows — builds, tickets, applications, settings — is in the
# public bot-data branch. Restoring needs no token because the repo is public.
if [ -f "$DIR/buildboard.db" ]; then
  echo "A database is already here; leaving it alone."
else
  if sudo -u "$RUN_USER" env \
       STATE_REMOTE="$REPO_URL" \
       DB_PATH="$DIR/buildboard.db" \
       SCHEMATIC_DIR="$DIR/schematics" \
       STATE_DIR="$DIR/.state" \
       "$DIR/.venv/bin/python" "$DIR/hosting/sync_state.py" restore; then
    echo "Restored from the bot-data branch."
  else
    warn "Couldn't restore the old data — the bot will start with an empty database."
    warn "Nothing is lost: it's still on the bot-data branch. Ask before running /setup again."
  fi
fi
chown -R "$RUN_USER:$RUN_USER" "$DIR"

# --------------------------------------------------------------------------
say "Installing the service"
# --------------------------------------------------------------------------
for unit in "$SERVICE.service" "$SERVICE-backup.service" "$SERVICE-backup.timer"; do
  sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DIR|g" \
      "$DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl enable --quiet --now "$SERVICE-backup.timer"
systemctl restart "$SERVICE"

sleep 5
if systemctl is-active --quiet "$SERVICE"; then
  say "The bot is running, and will start again by itself on reboot or crash."
else
  warn "It didn't stay up. The last few lines of the log:"
  journalctl -u "$SERVICE" -n 25 --no-pager || true
  die "Fix whatever that says, then run this script again."
fi

cat <<DONE

  Watch it live      sudo journalctl -u $SERVICE -f
  Is it running?     systemctl status $SERVICE
  Restart it         sudo systemctl restart $SERVICE
  Update to latest   sudo bash $DIR/deploy/update.sh

  One last thing: turn OFF the GitHub Actions workflow, or two bots will be
  online at once and every command gets answered twice.
      GitHub → Actions → "Run bot" → ⋯ → Disable workflow

DONE

#!/usr/bin/env bash
#
# Pull the newest code and restart the bot.  sudo bash /opt/astra-bot/deploy/update.sh
#
# The database and .env are untouched — only tracked files move, and both of
# those are ignored by git.

set -euo pipefail

DIR="${DIR:-/opt/astra-bot}"
SERVICE="astra-bot"
BRANCH="${BRANCH:-$(git -C "$DIR" rev-parse --abbrev-ref HEAD)}"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
[ -d "$DIR/.git" ] || { echo "No install at $DIR — run install.sh first." >&2; exit 1; }

RUN_USER=$(stat -c '%U' "$DIR")
BEFORE=$(git -C "$DIR" rev-parse --short HEAD)

git -C "$DIR" fetch --quiet origin "$BRANCH"
git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
chown -R "$RUN_USER:$RUN_USER" "$DIR"

sudo -u "$RUN_USER" "$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

AFTER=$(git -C "$DIR" rev-parse --short HEAD)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "Already up to date ($AFTER). Restarting anyway."
else
  echo "$BEFORE → $AFTER"
fi

systemctl restart "$SERVICE"
sleep 5

if systemctl is-active --quiet "$SERVICE"; then
  echo "Running."
else
  echo "It didn't come back up:" >&2
  journalctl -u "$SERVICE" -n 25 --no-pager >&2
  exit 1
fi

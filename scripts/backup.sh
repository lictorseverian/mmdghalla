#!/usr/bin/env bash
# Back up the world (already mirrored into ./world) to the `backup` branch, encrypted.
#
# The repo is public, and the save holds every player's platform ID, so nothing goes up unencrypted:
# each copy is a tar.gz sealed with AES-256 using the BACKUP_KEY secret. Without that key the files
# are useless to anyone, including us, so keep the key somewhere besides GitHub too.
#
# Kept on the branch (one commit, force-pushed so history doesn't grow):
#   latest.tar.gz.enc         the newest save
#   daily/YYYY-MM-DD.tar.gz.enc  the last save of each day, for the last 14 days
#
# What's inside: the whole world folder (worlds_local/<World>/: _main.N.fwl2/.db2/.chunks/.ok + the
# .chunk files) and, if present, adminlist.txt, bannedlist.txt and permittedlist.txt.
# Characters are not on the server: each player's character and inventory live on their own device.
#
# To restore on a new server:
#   1. download latest.tar.gz.enc (or a daily one) from the backup branch on GitHub
#   2. openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in latest.tar.gz.enc -out backup.tar.gz   (asks for the key)
#   3. tar -xzf backup.tar.gz   ->  Caraguatatuba/ and lists/
#   4. stop the new server, copy Caraguatatuba/ into .config/unity3d/IronGate/Valheim/worlds_local/
#      and the lists into .config/unity3d/IronGate/Valheim/, then start it with -world Caraguatatuba
#
# Needs: BACKUP_KEY, GH_TOKEN, GITHUB_REPOSITORY; SFTP_* for the lists (optional).
set -euo pipefail

: "${BACKUP_KEY:?no BACKUP_KEY secret set}"
ls world/_main.*.ok >/dev/null                      # only after a good pull
WORLD_NAME="${WORLD_NAME:-Caraguatatuba}"
WORLD_PATH="${WORLD_PATH:-.config/unity3d/IronGate/Valheim/worlds_local/$WORLD_NAME}"
KEEP_DAYS=14
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

mkdir -p "$work/pack/lists"
cp -r world "$work/pack/$WORLD_NAME"
if [ -n "${SFTP_HOST:-}" ]; then                      # server lists sit two folders above the world
  export LFTP_PASSWORD="$SFTP_PASSWORD"
  base="${WORLD_PATH%/worlds_local/*}"
  for root in "$base" "/home/container/$base"; do
    lftp -e "
      set sftp:auto-confirm yes; set net:timeout 30; set net:max-retries 2;
      open --env-password -u '$SFTP_USER' -p $SFTP_PORT sftp://$SFTP_HOST;
      set cmd:fail-exit no; lcd $work/pack/lists;
      get '$root/adminlist.txt'; get '$root/bannedlist.txt'; get '$root/permittedlist.txt';
      bye" >/dev/null 2>&1 || true
    ls "$work/pack/lists"/*.txt >/dev/null 2>&1 && break
  done
fi
echo "packing: $(ls "$work/pack/$WORLD_NAME" | wc -l) world files, lists: $(ls "$work/pack/lists" | tr '\n' ' ')"

seal() { tar -C "$work/pack" -czf - . | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_KEY -out "$1"; }

# start from what's on the branch now, so the daily copies carry over
mkdir -p "$work/branch"
cd "$work/branch"
git init -q -b backup
url="${BACKUP_REMOTE:-https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git}"
if git fetch -q --depth 1 "$url" backup 2>/dev/null; then git checkout -q FETCH_HEAD -- . 2>/dev/null || true; fi
mkdir -p daily
seal latest.tar.gz.enc
# verify before replacing anything: it must decrypt and list
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_KEY -in latest.tar.gz.enc | tar -tzf - > "$work/list"
grep -q '_main\..*\.ok$' "$work/list"
cp latest.tar.gz.enc "daily/$(TZ=America/Sao_Paulo date +%F).tar.gz.enc"
ls daily/*.tar.gz.enc | sort | head -n -$KEEP_DAYS | xargs -r rm -f
cat > README.txt <<'EOF'
Encrypted backups of the Valheim world. See scripts/backup.sh on the main branch for how to restore.
EOF
git add -A
git -c user.name="github-actions[bot]" -c user.email="41898099+github-actions[bot]@users.noreply.github.com" \
  commit -q -m "World backup $(date -u +%Y-%m-%dT%H:%MZ)"
git push -q --force "$url" HEAD:backup
echo "backed up: $(du -h latest.tar.gz.enc | cut -f1), daily copies: $(ls daily | tr '\n' ' ')"

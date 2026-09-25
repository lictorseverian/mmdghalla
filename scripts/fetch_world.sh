#!/usr/bin/env bash
# Mirror the world folder from the game server over SFTP into ./world
# Needs: SFTP_HOST SFTP_PORT SFTP_USER SFTP_PASSWORD, optional WORLD_PATH.
set -euo pipefail

: "${SFTP_HOST:?}" "${SFTP_PORT:?}" "${SFTP_USER:?}" "${SFTP_PASSWORD:?}"
WORLD_PATH="${WORLD_PATH:-.config/unity3d/IronGate/Valheim/worlds_local/Caraguatatuba}"
export LFTP_PASSWORD="$SFTP_PASSWORD"

fetch() {  # $1 = remote path
  rm -rf world
  lftp -e "
    set sftp:auto-confirm yes;
    set net:timeout 30; set net:max-retries 3; set net:reconnect-interval-base 5;
    open --env-password -u '$SFTP_USER' -p $SFTP_PORT sftp://$SFTP_HOST;
    mirror --parallel=4 --no-perms '$1' world;
    bye"
}

# Panel file managers often show the server root as /home/container; SFTP usually starts there already.
for path in "$WORLD_PATH" "/home/container/$WORLD_PATH"; do
  if fetch "$path" && ls world/_main.*.ok >/dev/null 2>&1; then
    echo "fetched $(ls world | wc -l) files from $path"
    exit 0
  fi
done
echo "fetch failed: check the lftp error above (login, host/port, or WORLD_PATH=$WORLD_PATH)" >&2
exit 1

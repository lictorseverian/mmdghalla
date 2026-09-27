#!/usr/bin/env bash
# Print the number of the server's latest completed world save (the N in _main.N.ok),
# by listing the world folder over SFTP. Cheap: no files are downloaded.
# Needs: SFTP_HOST SFTP_PORT SFTP_USER SFTP_PASSWORD, optional WORLD_PATH.
set -euo pipefail

: "${SFTP_HOST:?}" "${SFTP_PORT:?}" "${SFTP_USER:?}" "${SFTP_PASSWORD:?}"
WORLD_PATH="${WORLD_PATH:-.config/unity3d/IronGate/Valheim/worlds_local/Caraguatatuba}"
export LFTP_PASSWORD="$SFTP_PASSWORD"

for path in "$WORLD_PATH" "/home/container/$WORLD_PATH"; do
  names=$(lftp -e "
    set sftp:auto-confirm yes;
    set net:timeout 30; set net:max-retries 2; set net:reconnect-interval-base 5;
    open --env-password -u '$SFTP_USER' -p $SFTP_PORT sftp://$SFTP_HOST;
    cls -1 '$path/';
    bye" 2>/dev/null) || continue
  gen=$(printf '%s\n' "$names" | sed -n 's#.*_main\.\([0-9][0-9]*\)\.ok$#\1#p' | sort -n | tail -1)
  if [ -n "$gen" ]; then echo "$gen"; exit 0; fi
done
echo "could not list the world folder" >&2
exit 1

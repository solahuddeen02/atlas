#!/bin/sh
# One-command install/start for Atlas (SELF_HOSTING quick start).
# Creates the external volume(s) if missing, then `docker compose up -d`.
# Safe to re-run: it never deletes or recreates anything.
set -eu
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "install.sh: deploy/.env not found. Run: cp .env.example .env  (then edit it)" >&2
    exit 1
fi

# Last assignment wins, as in Compose. Surrounding quotes are stripped.
setting() {
    sed -n "s/^$1=//p" .env | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//"
}

volume="$(setting ATLAS_PGDATA_VOLUME)"
volume="${volume:-atlas_pgdata}"
if docker volume inspect "$volume" > /dev/null 2>&1; then
    echo "install.sh: using existing volume $volume"
else
    docker volume create "$volume" > /dev/null
    echo "install.sh: created volume $volume"
fi
# TODO(spike-s): also create ${ATLAS_IDP_VOLUME:-atlas_idp} when the bundled IdP
# (profile bundled-idp) is enabled.

exec docker compose up -d "$@"

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

ensure_volume() {
    if docker volume inspect "$1" > /dev/null 2>&1; then
        echo "install.sh: using existing volume $1"
    else
        docker volume create "$1" > /dev/null
        echo "install.sh: created volume $1"
    fi
}

volume="$(setting ATLAS_PGDATA_VOLUME)"
ensure_volume "${volume:-atlas_pgdata}"

# The bundled IdP's volume, only with its profile. A COMPOSE_PROFILES in the
# environment wins over deploy/.env, as in Compose.
profiles="${COMPOSE_PROFILES-$(setting COMPOSE_PROFILES)}"
case ",$(printf '%s' "$profiles" | tr -d ' ')," in
    *,bundled-idp,*)
        idp_volume="$(setting ATLAS_IDP_VOLUME)"
        ensure_volume "${idp_volume:-atlas_idp}"
        ;;
esac

exec docker compose up -d "$@"

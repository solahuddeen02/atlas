# deploy/

The v1 Compose stack. `atlas-proxy` is the only service that publishes a port: `${ATLAS_BIND_ADDRESS:-127.0.0.1}:${ATLAS_HTTP_PORT:-8080}`. Put a tunnel or your own TLS proxy in front of it ([ADR-003](../docs/adr/003-internet-exposure.md)). The legacy stack is in `legacy/docker-compose.yml`.

> ⚠️ **Never run `docker compose down -v`.** Stop with `docker compose down`.

| File | Purpose |
|---|---|
| `docker-compose.yml` | The stack: `atlas-init` → `postgres` → `atlas-db-init` → `atlas-migrate` → `atlas-api`, plus `atlas-backup` (profile `tools`) |
| `.env.example` | Non-secret settings template. Copy it to `.env` |
| `install.sh` | Creates the external volume if it's missing, then `docker compose up -d` |
| `nginx/` | `atlas-proxy`'s startup script (validates settings, renders the config) and the placeholder page |
| `compose.test-failing-migration.yml`, `test/` | **Test-only** overlays and images for `tests/compose`. Never use them for a deployment |

## Start

```bash
cp .env.example .env
```

Edit `.env` and set `ATLAS_PUBLIC_URL`. Until login lands (Phase 5), also put `ATLAS_OIDC_ISSUER` and `ATLAS_OIDC_CLIENT_SECRET` in `secrets/atlas/app.env`. Then:

```bash
./install.sh
```

Then check it: `curl -s http://127.0.0.1:8080/health` prints `{"status":"ok"}`.

Binding to anything other than loopback (`ATLAS_BIND_ADDRESS`) requires `ATLAS_TRUSTED_PROXY_CIDRS`; the proxy refuses to start without it (ADR-003 §1).

`atlas-init` generates every missing secret under `./secrets` and sets its ownership ([ADR-004](../docs/adr/004-config-and-secrets.md) §3). Re-running `up` never overwrites a value.

## Back up and restore

```bash
docker compose run --rm atlas-backup
```

Backups land in `./backups/atlas-<UTC timestamp>/`. To restore one, stop the API first:

```bash
docker compose stop atlas-api
```

```bash
docker compose run --rm atlas-backup atlas-admin restore --from /var/backups/atlas/<dir> --yes
```

```bash
docker compose start atlas-api
```

# deploy/

The v1 Compose stack. The reverse proxy (`atlas-proxy`), the only service that will publish a port, arrives in Phase 4 of [PLAN-v1](../docs/PLAN-v1.md). The legacy stack is in `legacy/docker-compose.yml`.

> ⚠️ **Never run `docker compose down -v`.** Stop with `docker compose down`.

| File | Purpose |
|---|---|
| `docker-compose.yml` | The stack: `atlas-init` → `postgres` → `atlas-db-init` → `atlas-migrate` → `atlas-api`, plus `atlas-backup` (profile `tools`) |
| `.env.example` | Non-secret settings template. Copy it to `.env` |
| `compose.test.yml`, `compose.test-failing-migration.yml`, `test/` | **Test-only** overlays and images for `tests/compose`. Never use them for a deployment |

## Start

```bash
cp .env.example .env
```

Edit `.env` and set `ATLAS_PUBLIC_URL`. Until login lands (Phase 5), also put `ATLAS_OIDC_ISSUER` and `ATLAS_OIDC_CLIENT_SECRET` in `secrets/atlas/app.env`. Then:

```bash
docker volume create atlas_pgdata
```

```bash
docker compose up -d
```

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

# ADR-005: Database and migrations: PostgreSQL, Alembic honouring `DATABASE_URL`, migrations at container start, data that survives `down -v`

- **Status:** Proposed
- **Date:** 2026-10-29. Revised for generic self-hosting.
- **Related:** [AUDIT](../AUDIT.md) §1.5, §2.2, §2.4, §6, [ADR-004](004-config-and-secrets.md), [ARCHITECTURE](../ARCHITECTURE.md) §7, [SELF_HOSTING](../SELF_HOSTING.md)

## Context

The audit found:

- `alembic/env.py` ignores `DATABASE_URL`.
- The Dockerfile migrates **at build time** into the image layer.
- The app's `create_all()` builds the real schema at runtime with no `alembic_version`.
- The `owner_id` migration can't upgrade a populated DB.
- The schema has no FKs or indexes, uses TEXT timestamps in two formats, and has a free-text `type`.

`origin/dev` already fixes part of this: a Postgres compose service with a healthcheck, `env.py` honouring `DATABASE_URL`, and an entrypoint that runs `alembic upgrade head` before uvicorn.

Self-hosters will run `docker compose down -v` by accident, sometimes following generic tutorials. Data must survive it.

## Decision

### 1. PostgreSQL only

- PostgreSQL 17 (official image, pinned by digest), via **psycopg 3** (`postgresql+psycopg://`).
- SQLite is dropped for the platform. Tests use a real Postgres (a CI service container, and `compose.test.yml` locally), because RLS, `timestamptz`, JSONB and advisory locks must behave as in production.
- **One database, one schema per module.**
  - Schema `kernel` holds users, permissions, role_permissions, user_permission_overrides, sessions, audit_log, modules, connectors, outbox, rate_limits and settings.
  - Each module owns its own schema (`drive`, `lift`, …).
  - Cross-schema FKs are allowed only *to* `kernel.users`.
- **Roles** (created by the one-shot `atlas-db-init` service, `atlas-admin db-init`, using the superuser password from `postgres/password`; see the note below):
  - `atlas_migrator` owns the schemas and runs DDL.
  - `atlas_app` gets DML only, and only `INSERT/SELECT` on `audit_log`. The API uses it, so an app compromise can't `DROP` tables or rewrite the audit trail.
- **Time:**
  - Every timestamp is `timestamptz`, stored in UTC, and the DB session timezone is UTC.
  - `TZ` (default `UTC`) affects only container clocks, logs, scheduled job times and default display formatting. The UI shows times in each user's browser time zone. That fixes the audit's mixed-format TEXT timestamps.
- **Keys and constraints:**
  - UUIDv7 primary keys for anything exposed in URLs, so ids can't be enumerated.
  - FKs everywhere, `CHECK`/enum constraints, and indexes on every filter column.

### 2. Alembic, configured from `Settings`

- `env.py` builds the URL from `Settings`, using `ATLAS_MIGRATOR_DATABASE_URL`, which falls back to `DATABASE_URL`.
- `sqlalchemy.url` is removed from `alembic.ini`, so a missing URL fails instead of silently creating `./atlas.db`.
- **Per-module version locations with branch labels:**
  - Kernel migrations live in `atlas/kernel/migrations/`, and each module's in `modules/<id>/migrations/`.
  - The registry supplies the installed modules' locations, and each branch is labelled with the module id.
  - Module migrations may `depends_on` kernel revisions.
  - Upgrade target is `heads`.
- **Fresh baseline:**
  - v1 starts with `0001_kernel_baseline`.
  - The legacy migrations (`87f6cab4a51a`, `c99f9c7e897e`, `c1fdc6c1bce2`) stay with the legacy code until the v2 files module, which gets its own baseline and a one-off import from any legacy SQLite DB.
- **Rules:**
  - Forward-only in production. Downgrades exist for dev only.
  - Destructive changes follow expand → migrate → contract across releases.
  - Adding a `NOT NULL` column needs a default or a backfill, which prevents the `owner_id` failure class.
  - CI runs `upgrade heads` on an empty DB and on a seeded DB from the previous release, plus `alembic check`.
- **`create_all()` is deleted from app startup.** Tests build schemas through Alembic.

### 3. Migrations run at container start, not at build time

- A one-shot **`atlas-migrate`** service uses the same image as the API and runs `atlas-admin migrate`. That command does:
  1. Takes a Postgres advisory lock.
  2. If revisions are pending, writes a `pg_dump -Fc` to the backup location, and **aborts if the dump fails**.
  3. Runs `alembic upgrade heads` as `atlas_migrator`.
  4. Runs idempotent seed steps: built-in roles, manifest permissions and default grants, registry sync.
  5. Exits 0.
- `atlas-api` has `depends_on: { atlas-init: completed_successfully, postgres: healthy, atlas-migrate: completed_successfully }`.
- **Role setup is its own step (amended in Phase 3).** `atlas-init` must run *before* Postgres starts, because Postgres reads the password file it writes. But roles can only be created *after* Postgres is healthy. So the order is:
  1. `atlas-init` (`atlas-admin init`) writes the secrets;
  2. `postgres` starts;
  3. `atlas-db-init` (`atlas-admin db-init`) runs as superuser. It creates `atlas_migrator` and `atlas_app` if missing and updates a password only when the one in `app.env` no longer works, so a rotated password gets applied. It also creates the database and the `kernel` schema (owned by `atlas_migrator`) and locks down `public`;
  4. `atlas-migrate`;
  5. `atlas-api`.

  Only `atlas-db-init` mounts `postgres/password`. Neither the API nor migrations ever see the superuser password.
- If a migration fails, the API doesn't start with a half-migrated schema, and the operator gets a clear log.
- **Why not dev's entrypoint-runs-alembic-then-uvicorn?** It would give the long-running API DDL rights and run once per replica or restart. A separate service keeps the API on `atlas_app`. Dev's idea (run at start, not build) is kept.

### 4. Data survives `docker compose down -v`

- **The Postgres data volume is declared `external: true`** under a configurable name (`ATLAS_PGDATA_VOLUME`, default `atlas_pgdata`).
  - Compose never creates or deletes external volumes, so `down -v` can't remove it.
  - The quick start creates it once (`docker volume create atlas_pgdata`, see [SELF_HOSTING](../SELF_HOSTING.md)).
  - If it's missing, `up` fails loudly instead of starting with an empty database.
- The same applies to the bundled IdP's data volume (`ATLAS_IDP_VOLUME`, default `atlas_idp`).
- Secrets and backups live on **host paths** (`ATLAS_SECRETS_DIR`, `ATLAS_BACKUP_DIR`), so `down -v` can't touch them either.
- **Backups:**
  - `docker compose run --rm atlas-backup` writes a `pg_dump -Fc` plus an IdP backup into `ATLAS_BACKUP_DIR`, with retention `ATLAS_BACKUP_KEEP_DAILY` (default 7) and `ATLAS_BACKUP_KEEP_WEEKLY` (default 4).
  - Scheduling it (cron, a systemd timer, your NAS's scheduler) and copying backups off the host are the operator's choice. SELF_HOSTING shows a generic example.
  - A **restore drill** is part of the v1 release checklist.
- `docker-compose.yml` starts with a comment and SELF_HOSTING has a warning box: **never run `docker compose down -v`.**

### 5. What to reuse from `origin/dev`

| From `origin/dev` | Decision | Notes |
|---|---|---|
| `alembic/env.py` honouring `DATABASE_URL` | **Reuse the idea, reimplement it** | Via `Settings` instead of `os.environ` |
| `entrypoint.sh` (migrate at runtime) | **Reuse the idea** | As the `atlas-migrate` one-shot service with an advisory lock and pre-migration dump |
| Postgres compose service + `pg_isready` healthcheck | **Reuse** | Change: external named volume, `POSTGRES_PASSWORD_FILE`, no literal password, pinned image |
| `psycopg2-binary` | **Replace** | Use psycopg 3 |
| `core/` vs `apps/` split | **Reuse the concept** | It becomes `atlas/kernel` + `atlas/sdk` + `modules/*` with import-linter contracts |
| Tenant model (`tenants`, `tenant_members`, tenant/role in JWT) | **Dropped (owner decision)** | One household per instance ([VISION](../VISION.md) non-goals). Trusting tenant and role from the JWT was itself a flaw (AUDIT §6) |
| Invite tokens | **Do not reuse** | Replaced by admin-created accounts, IdP reset tokens, or link codes for BYO ([ADR-001](001-identity.md)) |
| Share links | **Defer to v2** | Redesign on kernel `shares` ([ADR-002](002-authorization.md)) with a `deleted_at` check, hashed tokens and `timestamptz` expiry |
| Pillow thumbnails | **Defer to v2** (likely moot if Photos is the Immich link) | If built: background worker, decompression-bomb guard, size caps |
| Admin stats endpoint | **Defer** | Rebuild on module-provided metrics |
| Migrations `a1b2c3d4e5f6`, `b2c3d4e5f6a7`, `d4e8f2a9b3c5` | **Do not reuse** | Superseded by the fresh kernel baseline |

## Consequences

- Dev and tests need Docker for Postgres (`make db`).
- First install has one extra step: create the external volumes. The quick start includes it, and `atlas-init` prints a clear error if they're missing.
- Legacy SQLite data is imported once in v2. v1 doesn't touch it.

## Verification

- CI:
  - upgrade on an empty DB;
  - upgrade from the previous release's seeded snapshot;
  - `alembic check`;
  - schema lint (`timestamptz` only, an FK and index on every `*_id`).
- `atlas_app` gets `permission denied` for DDL, and for `UPDATE`/`DELETE` on `audit_log`.
- Compose tests:
  - the pg and IdP volumes are external;
  - `down -v` followed by `up` keeps the data;
  - a failing migration blocks `atlas-api`;
  - a missing volume makes `up` fail loudly.
- Release checklist: restore the latest backup into a scratch stack and run the smoke tests.

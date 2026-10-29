# ADR-005: Database and migrations: PostgreSQL, Alembic honouring `DATABASE_URL`, migrations at container start, data that survives `down -v`

- **Status:** Proposed
- **Date:** 2026-10-29
- **Related:** [AUDIT](../AUDIT.md) §1.5, §2.2, §2.4, §6, [ADR-004](004-config-and-secrets.md), [ARCHITECTURE](../ARCHITECTURE.md) §7

## Context

The audit found:

- `alembic/env.py` ignores `DATABASE_URL`.
- The Dockerfile migrates **at build time** into the image layer.
- The app's `create_all()` builds the real schema at runtime with no `alembic_version`.
- The `owner_id` migration can't upgrade a populated DB.
- The schema has no FKs or indexes, uses TEXT timestamps in two formats, and has a free-text `type`.

`origin/dev` already fixes part of this: a Postgres compose service with a healthcheck, `env.py` honouring `DATABASE_URL`, and an entrypoint that runs `alembic upgrade head` before uvicorn.

**Deploy contract:**
- PostgreSQL data lives in a **named Docker volume**.
- `TZ=Asia/Bangkok`.
- **Never `docker compose down -v`.**

## Decision

### 1. PostgreSQL only (for deployed environments)

- PostgreSQL 17 (official image, pinned by digest), driven by **psycopg 3** (`postgresql+psycopg://`).
- SQLite is dropped for the platform. Tests use a real Postgres (a CI service container and a local `docker compose -f compose.test.yml`), so test behaviour matches production. That matters for RLS, `timestamptz`, JSONB and advisory locks.
- **One database, one schema per module.**
  - The kernel uses schema `kernel` (users, role_permissions, user_permission_overrides, sessions, audit_log, modules, connectors, outbox, rate_limits).
  - Each module owns its own schema (`drive`, `lift`, …) ([ARCHITECTURE](../ARCHITECTURE.md) §7).
  - Cross-schema FKs are allowed only *to* `kernel.users`.
- **Roles:**
  - `postgres` (superuser) is used only by the init script.
  - `atlas_migrator` owns the schemas and runs DDL.
  - `atlas_app` gets DML only (`SELECT/INSERT/UPDATE/DELETE`) and no DDL, and is used by the API.
  - So an app compromise can't `DROP` tables.
- **Conventions:**
  - Every timestamp is `timestamptz`, stored in UTC; the session timezone is UTC.
  - `TZ=Asia/Bangkok` affects only container clocks, logs and display formatting. It never affects stored values, which fixes the mixed-format TEXT timestamps.
  - Bigint or UUIDv7 primary keys: UUIDv7 for anything exposed in URLs, so ids can't be enumerated.
  - FKs everywhere, `CHECK` constraints for enums (or native enums), indexes on every filter column.

### 2. Alembic, configured from `Settings`

- `alembic/env.py` builds the URL from `Settings` (which reads `DATABASE_URL`), never from `alembic.ini`. `sqlalchemy.url` is removed from the ini file, so a missing URL fails instead of silently creating `./atlas.db`.
- **Per-module version locations with branch labels:**
  - Kernel migrations live in `atlas/kernel/migrations/`.
  - Each module's live in `modules/<id>/migrations/`.
  - The registry passes the installed modules' locations to Alembic, and each module has its own branch label (`kernel`, `drive`, …).
  - Module migrations may `depends_on` kernel revisions.
  - Upgrade target is `heads`.
- **Fresh baseline:**
  - v1 starts with a new kernel baseline revision, `0001_kernel_baseline`.
  - The three legacy migrations (`87f6cab4a51a`, `c99f9c7e897e`, `c1fdc6c1bce2`) are **not** carried into the platform chain. They stay with the legacy code until the v2 files module, which gets its own baseline and a one-off data import from any legacy SQLite DB.
- **Migration rules:**
  - Forward-only in production. Downgrades exist for dev only.
  - Destructive changes follow expand → migrate → contract across releases.
  - Adding a `NOT NULL` column requires a default or a backfill step, which prevents the `owner_id` class of failure.
  - CI runs `alembic upgrade heads` on an empty DB and on a seeded DB from the previous release, then `alembic check` (the models match the migrations).
- **`create_all()` is deleted from app startup.** Tests build schemas through Alembic too, so tests exercise the real migrations.

### 3. Migrations run at container start, not at build time

- A one-shot **`atlas-migrate`** compose service uses the same image as the API and runs `atlas-admin migrate`. That command does:
  1. Takes a Postgres advisory lock, so only one migrator ever runs.
  2. Runs `alembic upgrade heads` as `atlas_migrator`.
  3. Runs idempotent seed steps: built-in roles, default role→permission grants from manifests, and registry sync.
  4. Exits 0 on success.
- `atlas-api` has `depends_on: { atlas-migrate: { condition: service_completed_successfully }, postgres: { condition: service_healthy } }`.
- If a migration fails, the API doesn't start with a half-migrated schema. The previous containers keep serving until the operator acts.
- **Why not dev's entrypoint-runs-alembic-then-uvicorn?** It works, but it gives DDL rights to the long-running API process and runs the migration once per replica or restart. A separate service keeps the API on `atlas_app` (DML only). Dev's `entrypoint.sh` *idea* (runtime, not build-time) is kept.
- **Pre-migration backup.** `atlas-migrate` first runs `pg_dump -Fc` into the backup volume when there are pending revisions, and refuses to proceed if the dump fails.

### 4. Data survives `docker compose down -v`

The contract says "never run it". The design also makes it harmless:

- **The Postgres data volume is declared `external: true`** (named, e.g. `atlas_pgdata`).
  - Compose never creates or deletes external volumes, so `down -v` cannot remove it.
  - It's created once by the runbook or Terraform with `docker volume create atlas_pgdata`.
  - If it's missing, `up` fails loudly instead of starting with an empty DB.
- The same applies to the IdP data volume and the backup volume.
- Backups:
  - nightly `pg_dump -Fc` from a scheduled job (host cron or Terraform-managed), with retention 7 daily + 4 weekly;
  - copied off-VM by existing infra (out of scope here; no details in this public repo);
  - a **restore drill** is part of the v1 acceptance criteria.
- A runbook line in the README and a comment at the top of `docker-compose.yml` say: *never `docker compose down -v`*.

### 5. What to reuse from `origin/dev`

| From `origin/dev` | Decision | Notes |
|---|---|---|
| `alembic/env.py` honouring `DATABASE_URL` | **Reuse (idea), reimplement** | Read it via `Settings` instead of `os.environ` |
| `entrypoint.sh` (migrate at runtime) | **Reuse the idea** | As the `atlas-migrate` one-shot service with an advisory lock |
| Postgres compose service + `pg_isready` healthcheck | **Reuse** | Change: external named volume, separate env file, no literal password, image pinned |
| `psycopg2-binary` | **Replace** | Use psycopg 3 |
| `core/` (platform) vs `apps/` (features) split | **Reuse the concept** | It becomes `atlas/kernel` + `atlas/sdk` + `modules/*`, with import-linter contracts |
| Tenant model (`tenants`, `tenant_members`, tenant_id in JWT) | **Do not reuse** | One household means no multi-tenancy (see [VISION](../VISION.md) non-goals). Family roles replace tenant roles. Trusting role and tenant from the JWT was itself a flaw (AUDIT §6) |
| Invite tokens | **Do not reuse** | Replaced by admin-created accounts and IdP reset/enrolment tokens ([ADR-001](001-identity.md)) |
| Share links | **Defer to v2** | Redesign on the `shares` table ([ADR-002](002-authorization.md)) with a `deleted_at` check, hashed tokens and expiry as `timestamptz` (fixes AUDIT §6 findings) |
| Pillow thumbnails | **Defer to v2** | Generate in a background worker, with a decompression-bomb guard and size caps |
| Admin stats endpoint | **Defer** | Rebuild on module-provided metrics after v2 |
| Migrations `a1b2c3d4e5f6`, `b2c3d4e5f6a7`, `d4e8f2a9b3c5` | **Do not reuse** | The fresh kernel baseline supersedes them |

## Consequences

- Local dev and tests need Docker for Postgres. That's acceptable, and a `make db` target wraps it.
- Legacy SQLite data (if any exists) is imported once, in v2, by a script. v1 doesn't touch it.
- Two DB roles and an init script add a little setup, in exchange for an API that can't alter the schema.
- `external: true` volumes need a one-time create step on new hosts. The runbook and Terraform own that.

## Verification

- CI:
  - `alembic upgrade heads` on an empty DB;
  - upgrade from the previous release's seeded snapshot;
  - `alembic check`;
  - a schema-lint test (every timestamp is `timestamptz`, every `*_id` column has an FK, every FK column has an index).
- A test asserts `atlas_app` can't run DDL (expects `permission denied`).
- Compose test:
  - `docker compose config` shows the pg volume as external;
  - `down -v` followed by `up` keeps the data (run in CI against a throwaway volume);
  - the API doesn't start if `atlas-migrate` fails (injected failing migration).
- Restore drill: restore last night's dump into a scratch DB and run the smoke tests against it. This is a release checklist item.

# Atlas architecture (Foundation target)

- **Status:** Proposed design. Nothing here is implemented yet.
- **Reads with:** [VISION](VISION.md) · [SELF_HOSTING](SELF_HOSTING.md) · [AUDIT](AUDIT.md) · ADRs [001 identity](adr/001-identity.md) · [002 authorization](adr/002-authorization.md) · [003 internet exposure](adr/003-internet-exposure.md) · [004 config & secrets](adr/004-config-and-secrets.md) · [005 database & migrations](adr/005-database-and-migrations.md) · [PLAN-v1](PLAN-v1.md)

---

## 1. Shape: a modular monolith

Atlas is:
- **one backend process** (FastAPI);
- **one web app** (a React SPA, served as static files by the bundled proxy);
- **a few supporting containers**, all in one Docker Compose project.

Inside the backend there is a **kernel**, the platform, and a set of **modules**, the features. Modules are first-party Python packages in this repo. They are compiled in and switched on or off by an admin. There is no runtime plugin loading.

Why a monolith:
- One household and one host.
- Small maintenance capacity.
- One Postgres.
- Atomic transactions across the kernel and a module.

Module boundaries are enforced in code (import contracts, the SDK and per-module DB schemas), not over the network.

### Containers (default `docker-compose.yml`)

| Service | Role | Published port | Lifetime |
|---|---|---|---|
| `atlas-init` | Generates missing secrets and the internal IdP CA/cert, creates DB roles, bootstraps the bundled IdP (ADR-004, ADR-001). Idempotent | — | One-shot, every `up` |
| `postgres` | PostgreSQL 17, data in an **external** named volume (ADR-005) | — | Long-running |
| `idp` | Bundled Kanidm. Compose profile `bundled-idp`, on by default. Internal TLS only | — | Long-running |
| `atlas-migrate` | `atlas-admin migrate`: lock → pre-migration dump → `alembic upgrade heads` → seed | — | One-shot, every `up` |
| `atlas-api` | FastAPI kernel + enabled modules, as `atlas_app` (DML only) | — | Long-running |
| `atlas-proxy` | nginx: serves the SPA, proxies `/api` and `/health`, re-encrypts to `idp`, applies security headers and rate limits | **`${ATLAS_BIND_ADDRESS:-127.0.0.1}:${ATLAS_HTTP_PORT:-8080}`**, the only one | Long-running |
| `atlas-backup` | `pg_dump` + IdP backup into `ATLAS_BACKUP_DIR` | — | Run on demand or by the operator's scheduler |
| `cloudflared` | Optional Cloudflare Tunnel connector (profile `tunnel`, off by default). Forwards public hostnames to `atlas-proxy` over the compose network | — | Long-running, when enabled |

```
        public TLS + exposure (operator's choice, ADR-003): Cloudflare Tunnel (recommended) │ own TLS proxy │ VPN
                                                │
                                                ▼  http://<ATLAS_BIND_ADDRESS>:<ATLAS_HTTP_PORT>   (loopback by default)
  ┌─────────────────────────────── docker compose project ───────────────────────────────┐
  │  atlas-proxy ─┬─ Host = Atlas URL ─▶ SPA (static)                                     │
  │               │                    └▶ atlas-api ──▶ postgres  (external volume)      │
  │               └─ Host = IdP URL ───▶ idp (Kanidm, internal TLS) ──▶ idp volume        │
  │  atlas-init (one-shot) · atlas-migrate (one-shot) · atlas-backup (on demand)          │
  └───────────────────────────────────────────────────────────────────────────────────────┘
  Immich (separate stack, its own hostname) ◀─ OIDC ─ idp          atlas-api ─ connector (HTTP) ─▶ Immich
```

**With bring-your-own OIDC** ([ADR-001](adr/001-identity.md)):
- the `bundled-idp` profile is off, so there's no `idp` service and no IdP vhost;
- `atlas-api` talks to the external issuer;
- `atlas-init` skips IdP bootstrap.

## 2. Code layout and module boundaries

```
atlas/
  kernel/            platform internals. Only the kernel (and sdk) imports these.
    config.py        Settings (ADR-004). The only place that reads the environment or env files
    db/              engine, unit-of-work, Alembic env, kernel migrations
    identity/        OIDC relying party, sessions, step-up, account linking, IdentityAdmin (kanidm | manual | fake)
    authz/           Principal, permission registry, policy, scoped repositories (ADR-002)
    registry/        module discovery, manifest validation, enable/disable, nav
    events/          event types, outbox, dispatcher
    connectors/      connector types, config store, health checks
    audit/           audit log writer
    admin/           admin-console API (users, roles, modules, connectors, sessions, audit)
    web/             create_app(), middleware (request id, security headers, CSRF, rate limit), /health
    cli/             atlas-admin: init, migrate, backup, restore, bootstrap-owner, break-glass, rotate-*
  sdk/               the ONLY import surface for modules (stable, versioned)
modules/
  hello/             tiny reference module (used by tests; Foundation)
  immich/            connector module: nav link, health, SSO hand-off (Foundation)
  files/ drive/      (First modules, ported from today's legacy code)
  lift/              (Household apps)
web/                 React SPA: shell, admin console, web/src/modules/<id>/
deploy/              docker-compose.yml, .env.example, nginx templates, compose.test.yml
legacy/              today's core/, frontend/, tests/, alembic/ (moved in Phase 1); not mounted in the new app
```

**Boundary rules, enforced in CI with `import-linter`:**
1. `modules.*` may import `atlas.sdk` and third-party libraries only, never `atlas.kernel.*` or another module.
2. `atlas.sdk` may import `atlas.kernel.*`. It is the facade.
3. `atlas.kernel.*` never imports `modules.*`. Discovery happens through a configured list or entry points.
4. Modules interact through **events** (§5) or interfaces exported in the manifest (`provides=`) and consumed via `requires=`. The registry wires them up and rejects cycles.

## 3. The module manifest

Each module exposes `MANIFEST: ModuleManifest` in `modules/<id>/__init__.py`. The registry validates it at startup and **refuses to boot** if it's invalid, if ids are duplicated, or if any route is undeclared.

```python
MANIFEST = ModuleManifest(
    id="drive",                       # [a-z][a-z0-9_]{1,30}; also DB schema name and URL prefix
    name="Drive",
    version="2.0.0",
    sdk=">=1,<2",
    requires=["files"],
    provides=[],

    permissions=[                     # ADR-002: <module>.<resource>.<action>
        Permission("drive.access",        "Open Drive",          default_roles=["owner","admin","adult","child"]),
        Permission("drive.files.read",    "View files",          default_roles=["owner","admin","adult","child","guest"]),
        Permission("drive.files.write",   "Upload/rename/move",  default_roles=["owner","admin","adult","child"]),
        Permission("drive.share.public",  "Create public links", default_roles=["owner","admin"], ceiling_excludes=["child","guest"]),
        Permission("drive.admin.purge",   "Purge any trash",     default_roles=["owner"], step_up=True),
    ],

    routes=lambda r: drive_routes(r), # r = atlas.sdk.router(module); every endpoint declares permission= or public=True
    nav=[NavItem(label="Drive", path="/drive", icon="folder", permission="drive.access", order=10)],
    migrations="modules/drive/migrations",
    settings=DriveSettings,           # pydantic model from ATLAS_MODULE_DRIVE_* (non-secret) + secret_refs
    events=Subscriptions({"kernel.user.disabled": on_user_disabled}),
    connectors=[],
    jobs=[],                          # periodic jobs; single-instance via advisory lock
)
```

| Field | Kernel behaviour |
|---|---|
| `id` | URL prefix `/api/v1/m/<id>/…`, DB schema `<id>`, settings prefix `ATLAS_MODULE_<ID>_`, nav namespace |
| `permissions` | Upserted into the permission registry. Default grants are seeded **only for new permissions**, so admin edits survive upgrades. Ceilings are always enforced |
| `routes` | Mounted behind a guard: module enabled **and** `<id>.access` held, otherwise **404**. Each route's permission is checked before the handler runs |
| `nav` | `GET /api/v1/nav`, filtered by enablement and the caller's permissions |
| `migrations` | Added to Alembic version locations. Run by `atlas-migrate` whether or not the module is enabled |
| `settings` | Validated at startup. Invalid settings mark the module `misconfigured` (it can't be enabled). Invalid kernel settings stop startup (ADR-004) |
| `events` | Handlers registered with the dispatcher |
| `requires` | Dependencies must be enabled first. Disabling one with enabled dependants is refused |

**Lifecycle:** `installed` → `enabled` / `disabled` by an admin (step-up, audited). `misconfigured` is computed and blocks enabling.

**Frontend:**
- Module UI lives in `web/src/modules/<id>/` and is code-split (`React.lazy`).
- Routes are built from `/api/v1/nav`, so disabled modules' chunks are never requested.
- No remotely loaded JavaScript.

## 4. Connectors

A **connector** is a typed, admin-managed link from Atlas to an external service the household runs: Immich first, later perhaps a Nextcloud migration source or Hermes. Connectors are about configuration and trust only. **They never provision or manage infrastructure.**

| Part | Description |
|---|---|
| **Connector type** | Code declared by a module. It defines a config schema (non-secret, e.g. `base_url`), `secret_refs` (env var names, ADR-004), capabilities (`health`, `sso_link`, `user_sync`, `api`) and a client factory |
| **Instance** | A row in `kernel.connectors`: `id, type, name, config (JSONB, validated), enabled, status, last_checked_at`. Managed in the console (`connectors.manage`, step-up) |
| **Secrets** | Never in the DB in Foundation. They live in `${ATLAS_SECRETS_DIR}/atlas/connectors.env` and are referenced by name. The console shows "set" or "missing" |
| **Health** | Scheduled `health()` with timeouts and backoff. Status shown in the console and in `/health/ready` details (trusted peers only) |
| **Access** | Modules get a ready client from `sdk.connector("<name>")` behind the module's permission. Modules never see raw secrets |
| **Outbound safety** | The client allow-lists the configured `base_url` host. No user-supplied URLs are fetched, which prevents server-side request forgery (SSRF) |

**Immich connector (Foundation):**
- **Health:** server ping and version.
- **Nav:** "Photos (Immich)" opens the configured Immich URL, and Immich signs the user in through the same IdP via SSO.
- **Access:** the console's per-user "Immich access" toggle maps to the IdP group that Immich's OIDC client allows (bundled IdP). With BYO, the operator maps the group in their IdP, and the toggle only controls Atlas's nav entry.

## 5. Event bus

The event bus is in-process with a transactional outbox, so it's durable without extra infrastructure.

**Event envelope:**

```json
{ "id": "uuidv7", "type": "kernel.user.created", "version": 1, "occurred_at": "…Z",
  "actor": {"user_id": "…", "kind": "user|system"}, "subject": {"type": "user", "id": "…"},
  "data": { … }, "request_id": "…" }
```

**Publishing:**
- `sdk.emit(event)` inserts into `kernel.outbox` **in the caller's transaction**. A rollback means no event.

**Dispatch:**
- A kernel background task claims committed rows (`FOR UPDATE SKIP LOCKED`) and calls the handlers.
- Delivery is at-least-once, with retries and backoff. Rows go to `dead` after N failures, and the console shows them.
- Handlers must be idempotent, keyed by event `id`.

**Naming:**
- `<module>.<entity>.<verb>` in past tense, with a pydantic schema and an integer `version` per type.

**Foundation kernel events:**
- `kernel.user.created|updated|disabled|enabled|deleted`
- `kernel.user.role_changed`
- `kernel.session.revoked`
- `kernel.module.enabled|disabled`
- `kernel.connector.created|updated|status_changed`
- `kernel.auth.login_succeeded|login_failed|step_up`

**Audit vs. events:** the audit log is written **synchronously** in the same transaction, and it's never derived from events. Events are for reactions.

## 6. Configuration

| Layer | Holds | Edited by |
|---|---|---|
| Code defaults | Safe, generic, non-secret defaults. **No secret has a default** | Developers |
| `.env` next to `docker-compose.yml` (untracked; template `.env.example`) | Non-secret deployment settings: bind address, port, `TZ`, public URLs, secrets/backup dirs, volume names, compose profiles, image tag | Operator |
| `${ATLAS_SECRETS_DIR}` env files (untracked, `0600`, generated if missing) | All secrets (ADR-004) | `atlas-init`, or the operator |
| Real environment variables | Override anything above (for operators who inject config differently) | Operator |
| DB runtime settings (`kernel.modules`, `kernel.connectors`, `kernel.role_permissions`, `kernel.settings`) | Admin-editable, **non-secret** runtime choices | Admin console (audited, step-up) |

- `Settings` is built once, validated, and **fails closed** (ADR-004).
- Module settings use the prefix `ATLAS_MODULE_<ID>_`.
- DB settings cover only things the environment doesn't define, so there's no ambiguity about which value wins.
- Every variable is listed in [SELF_HOSTING](SELF_HOSTING.md#configuration-reference).

## 7. Data and storage

**PostgreSQL** (ADR-005):
- One database. Schema `kernel` plus one schema per module.
- Roles `atlas_migrator` (DDL) and `atlas_app` (DML only).
- `timestamptz` in UTC everywhere. FKs and indexes mandatory. UUIDv7 for anything exposed in URLs.
- External named volume, so `down -v` is safe.

**Kernel tables (Foundation):**

| Table | Purpose |
|---|---|
| `users` | `id, idp_issuer, idp_sub (unique together), username, display_name, role, status, expires_at, created_at, updated_at` |
| `account_links` | One-time link codes for BYO OIDC (hashed, expiring) |
| `permissions`, `role_permissions`, `user_permission_overrides` | ADR-002 |
| `sessions` | ADR-003 |
| `audit_log` | Append-only. `atlas_app` has `INSERT/SELECT` only |
| `modules`, `connectors`, `outbox`, `rate_limits`, `settings` | — |

**Volumes and host paths** (all names and paths configurable; details in SELF_HOSTING):

| Data | Where | Survives `down -v`? |
|---|---|---|
| Postgres data | External volume `${ATLAS_PGDATA_VOLUME:-atlas_pgdata}` | ✓ (external) |
| Bundled IdP data | External volume `${ATLAS_IDP_VOLUME:-atlas_idp}` | ✓ (external) |
| Secrets | Host dir `${ATLAS_SECRETS_DIR:-./secrets}` | ✓ (host path) |
| Backups | Host dir `${ATLAS_BACKUP_DIR:-./backups}` | ✓ (host path) |
| Blob storage (First modules) | External volume `${ATLAS_FILES_VOLUME:-atlas_files}`, or S3-compatible storage | ✓ |

**Blob storage** (First modules) keeps the audit's good parts and fixes the bad ones:
- A kernel `StorageBackend` exposed via the SDK.
- **Backend-relative keys** (`objects/<uuid>`).
- Streaming, with size limits enforced while streaming.
- Server-side MIME sniffing.
- `serve()` returns a response or a presigned URL.
- S3-compatible storage is optional and tested against a container.
- Foundation stores no user files.

## 8. Mapping today's code to future modules (First modules)

| Today (`main`) | Becomes | Changes during the port |
|---|---|---|
| `core/storage/*` | **Kernel service** `atlas.kernel.storage` via `sdk.storage()` | Relative keys; streaming; size caps; MIME sniffing; recovery inside the backend; S3 streaming plus tests |
| `core/db/models.py::Object` + `core/domain/objects.py` | **`files` module** (schema `files`) | FKs, enum `kind`, `timestamptz`, indexes; `get_for/list_for` everywhere (ADR-002); recursive CTE; restore and purge recurse; purge only from trash; RLS backstop |
| `core/apps/objects/router.py` | Split between **`files`** (item operations) and **`drive`** (user endpoints) | Typed bodies and response models (no storage paths), bounded pagination, server search |
| `core/apps/drive/router.py` | **`drive` module** (`requires=["files"]`) | Parent loaded via `get_for(principal, "write", parent_id)`, which removes AUDIT O1–O3 by construction |
| `core/apps/photos/router.py` + `PhotosView` | **Undecided.** Leaning: **Photos = the Immich connector** (nav link + SSO), with no Atlas gallery | Decided at the start of First modules. If a light gallery is kept: a `photos` module over `files`, background thumbnails, server-side MIME. Otherwise the legacy photos code is retired |
| `core/apps/auth/*` | **Deleted** | Replaced by kernel identity (ADR-001) |
| `recover_incomplete_uploads` | `files` module **job** | Age-scoped, single-instance, uses the storage interface |
| `origin/dev` share links | Sharing on kernel `shares` | Hashed tokens, `timestamptz` expiry, `deleted_at` checked, child/guest ceilings |
| `origin/dev` tenants | **Dropped** | One household per instance |
| `frontend/src/*` views | `web/src/modules/drive` (and `photos` if kept) | API client with 401 → re-login, TanStack Query, router URLs |
| Legacy SQLite data | One-off import script into `files` | Map integer ids; map owners to linked users |

During Foundation, today's code sits untouched under `legacy/`, except for the Phase 0 security patches. It is **not mounted** in the new app.

## 9. Hooks for later (Hermes, lift)

**Hermes (undecided).** Foundation only guarantees the extension points.
- **Events:** an `agent` connector type could subscribe to an allow-listed subset of outbox events.
- **Scoped service tokens:** the `kernel.api_tokens` table and middleware are designed but **disabled** in Foundation. Tokens would be hashed, scoped to explicit permissions, tied to a user (as their delegate) or the system, expiring and revocable.
- There's no privileged agent path. An agent would go through the same router, permission and `get_for` checks as everyone else.

**lift (Household apps).**
- It becomes `modules/lift` (schema `lift`) with permissions like `lift.access`, `lift.tasks.write` and `lift.journal.read_own`.
- Journal entries are owner-only, with no default admin read.
- SQLite data is imported once, routers are adapted to `sdk.router`, and its views move to `web/src/modules/lift`.

## 10. Cross-cutting conventions

**API:**
- `/api/v1/...` for the kernel and `/api/v1/m/<module>/...` for modules.
- Pydantic request/response models.
- Errors look like `{"error": {"code", "message"}}`.
- Cursor pagination with `limit ≤ 100`.
- OpenAPI available in dev only.

**Observability:**
- Structured JSON logs with request ids, and never secrets or tokens.
- Log timestamps in UTC, with `TZ` applied only to the human-readable field.
- `/health` exactly `{"status":"ok"}`. `/health/ready` for trusted peers only.

**Testing:**
- Real Postgres. `Settings` is injected.
- A fake `IdentityAdmin` and a fake OIDC issuer for unit tests. The real bundled IdP in compose integration tests.
- The route-inventory and two-user matrix tests (ADR-002).

**Dependencies:**
- Python and npm lockfiles.
- Renovate/Dependabot, `pip-audit`, `npm audit` and gitleaks in CI.

**Supported platforms:**
- linux/amd64 and linux/arm64 images, so small ARM boards are fine.
- Docker Engine with a current Compose v2 plugin (needs `depends_on` conditions and profiles). The minimum version is pinned and tested in CI.

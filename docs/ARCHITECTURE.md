# Atlas architecture (v1 target)

- **Status:** Proposed design. Nothing here is implemented yet.
- **Reads with:** [VISION](VISION.md) · [AUDIT](AUDIT.md) · ADRs [001 identity](adr/001-identity.md) · [002 authorization](adr/002-authorization.md) · [003 internet exposure](adr/003-internet-exposure.md) · [004 config & secrets](adr/004-config-and-secrets.md) · [005 database & migrations](adr/005-database-and-migrations.md) · [PLAN-v1](PLAN-v1.md)

---

## 1. Shape: a modular monolith

- Atlas is **one deployable backend process** (FastAPI) and **one web app** (React SPA, served as static files by nginx).
- Inside the backend there is a **kernel**, the platform every feature relies on, and a set of **modules**, the features.
- Modules are first-party Python packages in this repo. They are compiled in and switched on or off by the admin. There is no runtime plugin loading.

Why a monolith and not microservices:
- One family and one VM.
- One maintainer.
- One Postgres.
- Transactions that span the kernel and a module (e.g. "create a child account and grant module access") stay atomic.
- Module boundaries are enforced in code (import contracts, the SDK and per-module schemas), not over the network. A module can be extracted later if that's ever needed.

```
                       ┌──────────────────────────── Docker Compose (one VM) ─────────────────────────────┐
cloudflared ──▶ 127.0.0.1:8081 ─▶ nginx ─┬─▶ atlas-web  (static SPA build, served by nginx itself)      │
   (host)                                 ├─▶ atlas-api  (FastAPI: kernel + modules)  ──▶ postgres        │
                                          └─▶ idp        (OIDC provider, ADR-001)      ──▶ own volume     │
                                                         atlas-migrate (one-shot, ADR-005) ──▶ postgres    │
                       └───────────────────────────────────────────────────────────────────────────────────┘
          Immich (separate stack) ◀── OIDC ── idp          Atlas ── connector (HTTP API) ──▶ Immich
```

## 2. Code layout and module boundaries

```
atlas/
  kernel/            platform internals. Only the kernel imports these.
    config.py        Settings (ADR-004). The only place that reads the environment
    db/              engine, unit-of-work, Alembic env, kernel migrations
    identity/        OIDC relying party, sessions, step-up, IdentityAdmin adapter (Kanidm, fake)
    authz/           Principal, permission registry, policy, scoped repositories (ADR-002)
    registry/        module discovery, manifest validation, enable/disable, nav
    events/          event types, outbox, dispatcher
    connectors/      connector types, config store, health checks
    audit/           audit log writer
    admin/           admin-console API (users, roles, modules, connectors, sessions, audit)
    web/             create_app(), middleware (request id, security headers, CSRF, rate limit), /health
  sdk/               the ONLY import surface for modules (stable, versioned)
    __init__.py      router(), Principal, require(), get_for()/list_for() helpers, emit(), on(), ModuleManifest, Permission, NavItem, settings()
modules/
  hello/             tiny reference module, used by the tests (v1)
  immich/            connector-only module: nav link, health, SSO hand-off (v1)
  files/ drive/ photos/   (v2, ported from today's core/apps + core/domain + core/storage)
  lift/              (v3+)
web/                 React SPA (rewritten from frontend/ shell): shell, admin console, per-module UI folders
deploy/              compose, nginx config, app.env.example (names only), runbooks
legacy/  (= today's core/, frontend/, tests/)  untouched until the v2 port; not mounted in v1 production
```

**Boundary rules, enforced in CI with `import-linter` contracts:**
1. `modules.*` may import `atlas.sdk` and third-party libraries only, never `atlas.kernel.*` or another module.
2. `atlas.sdk` may import `atlas.kernel.*`. It is the facade.
3. `atlas.kernel.*` never imports `modules.*`. The registry discovers modules by entry point or config list, never by import from kernel code.
4. Cross-module interaction goes through **events** (§5) or an interface a module *exports* in its manifest (`provides=`), which another module *requires* (`requires=`). The registry wires these up and checks for cycles.

## 3. The module manifest

Each module exposes one `MANIFEST: ModuleManifest` in `modules/<id>/__init__.py`. The registry validates it at startup and **refuses to boot** if it's invalid, if ids are duplicated, or if there's an undeclared route.

```python
MANIFEST = ModuleManifest(
    id="drive",                       # [a-z][a-z0-9_]{1,30}; also the DB schema name and URL prefix
    name="Drive",
    version="2.0.0",
    sdk=">=1,<2",                     # SDK compatibility range
    requires=["files"],               # other modules that must be installed + enabled
    provides=[],                      # exported service interfaces (by name)

    permissions=[                     # ADR-002: <module>.<resource>.<action>
        Permission("drive.access",        "Open Drive",          default_roles=["owner","admin","adult","child"]),
        Permission("drive.files.read",    "View files",          default_roles=["owner","admin","adult","child","guest"]),
        Permission("drive.files.write",   "Upload/rename/move",  default_roles=["owner","admin","adult","child"]),
        Permission("drive.share.public",  "Create public links", default_roles=["owner","admin"], ceiling_excludes=["child","guest"]),
        Permission("drive.admin.purge",   "Purge any trash",     default_roles=["owner"], step_up=True),
    ],

    routes=lambda r: drive_routes(r), # receives atlas.sdk.router(module); every endpoint declares permission= or public=True
    nav=[NavItem(label="Drive", path="/drive", icon="folder", permission="drive.access", order=10)],
    migrations="modules/drive/migrations",   # Alembic version location; branch label = id; schema = id
    settings=DriveSettings,           # pydantic model, validated from ATLAS_MODULE_DRIVE_* env (non-secret) + secret_refs
    events=Subscriptions({"kernel.user.disabled": on_user_disabled}),
    connectors=[],                    # connector types this module provides (see §4)
    jobs=[],                          # periodic jobs (run by the kernel scheduler, single-instance via advisory lock)
)
```

**What the registry does with a manifest:**

| Field | Kernel behaviour |
|---|---|
| `id` | URL prefix `/api/v1/m/<id>/…`, DB schema `<id>`, settings prefix `ATLAS_MODULE_<ID>_`, nav namespace |
| `permissions` | Upserted into the permission registry. Default role grants are seeded **only for new permissions**, so admin edits are preserved. Ceilings are enforced always |
| `routes` | Mounted under the prefix behind a guard: module enabled **and** `<id>.access` held, otherwise **404**. Each route's own `permission` is checked before the handler runs |
| `nav` | Served by `GET /api/v1/nav`, filtered by enablement and the caller's permissions. The SPA renders only those entries |
| `migrations` | Added to Alembic `version_locations`. Run by `atlas-migrate` whether or not the module is enabled, so schemas always match the code |
| `settings` | Validated at startup. Invalid settings mark the module `misconfigured` (it can't be enabled) rather than crashing the kernel; kernel settings do crash it (ADR-004) |
| `events` | Handlers registered with the dispatcher (§5) |
| `requires` | Enabling a module also requires its dependencies to be enabled. Disabling one with enabled dependants is refused |

**Module lifecycle states** (stored in `kernel.modules`): `installed` (code present) → `enabled` / `disabled` by an admin, with step-up, and audited. `misconfigured` is computed and blocks enabling.

**Frontend side:**
- Module UI lives in `web/src/modules/<id>/` and is code-split per module (`React.lazy`).
- The SPA's route table is built from `GET /api/v1/nav`, so a disabled module's chunk is never requested. Its code is still in the build, which is acceptable because it's first-party.
- **No remotely loaded JavaScript.**

## 4. Connectors

A **connector** is a typed, admin-managed link from Atlas to an external service the family runs (Immich first; later possibly the Nextcloud migration source or Hermes). The connector concept is **about configuration and trust only**. Connectors never provision or manage infrastructure.

| Part | Description |
|---|---|
| **Connector type** | Code, declared by a module (`connectors=[ImmichConnector]`). It defines a config schema (non-secret fields such as `base_url`), `secret_refs` (env var names, ADR-004), capabilities (`health`, `sso_link`, `user_sync`, `api`), and a client factory |
| **Connector instance** | A row in `kernel.connectors`: `id, type, name, config (JSONB, validated), enabled, status, last_checked_at`. It's created and edited in the admin console (`connectors.manage`, step-up) |
| **Secrets** | Never in the DB in v1. Instances reference env var names, and the console shows each as "set" or "missing" |
| **Health** | The kernel runs `health()` on a schedule (with timeouts and backoff) and stores the status. Shown in the console and in `/health/ready` details (on loopback only) |
| **Access** | Using a connector from a module requires the permission the module declares (e.g. `immich.access`). Module code gets a client from `sdk.connector("immich")`, which already holds the secret, so modules never see raw secrets |
| **Outbound safety** | The client allow-lists the configured `base_url` host. No user-supplied URLs are fetched, which prevents server-side request forgery (SSRF) |

**Immich connector (v1):**
- **Health:** server ping and version.
- **SSO hand-off:** nav item "Photos (Immich)" opens the Immich URL. Immich logs the user in through the same IdP (ADR-001), so there's no second password.
- **Access:** the console's per-user "Immich access" toggle maps to the IdP group that Immich's OIDC client allows. There's also an optional Immich admin role claim.
- **Later (v2+):** per-user API usage (e.g. a "recent photos" widget) with per-user Immich API keys, under a new ADR.

## 5. Event bus

The event bus is in-process with a transactional outbox, which keeps it durable without extra infrastructure.

**Event envelope:**

```json
{ "id": "uuidv7", "type": "kernel.user.created", "version": 1, "occurred_at": "2026-…Z",
  "actor": {"user_id": "…", "kind": "user|system"}, "subject": {"type": "user", "id": "…"},
  "data": { … }, "request_id": "…" }
```

**Publishing:**
- `sdk.emit(event)` inserts into `kernel.outbox` **in the caller's transaction**.
- If the business change rolls back, the event never happened.

**Dispatch:**
- A kernel background task reads committed outbox rows (`FOR UPDATE SKIP LOCKED`) and calls subscribed handlers.
- Delivery is at-least-once, with retries and exponential backoff. After N failures a row goes to `dead`, and the console shows it.
- **Handlers must be idempotent.** The event `id` is the dedupe key.

**Naming and schemas:**
- Types are `<module>.<entity>.<verb>` in past tense.
- Each type has a pydantic schema and an integer `version`. Changing a schema means a new version, and subscribers declare the versions they accept.

**v1 kernel events:**
- `kernel.user.created|updated|disabled|enabled|deleted`
- `kernel.user.role_changed`
- `kernel.session.revoked`
- `kernel.module.enabled|disabled`
- `kernel.connector.created|updated|status_changed`
- `kernel.auth.login_succeeded|login_failed|step_up`

**Audit vs. events:**
- The audit log is written **synchronously** by `atlas.kernel.audit` in the same transaction, and never derived from events.
- Events are for reactions (e.g. a module cleaning up when a user is disabled), not for record-keeping.

## 6. Configuration

| Layer | Holds | Edited by |
|---|---|---|
| Code defaults | Safe, non-secret defaults only. **No secret has a default** | Developers |
| `/srv/appdata/secrets/app.env` (via compose `env_file`) | All secrets, plus deployment-specific values (`DATABASE_URL`, `ATLAS_PUBLIC_URL`, OIDC issuer/client, connector secret values) | Operator on the host, or Terraform. Never the console |
| `site.env` (untracked, non-secret) | Hostnames, nginx server names | Operator |
| `docker-compose.yml` (in repo) | Non-secret constants only: `TZ=Asia/Bangkok`, `ATLAS_ENV=production`, image tags | Developers |
| DB runtime settings (`kernel.modules`, `kernel.connectors`, `kernel.role_permissions`, `kernel.settings`) | Admin-editable, **non-secret** runtime choices | Admin console (audited, step-up) |

- `Settings` is built once, validated and **fails closed** (ADR-004).
- Module settings are sub-models with the prefix `ATLAS_MODULE_<ID>_`.
- **Precedence:** env beats code defaults. DB settings cover only things env doesn't define, so there's no ambiguity about which wins.

## 7. Data and storage

**PostgreSQL** (ADR-005):
- One database. Schema `kernel` plus one schema per module.
- Two DB roles: `atlas_migrator` for DDL and `atlas_app` for DML only.
- `timestamptz` in UTC everywhere. FKs and indexes are mandatory (CI schema-lint).
- UUIDv7 for anything exposed in URLs.
- The data volume is external and named, so it survives `down -v`.

**Kernel tables (v1):**

| Table | Purpose |
|---|---|
| `users` | `id, idp_sub (unique), username, display_name, role, status (active/disabled), expires_at, created_at, updated_at` |
| `role_permissions` | `role, permission` |
| `user_permission_overrides` | `user_id, permission, effect (allow/deny)` |
| `permissions` | Registry: `name, module, description, default_roles, step_up, ceiling_excludes` |
| `sessions` | ADR-003 |
| `audit_log` | `id, at, actor_user_id, action, target_type, target_id, result, ip, request_id, details JSONB`. Append-only: `atlas_app` has `INSERT/SELECT` only on it |
| `modules` | `id, version, state, enabled_at, enabled_by` |
| `connectors` | §4 |
| `outbox` | §5 |
| `rate_limits` | ADR-003 |
| `settings` | Admin-editable, non-secret runtime settings (§6) |

**Blob storage** (v2, when files arrive) keeps the audit's good parts and fixes the bad ones:
- The `StorageBackend` interface is provided by the kernel through the SDK.
- Keys are **backend-relative** (`objects/<uuid>`), never filesystem paths (AUDIT §2.3 point 4).
- Streaming read/write, with size limits enforced while streaming.
- Server-side MIME sniffing.
- `serve()` returns a response or a presigned URL, so routers don't branch on env.
- Local backend on a named volume. S3/MinIO is optional and tested against a container.
- v1 has **no blob storage**: the foundation stores no user files.

**Backups:** nightly `pg_dump` (plus the IdP's own backup), retention 7 daily + 4 weekly, and a restore drill per release (ADR-005).

## 8. Mapping today's code to future modules (v2)

| Today (main) | Becomes | Changes during the port |
|---|---|---|
| `core/storage/*` (`StorageBackend`, Local, MinIO) | **Kernel service** `atlas.kernel.storage`, exposed via `sdk.storage()` | Relative keys; streaming; size caps; MIME sniffing; recovery inside the backend; MinIO streaming plus tests |
| `core/db/models.py::Object` + `core/domain/objects.py` | **`files` module** (schema `files`): owns `objects` (renamed `files.items`), versions later, `shares` integration | FKs, enum `kind` (file/folder), `timestamptz`, indexes; all queries via `get_for/list_for` (ADR-002); recursive CTE for trees; restore and purge recurse; permanent delete only from trash; RLS backstop |
| `core/apps/objects/router.py` (upload/download/trash/…) | Split: generic item operations in **`files`**, user-facing endpoints in **`drive`** | JSON/form bodies, response models (no `storage` path), bounded pagination, server search |
| `core/apps/drive/router.py` (`/drive/root`, `/folders`) | **`drive` module** (`requires=["files"]`) | Parent checks via `get_for(principal, "write", parent_id)`, which removes AUDIT O1–O3 by construction |
| `core/apps/photos/router.py` + `PhotosView` | **`photos` module** (`requires=["files"]`), a light gallery over `files` images | Server-side MIME and thumbnails in a background job. **Decision needed in v2:** keep a light Photos, or make "Photos" just the Immich connector link, to avoid duplicating Immich |
| `core/apps/auth/*` (HS256 JWT, local passwords) | **Deleted** | Replaced by kernel identity (ADR-001) |
| Startup recovery (`recover_incomplete_uploads`) | `files` module **job** | Age-scoped, single-instance (advisory lock), uses the storage interface |
| `origin/dev` share links | `files`/`drive` **sharing** on kernel `shares` | Hashed tokens, `timestamptz` expiry, `deleted_at` checked, child/guest ceilings |
| `frontend/src/*` views | `web/src/modules/drive`, `…/photos` | API client with 401 → re-login, TanStack Query, real thumbnails, router URLs per folder |
| Legacy SQLite data | One-off import script into `files` | Map old integer ids, owners → `idp_sub`-linked users |

In v1, all of today's code lives untouched (except the Phase 0 security patches) under `legacy/`. It is **not mounted** in the v1 production app.

## 9. Hooks for later (Hermes, lift)

**Hermes (undecided).** v1 only guarantees the extension points; it implements no agent features.
- **Events:** an `agent` connector type could subscribe to the outbox (read-only, filtered by an allow-list of event types).
- **Scoped service tokens:** the `kernel.api_tokens` table and middleware are designed but **not enabled** in v1. Tokens would be hashed, scoped to explicit permissions, tied to a user or the system, expiring and revocable. If Hermes is adopted, it gets the narrowest token and acts *as a user's delegate*, never as admin.
- No inbound "agent can call any endpoint" path. An agent would go through the same router, permission and `get_for` checks as everyone else.

**lift (v3+).**
- It becomes `modules/lift` (schema `lift`) with permissions like `lift.access`, `lift.tasks.write` and `lift.journal.read_own`.
- Journal entries are owner-only by default; no admin read permission is granted by default.
- Its SQLite data is imported once with a script.
- Its FastAPI routers are adapted to `sdk.router`, and its React views move under `web/src/modules/lift`.

## 10. Cross-cutting conventions

**API:**
- `/api/v1/...` for the kernel and `/api/v1/m/<module>/...` for modules.
- JSON bodies with pydantic request/response models, so the OpenAPI spec is complete (available in dev only).
- Errors look like `{"error": {"code": "...", "message": "..."}}`.
- Cursor pagination, with `limit ≤ 100`.

**Observability:**
- Structured JSON logs with a request id, and no secrets or tokens.
- `/health` (contract) and `/health/ready` (loopback only).

**Testing:**
- Real Postgres.
- `Settings` is injected.
- A fake `IdentityAdmin` and fake OIDC issuer for unit tests, and the real IdP in the compose integration test.
- The route-inventory and two-user matrix tests from ADR-002.

**Dependencies:**
- Python and npm lockfiles.
- Dependabot/Renovate, `pip-audit`, `npm audit` and gitleaks in CI.

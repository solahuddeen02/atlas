# Atlas v1: delivery plan

**Goal:** the foundation from [VISION](VISION.md), built as designed in [ARCHITECTURE](ARCHITECTURE.md) and the ADRs, and installable by any self-hoster following [SELF_HOSTING](SELF_HOSTING.md).

**Ordering principle:**
1. Close the audit's exploitable issues on today's code.
2. **Validate the identity decision (spike)** before any platform code depends on it.
3. Make the base safe (CI, secrets, DB, deploy).
4. Build identity → authorization → registry → console → connectors.

**Rules for every phase:**
- One PR or a small series of PRs. CI must be green and is required.
- A phase is done only when its acceptance criteria are met **and** its tests run in CI.
- Later phases never weaken earlier guarantees. Earlier tests stay.
- No hostnames, IPs or anyone's site details in the repo. Use `example.com` placeholders only.

| # | Phase | Main outcome | AUDIT ids closed |
|---|---|---|---|
| 0 | Emergency patches on legacy `main` | Today's code is no longer trivially exploitable; CI green | A1, O1–O3, O5, O7, I1, F1 |
| **S** | **Identity spike** (decision gate for ADR-001) | Kanidm validated behind a reverse proxy: Immich web + mobile, TOTP, step-up, admin API. Results in ADR-001 | — |
| 1 | Repo hygiene & CI baseline | Lockfiles, gitleaks, lint, required CI; legacy isolated | I5, §3.5 hygiene |
| 2 | Config & secrets | Fail-closed `Settings`; `atlas-init` secret generation; env files only | A1 (structural), §3.5 |
| 3 | Database & migrations | Postgres, kernel baseline, `atlas-migrate`, external volumes, backup/restore | §1.5, §2.2 |
| 4 | Deploy skeleton | `atlas-proxy` on a configurable bind/port, `/health`, headers, bundled IdP wired, one-command install | I2, I3 |
| 5 | Identity & sessions | OIDC RP (bundled + BYO), sessions, revocation, CSRF, rate limits, break-glass | A2–A8 |
| 6 | Authorization core | Roles, permissions, route inventory, `get_for`, two-user tests | O1–O6 (by construction) |
| 7 | Module registry | Manifests, enable/disable, nav, per-module migrations | — |
| 8 | Admin console | Users, roles, modules, sessions, audit, step-up | — |
| 9 | Connectors, Immich, events | Connector framework, Immich SSO + health, outbox, Hermes hook | — |
| 10 | Hardening, docs & release | Exposure guides (Cloudflare Tunnel + Access recommended), restore drill, SELF_HOSTING verified on a clean host, v1.0.0 | — |

---

## Phase 0: Emergency patches on legacy `main` (owner-approved)

**Why first:** the repo is public, and the audit shows token forgery and cross-user writes on current code. This is the single approved exception to "Drive/Photos code stays as-is". The patches are **minimal and security-only**.

**Work:**
1. `SECRET_KEY`: remove the default. Refuse to start if it's unset, shorter than 43 chars, or on the placeholder denylist.
2. Remove the literal secrets from `docker-compose.yml`. Read them from an untracked env file instead, and commit an `.env.example` (names only).
3. Ownership checks on referenced ids:
   - `parent_id` on `POST /folders` and on upload;
   - `new_parent_id` on move, which must be the caller's own **folder** and must not create a cycle;
   - all of them return 404 for not-yours.
4. `_get_owned_object` returns 404 (not 403) for other users' objects.
5. `DELETE /objects/{id}/permanent` only accepts trashed objects.
6. Drop the `storage` field from `/objects` and `/photos` responses.
7. Upload size cap (configurable, default 100 MB) enforced while streaming.
8. Fix the tests: `auth_headers` and `two_users` fixtures, JSON bodies, no leaking into `./atlas.db`, the recovery test's `owner_id`.

**Acceptance criteria:**
- The app exits non-zero with no `SECRET_KEY` and with each denylisted value.
- A token forged with the old default is rejected.
- User B gets 404 for every AUDIT O1–O3 attempt.
- A move into self or a descendant gives 400/404.
- Permanent delete of a live object gives 409/404.
- No response contains a filesystem path.
- An upload over the cap gives 413 and leaves no file and no `ready` row.
- `pytest` is green in CI. A run leaves no `atlas.db` in the repo.

**Tests:**
- `test_secret_key_fail_closed`
- `test_forged_default_token_rejected`
- `test_cross_user_parent_id_{folder,upload,move}`
- `test_move_cycle_rejected`
- `test_permanent_requires_trash`
- `test_no_storage_path_in_responses`
- `test_upload_size_limit`
- The existing suite, updated with auth.

## Spike S: identity validation (before Phase 1)

**Why here:** ADR-001 makes Kanidm the bundled IdP *provisionally*. Phases 2–5 (secrets layout, `atlas-init` bootstrap, proxy vhosts and internal TLS, the RP and the `IdentityAdmin` adapter) all depend on it. Validating it before Phase 1 means no platform code gets built on an unverified choice.

**Setup:**
- A throwaway compose stack on any test host: Kanidm + nginx (configured the way `atlas-proxy` will be) + Immich + a minimal authlib test client.
- Public TLS in front through any exposure option. The Cloudflare Tunnel is recommended because it matches the documented default.
- Two test hostnames on a domain the tester controls. They are **not recorded in the repo**.

**Checklist:** exactly the table in [ADR-001 → Spike results](adr/001-identity.md#spike-results). Must-pass items:
1. Kanidm behind the reverse proxy: public origin, internal TLS re-encryption, real client IP, lockout.
2. Per-group credential policy: admin requires passkey/MFA; child allows password.
3. No-mail enrolment through a reset token: password + **TOTP**, and passkey.
4. **TOTP** login and lockout.
5. RP login with PKCE + ES256, ID-token validation, logout.
6. **Step-up:** `prompt=login`/`max_age=0` forces re-auth, and `auth_time` is fresh.
7. **Immich web** SSO, group-restricted.
8. **Immich mobile** SSO from outside the LAN through the proxy.
9. **Admin API:** create/disable person, groups, reset token.
10. *(should)* List and revoke a person's sessions.
11. Kanidm backup and restore.
12. Idempotent bootstrap script: no overwrite of the CA/cert.

**Acceptance criteria:**
- ADR-001's "Spike results" table is fully filled in: versions, pass/fail and evidence (commands and redacted screenshots, with no hostnames or secrets).
- The outcome is recorded: (a) confirmed, (b) confirmed with workarounds, or (c) superseded (Authentik bundled, via a new ADR).
- If (c), Phases 2–5 are re-scoped before Phase 1 starts.

**Artifacts:**
- `deploy/spike/` holds the stack definition, bootstrap commands and test client, with placeholders only.
- The ID-token validation cases become fixtures for Phase 5.

## Phase 1: Repo hygiene & CI baseline

> **Sequencing note (owner-approved):** Phase 1 started before Spike S. It holds no identity code, so it doesn't build anything on the unverified IdP choice. Phases 2–4 can also do their IdP-agnostic parts before the spike. Their Kanidm-specific parts (the `idp/` secrets and internal CA, the IdP admin token, the bundled-IdP compose service and vhost, the IdP backup) and all of Phase 5 wait for the spike's outcome.

**Work:**
- `git mv core frontend tests alembic → legacy/…` (paths only). The legacy suite keeps running.
- Create `atlas/`, `modules/`, `web/` and `deploy/` per ARCHITECTURE §2.
- Python with `uv` and a lockfile. Node `npm ci`.
- `ruff` (lint + format) and `mypy --strict` on `atlas/`. Banned-api rule: `os.getenv` outside `atlas/kernel/config.py`.
- `import-linter` contracts.
- `.gitignore` / `.dockerignore` per ADR-004 §5.
- CI jobs:
  - backend tests;
  - legacy tests;
  - web lint + build;
  - gitleaks (on push, plus a weekly full-history scan);
  - `pip-audit`, `npm audit --omit=dev`;
  - compose-secret linter;
  - Renovate/Dependabot.
- Branch protection: CI required.

**Acceptance criteria:**
- Every job is green and required.
- gitleaks is clean on the full history.
- A PR adding a literal secret to compose fails.
- A PR adding `os.getenv` in `atlas/` fails.

**Tests:** linter self-checks, documented in CONTRIBUTING.

## Phase 2: Config & secrets (ADR-004)

**Work:**
- `Settings` with `SecretStr`. Loading from `ATLAS_ENV_FILES`, with real env vars overriding.
- Fail-closed validators, including rejection of `example.com` URLs in production.
- `create_app(settings)` and a test-settings builder.
- `atlas-admin init` (the `atlas-init` service). It generates missing secrets with an RNG into `${ATLAS_SECRETS_DIR}`, using the per-consumer subdirectories, `0700`/`0600` modes, never overwriting, and idempotent.
- `.env.example` with generic defaults (bind `127.0.0.1`, port `8080`, `TZ=UTC`, `./secrets`, `./backups`, `example.com` placeholders).

**Acceptance criteria:**
- Every ADR-004 check fails closed with a message that names the variable but not its value.
- `ATLAS_ENV` unset means production rules.
- `atlas-init` on an empty dir creates every file with the correct modes. A second run changes nothing. Pre-existing operator files are untouched.
- `docker compose config` renders from `.env.example`.

**Tests:**
- `tests/kernel/test_settings.py`: missing, short, low-variety and denylisted key; value not in the error; production default; `example.com` rejected; env-over-file precedence; connector `misconfigured`.
- `tests/cli/test_init.py`: generation, modes, idempotence, no-overwrite.
- Container test: a placeholder key means exit ≠ 0.

**Deferred until Spike S** (each one is marked `TODO(spike-s)` in the code):
- `atlas-init` generating the `idp/` directory: the internal CA, its key, and the server certificate and key.
- `ATLAS_IDP_ADMIN_TOKEN`, and `ATLAS_OIDC_CLIENT_SECRET` for the bundled IdP. Both are written after the IdP bootstrap, which may issue the client secret itself.
- `ATLAS_IDP_PUBLIC_URL`, the default `ATLAS_OIDC_ISSUER` derived from it, and `ATLAS_IDENTITY_ADMIN=kanidm`.
- The bundled-IdP lines in `deploy/.env.example` (`ATLAS_IDP_PUBLIC_URL`, `COMPOSE_PROFILES=bundled-idp`, `ATLAS_IDP_VOLUME`).

Until then, `ATLAS_OIDC_ISSUER`, `ATLAS_OIDC_CLIENT_ID` and `ATLAS_OIDC_CLIENT_SECRET` are set by hand (the BYO flow). `ATLAS_OIDC_INTERNAL_URL` and `ATLAS_OIDC_CA_FILE` move to Phase 5, which uses them.

**Moved to Phase 4**, because there's no v1 image or compose file before it:
- The container test. Phase 2 runs the same check at process level: `python -m atlas.kernel.web` exits 2 before binding a socket.
- `docker compose config` rendering from `.env.example`. Phase 2 lints `.env.example` instead.

## Phase 3: Database & migrations (ADR-005)

**Work:**
- Postgres 17 pinned by digest, `POSTGRES_PASSWORD_FILE`, external volume `${ATLAS_PGDATA_VOLUME}`.
- `atlas-init` creates the `atlas_migrator` and `atlas_app` roles.
- `atlas/kernel/db` (engine from `Settings`, unit-of-work, UTC session timezone).
- Alembic env from `Settings`. Per-module branches.
- `0001_kernel_baseline`: `users`, `account_links`, `permissions`, `role_permissions`, `user_permission_overrides`, `sessions`, `audit_log` (append-only grants), `modules`, `connectors`, `outbox`, `rate_limits`, `settings`.
- `atlas-migrate`: advisory lock, pre-migration dump into `${ATLAS_BACKUP_DIR}`, `upgrade heads`, seed.
- `atlas-backup` / `atlas-admin restore` with retention settings.
- Warning comment in compose: never `down -v`.

**Acceptance criteria:**
- Empty DB → migrate → API starts.
- An injected failing migration → API doesn't start.
- `atlas_app` can't run DDL or alter `audit_log`.
- `down -v && up -d` keeps all data (CI, throwaway volumes).
- A missing external volume makes `up` fail loudly with a helpful message.
- Backup → restore into a scratch stack passes the smoke tests.

**Tests:**
- `test_migrations_upgrade_empty`
- `test_alembic_check`
- `test_schema_lint`
- `test_app_role_no_ddl`
- `test_audit_log_append_only`
- Compose: `test_down_v_preserves_data`, `test_failed_migration_blocks_api`, `test_backup_restore_roundtrip`.

**Progress:** 3a merged (#19). 3b adds the compose stack and the `compose` CI job. Secret ownership in Compose is set by `atlas-init --chown` (ADR-004 §3).

**Delivered in two PRs** (owner-approved):
- **3a, DB core:** `atlas.kernel.db`, the baseline, the roles and grants, and `db-init`/`migrate`/`backup`/`restore`. Tested against a real Postgres in the `backend` job.
- **3b, compose:** a minimal image, `deploy/docker-compose.yml`, and the compose tests above in a new `compose` CI job.

Roles are created by a separate `atlas-db-init` step (see ADR-005 §3).

**Deferred until Spike S** (marked `TODO(spike-s)` in the code):
- the bundled IdP's backup and restore in `atlas-admin backup`/`restore`;
- its external volume `${ATLAS_IDP_VOLUME}` in compose.

**Handed to later phases:**
- Seeding built-in role grants and manifest permissions: Phase 6 (`TODO(phase-6)`).
- Per-module migration branches, module schemas in backups, and registry sync: Phase 7 (`TODO(phase-7)`).

## Phase 4: Deploy skeleton (one-command install)

> **Progress (IdP-agnostic part):**
> - **Delivered:** `atlas-proxy`, `/health/ready`, multi-stage multi-arch images and `install.sh`, tested in the `compose` and `image` CI jobs.
> - **Owner decisions** (recorded in ADR-003 §1):
>   - `/health` answers on any `Host`;
>   - the Docker gateway is trusted only for a loopback bind; any other bind requires `ATLAS_TRUSTED_PROXY_CIDRS`.
> - **Deferred until Spike S** (`TODO(spike-s)`): the bundled Kanidm service (`bundled-idp` profile), its volume, its virtual host and internal-TLS re-encryption, and creating the IdP volume in `install.sh`.
> - **Later phases:** the app reading `X-Real-IP` for app-level rate limits (Phase 5); the real SPA (Phase 7); publishing images (Phase 10).

**Work:**
- `atlas-proxy` (nginx) publishes only `${ATLAS_BIND_ADDRESS:-127.0.0.1}:${ATLAS_HTTP_PORT:-8080}`.
- Vhosts are generated from `ATLAS_PUBLIC_URL` and `ATLAS_IDP_PUBLIC_URL`. Unknown `Host` → 444.
- Real IP from `ATLAS_REAL_IP_HEADER`, trusted only from `ATLAS_TRUSTED_PROXY_CIDRS`.
- Security headers. `limit_req` zones. `client_max_body_size` from config.
- SPA static. `/api` and `/health` proxied to `atlas-api`, which has no published port.
- Bundled Kanidm under the `bundled-idp` profile (on by default) with an external volume. `atlas-init` generates the internal CA/cert, and the proxy re-encrypts and verifies it.
- `/health` → exactly `{"status":"ok"}`. `/health/ready` for trusted peers only.
- `TZ` (default `UTC`) on every service.
- `/docs` off in production.
- Multi-arch (amd64/arm64), multi-stage, non-root images.

**Acceptance criteria:**
- On a clean Linux host, the SELF_HOSTING quick start works: `cp .env.example .env`, edit the two URLs, create volumes, `docker compose up -d`. Then `curl -s http://127.0.0.1:8080/health` gives `{"status":"ok"}`.
- Only `atlas-proxy` publishes a port. Changing the bind/port settings moves it.
- Headers are present. `/docs` gives 404. The images contain no `.env`, `.git` or `tests` and run as non-root.
- BYO mode (profile off) starts without the `idp` service.

**Tests:**
- Compose smoke tests in CI (default + BYO): `health_ok`, `single_published_port`, `bind_and_port_configurable`, `unknown_host_dropped`, `real_ip_untrusted_ignored`, `security_headers_present`, `docs_disabled_in_production`.
- `nginx -t`. Image content test.

## Phase 5: Identity & sessions (ADR-001, ADR-003)

**Work:**
- OIDC RP using the spike's validated parameters:
  - login, callback, logout, step-up;
  - JWKS cache;
  - strict `iss`/`aud`/`exp`/`nonce`/`auth_time`;
  - internal back-channel URL + CA for the bundled IdP.
- Unknown `(issuer, sub)` → reject unless completing an **account link** (BYO).
- Sessions with a `__Host-` cookie, CSRF synchronizer token, rotation, per-role lifetimes (configurable).
- `IdentityAdmin` implementations: `kanidm`, `manual` (BYO) and `fake`.
- `atlas-init` IdP bootstrap (bundled): Atlas OAuth2 client, console service account, role groups and credential policies.
- `atlas-admin bootstrap-owner`: creates the first owner and prints a one-time enrolment link. In BYO mode it prints a link code.
- `atlas-admin break-glass`.
- App-layer rate limits.
- Legacy JWT, passwords and `/auth/register` are not carried forward.

**Acceptance criteria:**
- Login works against the bundled IdP (compose integration) and the fake issuer (unit tests).
- BYO linking binds exactly one `(issuer, sub)` per code, and an expired or reused code is rejected.
- Tampered, expired, wrong-`aud` and replayed tokens are rejected.
- Revoke → next request 401. Disable user → all sessions gone.
- Cookie flags correct. No token in `localStorage` or any response body.
- CSRF enforced. N+1-th auth callback → 429.
- Break-glass works once, expires, and is audited.

**Tests:** `tests/kernel/identity/` covers validation fixtures from the spike, session lifecycle, rotation, CSRF, rate limits, linking, bootstrap-owner, break-glass, and idle/absolute expiry (frozen time). Plus an integration test against the bundled IdP.

## Phase 6: Authorization core (ADR-002)

> **Carried over from Phase 0 (owner decision):** on legacy `main`, `GET /folders/{id}` for another user's folder returns `200 []`. It leaks nothing, so Phase 0 left it unchanged. In the new authorization layer, listing a folder must load it through `get_for(principal, "read", id)` and return **404** for a folder that isn't yours, the same as "not found". The two-user matrix test must cover it.

**Work:**
- `Principal`. The permission registry and seeding. User overrides.
- Ceilings and invariants: last owner, admin limits, child/guest ceilings, guest expiry.
- `sdk.router(module)` requiring `permission=` or `public=True`, plus a startup route-inventory check.
- Scoped repositories with `get_for`/`list_for`. Not-yours = 404.
- Step-up enforcement (`ATLAS_STEP_UP_MAX_AGE`).
- Same-transaction audit writer.
- `import-linter` contracts active.

**Acceptance criteria:**
- An undeclared route stops boot.
- The generated two-user matrix passes for every id-taking route, including **named AUDIT O1–O3 regressions** on the `hello` module's parent/child resource.
- The invariants hold under direct API attempts.
- A stale `auth_time` gives `401 step_up_required`.

**Tests:**
- `test_route_inventory`
- `test_two_user_matrix`
- `test_role_matrix`
- `test_invariants_*`
- `test_step_up`
- `test_audit_same_txn`
- import-linter.

## Phase 7: Module registry

**Work:**
- `ModuleManifest` and validation. Discovery from a configured list.
- Guarded mounting under `/api/v1/m/<id>`.
- `GET /api/v1/nav`.
- `requires` and cycles. `misconfigured` state.
- Per-module Alembic branches. Event registration.
- `modules/hello` reference module.
- SPA shell: router, typed API client (401 → re-login, `step_up_required` → re-auth), nav from the API, lazy chunks.

**Acceptance criteria:**
- Enabling or disabling `hello` toggles its routes (200/404) and its nav entry without a restart.
- An invalid manifest stops boot clearly.
- Disabling a required module is refused.
- Migrations run while the module is disabled.
- No chunk is requested for a disabled module.

**Tests:** `tests/kernel/registry/`. Web: Vitest + Testing Library for nav and the API client.

## Phase 8: Admin console

**Work:** console pages and their APIs (`admin.*`, step-up on mutations):
- **Users:**
  - create (atomic across Atlas + `IdentityAdmin`);
  - role;
  - disable/enable;
  - guest expiry;
  - **reset credentials** (bundled: IdP reset token shown once as a link and QR code; BYO: guidance plus a new link code if needed);
  - "Immich access" toggle.
- **Roles & permissions:** matrix (ceilings greyed out) and per-user overrides.
- **Modules:** state, enable/disable, `misconfigured` reasons.
- **Sessions:** per user, revoke one or all. Users also get "my sessions".
- **Audit log:** filterable, read-only.

**Acceptance criteria:**
- Every VISION "done" admin action works from the console and writes exactly one audit entry.
- An admin can't modify an owner or exceed their own permissions.
- Secrets are never displayed. Reset tokens are shown once and never stored.
- A stale step-up prompts a re-auth.

**Tests:**
- API tests per action (permissions, step-up, audit).
- `IdentityAdmin` failure injection leaves no partial state.
- Playwright end to end on the compose stack: create child → reset → child logs in → admin disables → child's session dies.

## Phase 9: Connectors, Immich, events, Hermes hook

**Work:**
- Connector framework: types from manifests, instances in the DB, `secret_ref` status from `connectors.env`, scheduled health checks, host allow-listed client.
- `modules/immich`: health, nav link, `immich.access` mapped to the IdP group (bundled) or nav-only (BYO).
- Outbox and dispatcher, with a dead-letter view. Kernel events.
- Hermes hook: `api_tokens` design and a disabled middleware stub. **No agent features.**

**Acceptance criteria:**
- Adding the Immich connector shows healthy, or a clear reason.
- A member with `immich.access` sees the link and lands in Immich signed in via SSO. Without it, there's no link, and with the bundled IdP the IdP blocks Immich login.
- Events are delivered at least once, with retries and dead-lettering. A rolled-back event is never delivered.

**Tests:**
- Connector validation and missing secret ref.
- Health against a fake server.
- Allow-list.
- Outbox semantics.
- Immich web SSO smoke test in the compose test stack (mobile goes on the release checklist).

## Phase 10: Hardening, docs & release

**Work:**
- **Exposure guides** in SELF_HOSTING:
  - Cloudflare Tunnel (recommended), with Access on `/admin*` and `/api/v1/admin/*`;
  - your own reverse proxy (Caddy and Traefik examples);
  - VPN-only.
- A threat-model review against ADRs 001–005.
- A dependency audit at zero high/critical.
- Restore drill.
- SELF_HOSTING verified end to end on a clean host by someone other than the author, if possible.
- README rewritten (quick start, links to SELF_HOSTING).
- CONTRIBUTING and SECURITY.md (vulnerability reporting).
- Tag `v1.0.0` with multi-arch images.

**Acceptance criteria:**
- Pen-test checklist passes:
  - forged and stale tokens;
  - CSRF;
  - two-user matrix on production settings;
  - rate limits;
  - headers;
  - `/docs` off;
  - single published port with loopback default;
  - `down -v` safe;
  - with the tunnel option, `/admin` asks for Access while the household app and Immich mobile still work without it.
- Restore drill recorded.
- Every VISION "done" statement demonstrated on a fresh install.

**Tests:** full suite plus `docs/runbooks/release-checklist.md`, executed and recorded on the release PR.

---

## After v1 (pointers, not commitments)

- **v2:**
  - `files` → `drive` ported from `legacy/` (ARCHITECTURE §8), with sharing on kernel `shares` and an RLS backstop;
  - **decide Photos** (leaning: Immich connector link + SSO, no Atlas gallery);
  - legacy data import.
- **v3+:** the `lift` module, the Nextcloud-replacement roadmap, and Hermes under its own ADR.

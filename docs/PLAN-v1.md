# Atlas v1: delivery plan

**Goal:** the foundation from [VISION](VISION.md), built as designed in [ARCHITECTURE](ARCHITECTURE.md) and the ADRs.

**Ordering principle:** close the audit's exploitable issues first, then make the base safe (CI, secrets, DB, deploy), then build identity → authorization → registry → console → connectors.

**Rules for every phase:**
- One PR or a small series of PRs. CI must be green and is required.
- Each phase ships with its tests. A phase is done only when its acceptance criteria are met **and** its tests run in CI.
- No phase may lower an earlier phase's guarantee. Earlier phases' tests stay in the suite.
- Nothing in the repo names hostnames, IPs or homelab details beyond the deploy contract.

| # | Phase | Main outcome | Security fixes closed (AUDIT ids) |
|---|---|---|---|
| 0 | Emergency patches on legacy `main` | Today's code is no longer trivially exploitable; CI green | A1, O1–O3, O5, O7, I1, F1 |
| 1 | Repo hygiene & CI baseline | Lockfiles, gitleaks, lint, required CI; legacy code isolated | I5, §3.5 hygiene |
| 2 | Config & secrets | Fail-closed `Settings`; env-file-only secrets | A1 (structural), §3.5 |
| 3 | Database & migrations | Postgres, kernel baseline, `atlas-migrate`, external volume, backups | §1.5, §2.2 |
| 4 | Deploy skeleton | nginx on `127.0.0.1:8081`, `/health`, security headers, compose per contract | I2, I3 |
| 5 | IdP spike (decision gate) | ADR-001 confirmed (Kanidm) or switched (Authentik) | — |
| 6 | Identity & sessions | OIDC login, server-side sessions, revocation, CSRF, rate limits | A2–A8 |
| 7 | Authorization core | Roles, permissions, route inventory, `get_for`, two-user tests | O1–O6 (by construction) |
| 8 | Module registry | Manifests, enable/disable, nav, per-module migrations | — |
| 9 | Admin console | Users, roles, modules, sessions, audit, step-up | — |
| 10 | Connectors, Immich, events | Connector framework, Immich SSO + health, outbox, Hermes hook | — |
| 11 | Hardening & release | Cloudflare edge rules and Access on admin paths, restore drill, runbooks, v1.0.0 | — |

---

## Phase 0: Emergency patches on legacy `main`

**Why first:** the repo is public, and the audit shows token forgery and cross-user writes on current code. These patches are deliberately **minimal**. This is the one exception to "Drive/Photos code stays as-is": only security patches, no refactors.

**Work:**
1. `SECRET_KEY`: remove the default. Refuse to start if it's unset, shorter than 43 chars, or on the placeholder denylist.
2. Remove the literal `SECRET_KEY` and MinIO credentials from `docker-compose.yml`. Use `env_file: /srv/appdata/secrets/app.env` and add `deploy/app.env.example` (names only).
3. Ownership check on referenced ids:
   - `parent_id` on `POST /folders` and on upload;
   - `new_parent_id` on move, which must also be the caller's own **folder** and must not create a cycle;
   - all of them return 404 for not-yours.
4. `_get_owned_object` returns 404 (not 403) for other users' objects.
5. `DELETE /objects/{id}/permanent` only accepts trashed objects.
6. Drop the `storage` field from `/objects` and `/photos` responses.
7. Upload size cap (config, default 100 MB) enforced while streaming.
8. Fix the tests: add `auth_headers` and `two_users` fixtures, use JSON bodies, and stop leaking into `./atlas.db` (set `DATABASE_URL` in conftest and drop the lifespan `init_db` in tests). Fix the recovery test's missing `owner_id`.

**Acceptance criteria:**
- The app exits non-zero with no `SECRET_KEY` and with each denylisted value.
- A token forged with the old default is rejected.
- User B gets 404 for every attempt in AUDIT O1–O3.
- Moving a folder into itself or a descendant gives 400 or 404.
- Permanent delete of a live object gives 409/404.
- No API response contains a filesystem path.
- An upload over the cap gives 413 and leaves no file and no `ready` row.
- `pytest` is fully green on CI (Python 3.11). Running `pytest` leaves no `atlas.db` in the repo.

**Tests:**
- `test_secret_key_fail_closed`
- `test_forged_default_token_rejected`
- `test_cross_user_parent_id_{folder,upload,move}`
- `test_move_cycle_rejected`
- `test_permanent_requires_trash`
- `test_no_storage_path_in_responses`
- `test_upload_size_limit`
- The existing suite, updated with auth.

## Phase 1: Repo hygiene & CI baseline

**Work:**
- `git mv core frontend tests alembic → legacy/…`. Path moves only, plus updated imports and CI paths. The legacy suite keeps running.
- Create empty `atlas/`, `modules/`, `web/` and `deploy/` packages per ARCHITECTURE §2.
- Python with `uv` and a lockfile (`uv.lock`). Node lockfile already exists (`npm ci`).
- `ruff` (lint + format) and `mypy --strict` on `atlas/`. Banned-api rule: `os.getenv` outside `atlas/kernel/config.py`.
- `import-linter` contracts (ARCHITECTURE §2), initially vacuous.
- `.gitignore` / `.dockerignore` updated per ADR-004 §4.
- CI jobs:
  - backend tests;
  - legacy tests;
  - web lint + build;
  - gitleaks (on push, plus a weekly full-history scan);
  - `pip-audit`, `npm audit --omit=dev`;
  - compose-secret linter;
  - Renovate/Dependabot.
- Branch protection on `main`: CI is required.

**Acceptance criteria:**
- Every CI job is green and required.
- gitleaks finds nothing on the full history.
- A PR that adds `SECRET_KEY=abc` to compose fails CI.
- A PR that adds `os.getenv` in `atlas/` fails CI.

**Tests:**
- A CI self-test fixture: a deliberately bad file in a scratch branch is caught by the secret linter. This is documented, not kept in `main`.

## Phase 2: Config & secrets (ADR-004)

**Work:**
- `atlas/kernel/config.py` `Settings` model with `SecretStr` fields.
- Fail-closed validators: length, variety, denylist; production is the default.
- `create_app(settings)` factory. A test-settings builder.
- `deploy/app.env.example` (names and generation commands only). `make dev-secrets` generates an untracked `.env.dev`.
- Postgres superuser password lives in its own env file. Compose has no `environment:` secrets and no hostnames (hostnames go in the untracked `site.env`).

**Acceptance criteria:**
- Every ADR-004 check fails closed with a message that names the variable but not its value.
- `ATLAS_ENV` unset means production rules apply.
- `docker compose config` renders using only the example files.

**Tests:**
- `tests/kernel/test_settings.py` covers:
  - missing, short, low-variety and denylisted key;
  - value not in the error message;
  - production default;
  - production-only rules;
  - `misconfigured` state for a connector with a missing `secret_ref`.
- A container test: a placeholder key means exit ≠ 0.

## Phase 3: Database & migrations (ADR-005)

**Work:**
- Postgres 17 service pinned by digest, healthcheck, **`atlas_pgdata` external volume**, and an init script creating `atlas_migrator` and `atlas_app`.
- `atlas/kernel/db`: SQLAlchemy 2 engine from `Settings`, unit-of-work, UTC session timezone.
- Alembic env reads from `Settings`; `sqlalchemy.url` removed from the ini. Per-module version locations and branch labels.
- `0001_kernel_baseline` creates: `users`, `permissions`, `role_permissions`, `user_permission_overrides`, `sessions`, `audit_log` (append-only grants), `modules`, `connectors`, `outbox`, `rate_limits`, `settings`.
- `atlas-admin migrate` (advisory lock → `pg_dump` if anything is pending → `upgrade heads` → seed) and the `atlas-migrate` one-shot compose service. The API `depends_on` it completing successfully.
- Nightly backup script and retention, plus a `restore` runbook. Compose comment and README warning: never `docker compose down -v`.
- Test infra: Postgres service in CI and `compose.test.yml` locally.

**Acceptance criteria:**
- `atlas-migrate` on an empty DB → API starts.
- An injected failing migration → API doesn't start and the old containers keep running.
- `atlas_app` can't run DDL.
- `docker compose down -v && docker compose up -d` keeps all data (CI, throwaway volume).
- A missing external volume makes `up` fail loudly.
- Restore from a dump into a scratch DB passes the smoke test.

**Tests:**
- `test_migrations_upgrade_empty`
- `test_alembic_check_models_match`
- `test_schema_lint` (`timestamptz`, FK + index on `*_id`)
- `test_app_role_no_ddl`
- `test_audit_log_append_only`
- Compose integration: `test_down_v_preserves_data`, `test_failed_migration_blocks_api`

## Phase 4: Deploy skeleton (contract)

**Work:**
- nginx container with the port published as **`127.0.0.1:8081:8080`** only. Host-based vhosts from `site.env`, unknown `Host` → 444, real IP from `CF-Connecting-IP` trusted only from loopback.
- Security headers (ADR-003) and `limit_req` zones.
- nginx serves the static SPA build and proxies `/api/` and `/health` to `atlas-api`, which has no published port.
- `GET /health` → exactly `{"status":"ok"}`. `/health/ready` is loopback-only.
- `TZ=Asia/Bangkok` on every service.
- Production disables `/docs`.
- Multi-stage images with non-root users.
- Runbook: first deploy (create volumes, env files, `up -d`), upgrade, rollback (previous image tag; migrations are forward-only, so restore from the pre-migration dump if needed).

**Acceptance criteria:**
- On a clean Debian VM following the runbook, `curl -s 127.0.0.1:8081/health` gives `{"status":"ok"}`.
- `ss -ltn` shows no other new listening ports.
- Headers present.
- `/docs` → 404.
- Containers run as non-root.
- The image doesn't contain `.env`, `.git` or `tests`.

**Tests:**
- Compose smoke test in CI: `health_ok`, `only_loopback_8081_published` (inspect the compose config), `unknown_host_dropped`, `security_headers_present`, `docs_disabled_in_production`.
- `nginx -t`.
- Image content test.

## Phase 5: IdP spike, the decision gate for ADR-001

**Work:** run Kanidm in the compose stack on a non-production VM, behind nginx with internal TLS. Work through this checklist:
1. Create persons in each role group, with credential policy per role (admin → passkey/MFA; child → password allowed).
2. Admin-issued credential reset token → enrolment of a password + TOTP and a passkey. No mail involved.
3. Atlas test client (authlib) with PKCE + ES256: login, ID-token validation, `auth_time` present, `max_age=0`/`prompt=login` forces re-auth, end-session works.
4. **Immich web and mobile SSO** against Kanidm: ES256 + `client_secret_post` web login, then the mobile app via Immich's mobile redirect override. Group-restricted access.
5. Admin API through a service account: create, disable and enable a person; set group membership; issue a reset token; list sessions and (if supported) revoke them.
6. Backup and restore of the IdP volume.
7. Resource usage on the VM.

**Acceptance criteria:**
- A written result appended to ADR-001 (status → Accepted, or Superseded by Authentik), with every checklist item marked pass/fail and its evidence.
- If any of items 3, 4 or 5 fails and has no workaround, repeat the spike with Authentik.

**Tests:** spike scripts kept under `deploy/spike/` (no secrets, no hostnames). The ID-token validation cases become fixtures for Phase 6.

## Phase 6: Identity & sessions (ADR-001, ADR-003)

**Work:**
- `atlas/kernel/identity`:
  - OIDC RP: login, callback, logout, step-up;
  - JWKS cache with rotation;
  - strict validation of `iss`, `aud`, `exp`, `nonce`, `auth_time`;
  - unknown `sub` → reject (no JIT sign-up).
- Sessions table, a `__Host-` cookie and CSRF synchronizer token. Rotation on login, step-up and role change.
- Idle and absolute lifetimes per role.
- `IdentityAdmin` interface with a Kanidm (or Authentik) implementation and a fake.
- Rate limits (app layer) on auth endpoints.
- `atlas-admin bootstrap-owner`: one-time, host-only. It creates the first owner and issues their IdP enrolment token.
- `atlas-admin break-glass`: one-time 10-minute owner login URL, audited.
- The legacy HS256 JWT, local passwords and `/auth/register` are not carried into `atlas/`.

**Acceptance criteria:**
- Login works end to end against the real IdP (compose integration test) and against a fake issuer (unit tests).
- Tampered, expired, wrong-`aud` or replayed (nonce) tokens are rejected.
- Revoke → the next request gives 401.
- Disable user → all of their sessions are gone at once.
- Cookie flags correct. No token appears in `localStorage` or in any response body.
- CSRF is enforced on unsafe methods.
- The N+1-th auth callback per window gives 429.
- Break-glass works once, expires, and is audited.

**Tests:**
- `tests/kernel/identity/` covers validation cases (from the spike fixtures), session lifecycle, rotation, CSRF, rate limit, bootstrap-owner, break-glass, and idle/absolute expiry (with frozen time).
- An integration test with the real IdP container.

## Phase 7: Authorization core (ADR-002)

**Work:**
- `Principal`; the permission registry; role → permission seeding; user overrides; ceilings and invariants (at least one owner, admin limits, child/guest ceilings, guest expiry).
- `sdk.router(module)`, which requires `permission=` or `public=True`. A startup route-inventory check refuses to boot on any undeclared route.
- Scoped repository base with `get_for`/`list_for`. Not-yours = 404.
- Step-up flag enforcement → `401 step_up_required`.
- Audit writer (same transaction).
- `import-linter` contracts become active.

**Acceptance criteria:**
- Booting with an undeclared route fails.
- The two-user matrix test generates cases for every id-taking route and all of them pass, including the **named regressions for AUDIT O1–O3** (re-expressed against the `hello` module's parent/child resource).
- Every invariant holds under direct API attempts.
- A stale `auth_time` on a step-up permission gives 401.

**Tests:**
- `test_route_inventory`
- `test_two_user_matrix` (generated)
- `test_role_matrix`
- `test_invariants_last_owner|admin_limits|ceilings|guest_expiry`
- `test_step_up`
- `test_audit_same_txn`
- import-linter in CI.

## Phase 8: Module registry

**Work:**
- `ModuleManifest` and validation. Discovery from a configured list.
- Mount under `/api/v1/m/<id>` with the enabled + `<id>.access` guard (otherwise 404).
- `GET /api/v1/nav` filtered per principal.
- `requires` and cycle checks. `misconfigured` state.
- Per-module Alembic branches run by `atlas-migrate`.
- Module events registration.
- `modules/hello` reference module: one owned resource with parent/child, permissions, nav, a migration and an event handler.
- SPA shell in `web/`: router, typed API client (401 → re-login, `step_up_required` → re-auth), nav from the API, lazy module chunks.

**Acceptance criteria:**
- Enable or disable `hello` → its routes return 200/404 and its nav entry appears/disappears without a restart.
- An invalid manifest stops boot with a clear error.
- Disabling a module that others require is refused.
- `hello`'s migration runs even while it's disabled.
- The SPA never requests a disabled module's chunk.

**Tests:**
- `tests/kernel/registry/` covers manifest validation, guard, nav filtering, `requires`, `misconfigured`, migrations per branch.
- Web: Vitest + Testing Library for nav rendering and the API client's 401/step-up handling.

## Phase 9: Admin console

**Work:** console pages and their APIs (all `admin.*` permissions, with step-up on mutations):
- **Users:**
  - create (in Atlas + IdP through `IdentityAdmin`, atomically: if the IdP call fails, the Atlas user isn't created);
  - set role;
  - disable/enable;
  - guest expiry;
  - **reset credentials** (issues an IdP reset token and shows it once as a link and QR code for in-person hand-over);
  - "Immich access" toggle (IdP group).
- **Roles & permissions:** role × permission matrix editor (ceilings shown greyed out) and per-user overrides.
- **Modules:** list, state, enable/disable, `misconfigured` reasons.
- **Sessions:** per user, revoke one or all. Users also get "my sessions".
- **Audit log:** filterable, read-only.

**Acceptance criteria:**
- An admin can do every VISION "done" action (create a child, reset a credential, revoke sessions, disable a module) from the console.
- Every one of them produces exactly one audit entry.
- An admin can't modify an owner or exceed their own permissions.
- No secret is ever displayed. Reset tokens are shown once and never stored in Atlas.
- Mutations after 5 minutes without step-up prompt a re-auth.

**Tests:**
- API tests for every console action, covering the permission and step-up paths and audit rows.
- IdP failure injection (the fake raises) leaves no partial state.
- Playwright end to end against the compose stack: create child → reset → login as child → admin disables child → child's session dies.

## Phase 10: Connectors, Immich, events, Hermes hook

**Work:**
- Kernel connector framework: types from manifests, instances in the DB, `secret_ref` status, scheduled health checks with timeouts, host allow-listed client.
- `modules/immich`: connector type (`health`, `sso_link`), nav item, `immich.access` permission mapped to the IdP group.
- Outbox and dispatcher (`SKIP LOCKED`, retries, dead-letter list in the console). Kernel events per ARCHITECTURE §5.
- Hermes hook: the `api_tokens` table design and its disabled middleware stub, plus the event allow-list design. **No agent features.**

**Acceptance criteria:**
- An admin adds the Immich connector. Status shows healthy, or a clear reason (`secret missing`, `unreachable`, `TLS error`).
- A family member with `immich.access` sees the nav item and lands in Immich already signed in through SSO. Without it, there's no nav item and the IdP blocks Immich login.
- Events are delivered at least once; a crashed handler is retried and eventually dead-lettered.
- An event emitted in a rolled-back transaction is never delivered.

**Tests:**
- Connector config validation and the missing secret ref case.
- Health with a fake server (timeouts, non-200).
- Allow-list blocks other hosts.
- Outbox transactional semantics, retry/dead-letter and idempotent handler.
- Integration: Immich SSO smoke test (web) in the compose test stack, where feasible. Otherwise a documented manual check in the release checklist.

## Phase 11: Hardening & release

**Work:**
- Cloudflare edge: rate-limit rules on the IdP and Atlas auth paths, plus managed WAF rules.
- **Cloudflare Access on `/admin*` and `/api/v1/admin/*` only** (ADR-003). The IdP's admin surface is either not routed or sits behind Access.
- A threat-model review against ADRs 001–005.
- A dependency audit at zero high/critical.
- Restore drill.
- Runbooks: first deploy, upgrade, rollback, backup/restore, secret rotation, onboarding a family member, lost device, break-glass.
- README rewritten from the new config table.
- Tag `v1.0.0`.

**Acceptance criteria:**
- Pen-test checklist passes:
  - forged and stale tokens;
  - CSRF;
  - two-user matrix against production config;
  - rate limits;
  - headers;
  - `/docs` off;
  - only `127.0.0.1:8081` bound;
  - `down -v` safe;
  - the admin path asks for Access from outside, while the family app and Immich mobile still work without it.
- Restore drill completed and recorded.
- Every VISION "done" statement demonstrated on the real deployment.

**Tests:** the full suite plus the release checklist (`docs/runbooks/release-checklist.md`), executed and recorded on the release PR.

---

## After v1 (pointers, not commitments)

- **v2:** `files` → `drive` → `photos` modules ported from `legacy/` per ARCHITECTURE §8. Sharing on kernel `shares`, with an RLS backstop. Decide what Photos means given Immich. Import legacy data.
- **v3+:** `lift` module. The Nextcloud replacement roadmap. Hermes, decided under its own ADR using the §9 hooks.

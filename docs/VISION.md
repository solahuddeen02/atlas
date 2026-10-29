# Atlas: Vision

## One sentence

**Atlas is our family's self-hosted home platform: one secure front door that knows who each family member is, decides what they may use, and hosts or connects the apps we run at home.**

## Who it is for

One household. Every person in it has an account with one role:

| Role | Who | In short |
|---|---|---|
| **Owner** | Deen (one or two people) | Can do everything, including managing admins. Recovers the system when something breaks |
| **Admin** | A trusted adult | Manages users, roles, modules and connectors. Cannot remove or demote the owner |
| **Adult** | Other grown-ups | Uses every module the household enables |
| **Child** | Kids | Uses the modules an admin allows. Cannot share anything outside the family |
| **Guest** | Visiting relatives, babysitter | Sees only what is explicitly shared with them. The account can expire |

There is no mail server. When someone forgets a password or loses a device, an admin issues a one-time reset/enrolment link in person, through the admin console.

Atlas is reached from home and from outside through a Cloudflare Tunnel. Nothing listens on a public port.

## Where it is going

```
v1  Foundation      identity · users & family roles · permissions · admin console · module registry
v2  First modules   Files (from today's objects code) · Drive · Photos-lite · Immich deep-link/SSO polish
v3+                 lift (tasks, habits, journal) as a module · Nextcloud replacement features · Hermes (if decided)
```

- **Nextcloud** gets replaced by Atlas modules over time. That is not part of v1.
- **Immich** stays a separate service. Atlas integrates it through a connector and single sign-on (SSO), and never re-implements photo management.
- **lift** (FastAPI/React/SQLite) is later ported into an Atlas module. Its data moves to Atlas's PostgreSQL database.
- **Hermes AI agent**: undecided. v1 only leaves a hook (the event outbox and scoped service tokens, see [ARCHITECTURE](ARCHITECTURE.md#9-hooks-for-later-hermes-lift)).

## v1 scope (the foundation)

1. **Identity.** Family members sign in through an OIDC identity provider (an OIDC IdP, see [ADR-001](adr/001-identity.md)), and Immich uses the same login. 2FA is required for owner and admin. Atlas keeps server-side sessions, so it can revoke them.
2. **Users and family roles.** The owner, admin, adult, child and guest roles above. Accounts can be created, disabled, given an expiry date (guests), and reset by an admin.
3. **Permissions.** Roles grant per-module permissions, and per-object ownership/sharing is designed for v2 ([ADR-002](adr/002-authorization.md)). Every check is enforced in one layer that routes cannot bypass.
4. **Admin console.** Manage users, roles, module enablement, connectors, active sessions and the audit log. Sensitive actions require a fresh re-authentication (step-up auth).
5. **Module registry.** Modules declare their routes, permissions, nav entries and migrations in a manifest. Admins enable or disable modules, and the UI shows each person only the modules they can use.
6. **Connectors.** A connector is a typed, admin-managed link to an external service. Immich is the first one: health status, nav link and SSO.
7. **A safe base.** Fail-closed config, secrets only in an env file, PostgreSQL with migrations run at container start, CI that is green and required, and the deploy contract below.

### Deploy contract (v1 must fit this)

- Docker Compose on a Debian VM.
- nginx bound to **`127.0.0.1:8081` only**. It is the sole entry point, and the tunnel connects to it.
- `GET /health` → `{"status":"ok"}`.
- `SECRET_KEY` (and every other secret) comes from the env file `/srv/appdata/secrets/app.env`.
- PostgreSQL data lives in a **named Docker volume**.
- `TZ=Asia/Bangkok`.
- **Never `docker compose down -v`**: the design protects data even if someone runs it (see [ADR-005](adr/005-database-and-migrations.md)).

## Explicit non-goals for v1

| Not in v1 | Why / where it goes |
|---|---|
| Drive, Photos, file upload/download in the new platform | The existing code stays untouched (apart from the Phase 0 security patches) and becomes the first modules in v2 |
| Provisioning VMs, containers, DNS or tunnels from the admin console | Infra stays in Terraform. The console manages **Atlas** users/roles/modules/connectors only |
| Writing our own password/TOTP/passkey/OIDC-provider stack | We rely on a proven IdP ([ADR-001](adr/001-identity.md)) |
| Email (password reset mail, notifications) | There is no mail server. Admins issue reset links in person |
| Public self-registration | Accounts are created only by admins |
| Multiple households / multi-tenancy | One family, one instance. `origin/dev`'s tenant model is not carried forward |
| Re-implementing photo management | Immich does that, and Atlas connects to it |
| Feature parity with Nextcloud (sync clients, WebDAV, calendar, contacts) | Considered module by module after v2 |
| Hermes AI agent features | Undecided. Only an integration hook exists |
| Native mobile apps | Responsive web/PWA. Immich keeps its own mobile app (using SSO) |
| Third-party / runtime-loaded plugins | Modules are first-party code in this repo, compiled in and enabled by config. No code is uploaded at runtime |
| High availability, clustering | One VM. Backups and a tested restore cover recovery |

## What "done" looks like for v1

Every family member can sign in to Atlas, from home or from outside, with their own account and the right 2FA for their role. The same login opens Immich. An admin can create a child account, reset a forgotten credential, disable a lost device's sessions and turn a module off, and every one of those actions shows in the audit log. Rebuilding the containers or redeploying never loses data. The public repo contains no secrets and no homelab details.

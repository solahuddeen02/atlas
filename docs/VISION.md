# Atlas: Vision

## One sentence

**Atlas is a self-hosted home platform for a household: one secure front door that knows who each family member is, decides what they may use, and hosts or connects the apps the household runs.**

## Who it is for

Atlas is for **a single household** that runs its own server: a mini-PC, a NAS, a VM or a small cloud box. It has one owner/maintainer, and family members who aren't technical.

Every person in the household has an account with one role:

| Role | Typically | In short |
|---|---|---|
| **Owner** | The person who installed Atlas (1–2 people) | Can do everything, including managing admins. Recovers the system when something breaks |
| **Admin** | A trusted adult | Manages users, roles, modules and connectors. Cannot remove or demote an owner |
| **Adult** | Other grown-ups | Uses every module the household enables |
| **Child** | Kids | Uses the modules an admin allows. Cannot share anything outside the household |
| **Guest** | Visiting relatives, a babysitter | Sees only what is explicitly shared with them. The account can expire |

**Atlas assumes no mail server.** When someone forgets a password or loses a device, an admin issues a one-time reset/enrolment link through the admin console and hands it over in person. If an admin wants to configure email later, it's optional, not required.

**Reaching Atlas from outside the home is the operator's choice.** The documented, recommended option is a Cloudflare Tunnel (with Cloudflare Access on admin paths). Your own reverse proxy with TLS, a VPN-only setup, or LAN-only all work too ([ADR-003](adr/003-internet-exposure.md), [SELF_HOSTING](SELF_HOSTING.md)). Atlas never needs an open inbound port of its own.

## Where it is going

```
Foundation       identity · users & family roles · permissions · admin console · module registry   → release 1.0.0
First modules    Files (from today's objects code) · Drive · Photos (leaning: Immich link + SSO)  → 1.x
Household apps   lift (tasks, habits, journal) as a module · Nextcloud-replacement features · Hermes (if decided)
```

These are **milestone names, not version numbers**. Releases follow SemVer: 0.x until Foundation ships as 1.0.0, and after that a new module is a MINOR release ([README](../README.md#versioning)).

- **Nextcloud.** Atlas aims to replace it over time, module by module. That is not part of Foundation.
- **Immich.** It stays a separate service. Atlas integrates it through a connector and single sign-on (SSO) and never re-implements photo management.
  - **Photos in First modules is undecided.** The current lean is that "Photos" is simply the Immich connector: a nav link plus SSO. No Atlas gallery would duplicate Immich. The final call comes at the start of First modules ([ARCHITECTURE](ARCHITECTURE.md) §8).
- **lift** (FastAPI/React/SQLite tasks, habits and journal) is later ported into an Atlas module. Its data moves to Atlas's PostgreSQL.
- **Hermes AI agent**: undecided. Foundation only leaves a hook: the event outbox plus the design for scoped service tokens ([ARCHITECTURE](ARCHITECTURE.md) §9).
- **Multi-tenancy (several households on one instance)** is out of scope. The tenant prototype on the `origin/dev` branch is **dropped**, not carried forward.

## Foundation scope

1. **Identity.** Atlas ships with a bundled identity provider (an IdP: Kanidm, inside the default compose stack), so installing it stays one command. Operators who already run an OIDC provider can bring their own instead ([ADR-001](adr/001-identity.md)). Immich can use the same login. 2FA (TOTP or passkey) is required for owner/admin. Atlas keeps server-side sessions, so it can revoke them.
2. **Users and family roles.** The roles above. Accounts can be created, disabled, given an expiry date (guests) and reset by an admin.
3. **Permissions.** Roles grant per-module permissions, and per-object ownership/sharing is designed for First modules ([ADR-002](adr/002-authorization.md)). Every check is enforced in one layer that routes cannot bypass.
4. **Admin console.** Manage users, roles, module enablement, connectors, active sessions and the audit log. Sensitive actions require step-up re-authentication.
5. **Module registry.** Modules declare their routes, permissions, nav entries and migrations in a manifest. Admins enable or disable modules, and each person sees only the modules they may use.
6. **Connectors.** A connector is a typed, admin-managed link to an external service. Immich is the first one: health, nav link and SSO.
7. **A safe base.** Fail-closed config, secrets only in env files, PostgreSQL with migrations run at container start, and CI that is green and required.

### Deployment defaults (configurable; see [SELF_HOSTING](SELF_HOSTING.md))

| Property | Default | Configurable via |
|---|---|---|
| Runtime | Docker Compose v2 on any 64-bit Linux host | — |
| Entry point | One bundled reverse proxy, the only container that publishes a port | — |
| Bind address : port | Loopback only, port `8080`. Put a tunnel or your own TLS proxy in front | `ATLAS_BIND_ADDRESS`, `ATLAS_HTTP_PORT` |
| Health check | `GET /health` → `{"status":"ok"}` | — |
| Secrets | Env files in a secrets directory, generated on first start if missing | `ATLAS_SECRETS_DIR` |
| Time zone | `UTC` | `TZ` |
| Database data | Named Docker volume, declared external so `docker compose down -v` can't delete it | `ATLAS_PGDATA_VOLUME` |
| Public URLs | None by default. You must set the Atlas URL and the IdP URL | `ATLAS_PUBLIC_URL`, `ATLAS_IDP_PUBLIC_URL` |

**Never run `docker compose down -v`.** The design protects data even if you do, but don't ([ADR-005](adr/005-database-and-migrations.md)).

## Explicit non-goals for Foundation

| Not in Foundation | Why / where it goes |
|---|---|
| Drive, Photos, file upload/download in the new platform | The existing code stays as it is (apart from the Phase 0 security patches) and becomes the first modules (First modules milestone) |
| Provisioning VMs, containers, DNS, tunnels or certificates from the admin console | Infrastructure belongs to the operator's own tooling. The console manages **Atlas** users, roles, modules and connectors only |
| Writing our own password/TOTP/passkey/OIDC-provider stack | Atlas relies on a proven IdP ([ADR-001](adr/001-identity.md)) |
| Required email (password reset mail, notifications) | Many homes have no mail server. Admins issue reset links instead |
| Public self-registration | Accounts are created only by admins |
| Multiple households / multi-tenancy | One household per instance. The `origin/dev` tenant model is dropped |
| Re-implementing photo management | Immich does that, and Atlas connects to it |
| Feature parity with Nextcloud (sync clients, WebDAV, calendar, contacts) | Considered module by module after First modules |
| Hermes AI agent features | Undecided. Only an integration hook exists |
| Native mobile apps | Responsive web/PWA. Immich keeps its own mobile app (with SSO) |
| Third-party or runtime-loaded plugins | Modules are first-party code in this repo, compiled in and enabled by config |
| High availability, clustering | Single host. Backups and a tested restore cover recovery |

## What "done" looks like for Foundation

On a fresh Linux host, an operator clones the repo, sets two public URLs and runs one command. They then have:

- Atlas and its bundled IdP running, with strong secrets generated, nothing secret in the repo, and only one loopback port published.
- An owner account enrolled with 2FA.

Every household member can sign in, at home or remotely through the exposure option the operator chose, with their own account and the right 2FA for their role. The same login opens Immich.

An admin can:
- create a child account;
- reset a forgotten credential;
- end a lost device's sessions;
- turn a module off.

Every one of those actions shows in the audit log. Upgrades and redeploys never lose data, and backups restore.

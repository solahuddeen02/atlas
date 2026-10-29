# ADR-001: Identity: use an existing OIDC identity provider; Atlas is a relying party

- **Status:** Proposed. Confirmed or reversed by the Phase 5 spike in [PLAN-v1](../PLAN-v1.md)
- **Date:** 2026-10-29
- **Deciders:** Deen (owner)
- **Related:** [AUDIT](../AUDIT.md) §3.1 (A1–A9), [ADR-002](002-authorization.md), [ADR-003](003-internet-exposure.md)

## Context

Today Atlas issues its own HS256 JWTs. The audit found:

- A forgeable default `SECRET_KEY` (A1, verified).
- An unmaintained JWT/hash stack in `python-jose` and `passlib` (A2).
- No revocation (A3).
- Tokens kept in `localStorage` (A4).
- No password policy (A5).
- No rate limiting (A6).

v1 has to deliver:

- Family accounts with roles.
- 2FA (TOTP, ideally passkeys too), strongest for owner/admin.
- Admin-driven credential resets **without a mail server**.
- Step-up re-authentication for admin actions.
- Access from outside through a Cloudflare Tunnel.
- **SSO into Immich**, including the Immich mobile app. Immich supports OIDC login and handles mobile login through a redirect-URI override ([Immich OAuth docs](https://docs.immich.app/administration/oauth)).

There is one maintainer, and the service is exposed to the internet.

To SSO into Immich, *something* must act as an **OIDC provider (OP)**. That makes the real question: do we build an OP or run one?

## Options

### A. Build it in Atlas on proven libraries

Libraries: `authlib` (as both OP and RP), `argon2-cffi`, `pyotp`, `py_webauthn`, plus our own session store.

- Atlas holds the passwords and TOTP secrets, and implements authorization-code + PKCE, discovery, JWKS, key rotation, consent and logout as an OP for Immich.
- Pros:
  - One service.
  - Total UX control.
  - The admin console manages credentials directly.
- Cons:
  - We would write and own an internet-facing OP and a credential store. That is the most security-critical code in the system, and a solo maintainer has the least capacity to get it right. The audit shows how easily small auth mistakes reach `main`.
  - Passkeys, recovery and brute-force protection would all be ours to build.

### B. Authentik

- Full-featured IdP with a Python/Django codebase.
- Its own web admin, visual "flows" and an enrolment/recovery UI.
- Password + TOTP + WebAuthn.
- Admin can generate recovery links.
- Rich REST API.
- Since 2025.10 it needs only PostgreSQL; Redis was removed ([authentik blog](https://goauthentik.io/blog/2025-11-13-we-removed-redis/)).
- Widely documented with Immich (Immich's own example uses it).
- Pros:
  - Most flexible.
  - Best end-user and admin UI.
  - Huge community.
- Cons:
  - The largest attack surface and the heaviest footprint (server + worker, about 1 GB RAM class).
  - A history of security advisories, as you'd expect from a big feature set.
  - Configuring flows is a project in its own right.

### C. Pocket ID

- Small, passkey-only OIDC provider ([sign-in methods](https://pocket-id.org/docs/guides/sign-in-methods)).
- Admins generate expiring one-time login codes from the UI or CLI. Those codes cover onboarding and recovery without mail ([user management](https://pocket-id.org/docs/setup/user-management)).
- Groups restrict which OIDC clients each user may reach.
- Has a REST API.
- Pros:
  - Simplest.
  - Phishing-resistant by construction.
  - Lightweight.
  - The no-mail recovery story fits perfectly.
- Cons:
  - **No passwords and no TOTP at all.** Every family member needs a passkey-capable device. That's hard for young children, shared tablets and guests, and it doesn't meet the stated "2FA (TOTP)" requirement.
  - Admins cannot add passkeys for users.

### D. Kanidm

- Identity server written in Rust, with security-first defaults.
- Supports passwords + TOTP, passkeys, and per-group credential policies.
- Admin-issued credential-reset tokens cover enrolment and recovery without mail.
- OIDC provider that **requires PKCE S256 and ES256 by default**, with per-client "legacy" escape hatches ([Kanidm OAuth2 examples](https://kanidm.github.io/kanidm/stable/integrations/oauth2/examples.html)).
- A REST API plus a CLI.
- Immich with ES256 + `client_secret_post` is reported working ([kanidm discussion #3994](https://github.com/kanidm/kanidm/discussions/3994)).
- Pros:
  - Smallest risk per feature: memory-safe, opinionated and few knobs.
  - Supports both passkeys and password+TOTP, so each role can get the right strength.
  - Light on resources.
  - A read-only LDAPS gateway, useful later for Nextcloud-style services.
- Cons:
  - The admin UX is mostly CLI and API, with a minimal web UI.
  - Insists on TLS on its own listener, so we need an internal certificate behind nginx.
  - Smaller community than Authentik.
  - Opinionated: some legacy clients need the escape hatches.

## Comparison

| Criterion | A. Build in Atlas | B. Authentik | C. Pocket ID | D. Kanidm |
|---|---|---|---|---|
| **Security** (attack surface, maturity, defaults) | ✗ Our own OP and credential store on the internet | ~ Mature but large; frequent advisories | ✓ Small, passkeys only | ✓✓ Small, memory-safe, strict defaults |
| **Effort for us** (build + run) | ✗✗ Months, plus permanent upkeep | ~ Low code, high configuration | ✓✓ Lowest | ✓ Low code; CLI learning curve |
| **Family UX**: adults | ✓ (we design it) | ✓✓ Polished UI | ✓ Passkeys are great when available | ✓ Passkey or password+TOTP |
| **Family UX**: children/guests | ✓ | ✓ Per-group flows | ✗ Need a passkey device | ✓ Per-group credential policy |
| **Reset without mail** | Build it | ✓ Recovery link | ✓ One-time login code | ✓ Credential reset token |
| **TOTP** | Build it | ✓ | ✗ | ✓ |
| **Passkeys** | Build it | ✓ | ✓✓ (only) | ✓ |
| **Immich SSO, web** | Build an OP | ✓ Documented by Immich | ✓ | ✓ Reported working (ES256) |
| **Immich mobile app** | Build an OP | ✓ | ✓ | ✓ Via Immich's mobile redirect override; to verify in the spike |
| **API for the Atlas admin console** | Native | ✓✓ Rich | ✓ | ✓ REST + service accounts |
| **Step-up re-auth** (`prompt=login` / `max_age`, `auth_time`) | Build it | ✓ | To verify | To verify in the spike |
| **Footprint on one VM** | — | Heavy | Tiny | Small |

## Decision

**Use an external OIDC IdP, and make Atlas a pure relying party. Choose Kanidm (D), with Authentik (B) as the documented fallback.**

The Phase 5 spike confirms or reverses this.

### Why Kanidm

- It is the only small, security-first option that supports **both** passkeys and password+TOTP.
- Credential policy can follow the family role:
  - owner/admin: passkey (or at least MFA)
  - adult: MFA
  - child/guest: password allowed, MFA encouraged
- Its no-mail reset tokens match how the household actually works.
- Pocket ID is attractive, but it fails the TOTP and child/guest requirements.
- Authentik works, but costs more attack surface and RAM than a family needs.
- Building it ourselves (A) is ruled out: writing an internet-facing OP and credential store is exactly the class of work v1 should avoid.

### Fallback trigger

Switch to Authentik if the spike shows any of the following:

- Immich mobile SSO cannot be made to work cleanly with Kanidm.
- Kanidm cannot provide a verifiable freshness signal for step-up (`auth_time` honoured with `max_age` / `prompt=login`).
- The admin API cannot do what the console needs (create a person, disable it, issue a reset token, set group membership).

The rest of the Atlas design is IdP-agnostic: plain OIDC plus a small `IdentityAdmin` adapter. A switch only changes the adapter and the deploy.

## How it works

### Responsibilities split

| Concern | Owned by |
|---|---|
| Credentials (passwords, TOTP, passkeys), MFA policy, brute-force lockout, reset tokens | **IdP** |
| Who a person is (`sub`, username, display name) | **IdP**. Atlas stores `sub` as the stable key |
| Family role, module permissions, object ownership/sharing, account expiry, enable/disable inside Atlas | **Atlas** ([ADR-002](002-authorization.md)) |
| Which IdP clients a person may use (Atlas, Immich) | **IdP groups**, *written by Atlas's admin console* through the adapter |

### Atlas as relying party

- Atlas uses the OIDC authorization-code flow with PKCE, through `authlib`'s client.
- It validates the ID token itself: `iss`, `aud`, `exp`, `nonce`, and the signature against the cached JWKS.
- After the callback, Atlas creates its **own server-side session**. Atlas never puts IdP tokens in the browser. The session is an opaque random ID in a `__Host-` cookie that is HttpOnly, Secure and SameSite=Lax, with a row in Postgres. Revocation is a delete ([ADR-003](003-internet-exposure.md)).
- **No local passwords exist in Atlas.** The current `users.hashed_password`, `/auth/register` and HS256 JWTs are retired (see PLAN Phase 6).
- **Provisioning is admin-only.** An admin creates a person in the Atlas console. Atlas calls the IdP admin API to create the identity and issue a reset/enrolment token, and the admin hands over the link or QR code in person. A login from an IdP `sub` that Atlas doesn't know is rejected; there is no just-in-time sign-up.

### Immich SSO

- Immich is a separate OIDC client of the same IdP.
- Immich access is controlled by an IdP group (e.g. `immich_users`). Atlas's console toggles that group per person.
- Immich-side admin rights stay in Immich, or are mapped through its role claim if we want that later.

### Step-up

- For admin actions, Atlas re-runs the OIDC flow with `max_age=0` / `prompt=login`, then requires `auth_time` to be no older than 5 minutes.
- Combined with an IdP policy of "admins must use passkey/MFA", this gives "fresh MFA" without Atlas handling credentials.

### Break-glass

If the IdP is down or misconfigured, the owner can still get in:

- Run `docker compose exec atlas atlas-admin break-glass --user <owner>` on the host.
- It mints a one-time, 10-minute Atlas login URL that is audited and works only for the `owner` role.
- It requires host shell access, so it adds no remote attack surface.

### Mobile

- Atlas itself is a responsive web app/PWA, using cookies and the same flow.
- Immich mobile uses the IdP directly.
- If Hermes or other API clients appear later, they use scoped, revocable Atlas personal access tokens (hook only in v1; see ARCHITECTURE §9). They do not get raw IdP tokens.

## Consequences

**Positive:**
- Atlas carries no credential storage or OP code.
- One login for Atlas and Immich.
- Passkeys and TOTP come built in.
- Resets work without mail.
- Audit findings A1/A2/A4/A5/A7/A8 disappear structurally, because the code that had them is removed rather than patched.

**Negative:**
- One more service to run, back up and upgrade.
- The IdP becomes a single point of failure for login (mitigated by break-glass).
- A second public hostname must go through the tunnel.
- Kanidm needs an internal TLS certificate.
- The admin console depends on the IdP API. If the IdP is down, user changes queue or fail loudly; they never half-apply.

**Follow-ups:**
- The Phase 5 spike checklist (PLAN-v1).
- An `IdentityAdmin` interface with a Kanidm implementation and a fake for tests.
- IdP backup and restore added to the backup runbook.

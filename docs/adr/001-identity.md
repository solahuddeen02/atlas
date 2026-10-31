# ADR-001: Identity: bundled OIDC identity provider (Kanidm) by default, bring-your-own OIDC supported; Atlas is a relying party

- **Status:** **Accepted (provisional).** Kanidm is the default IdP, pending the pre-Phase-1 spike ([PLAN-v1](../PLAN-v1.md), "Spike S"). Results are recorded [below](#spike-results).
- **Date:** 2026-10-29. Revised the same day for generic self-hosting and the bundled IdP.
- **Deciders:** project maintainer
- **Related:** [AUDIT](../AUDIT.md) §3.1 (A1–A9), [ADR-002](002-authorization.md), [ADR-003](003-internet-exposure.md), [ADR-004](004-config-and-secrets.md), [SELF_HOSTING](../SELF_HOSTING.md)

## Context

Today Atlas issues its own HS256 JWTs. The audit found:

- a forgeable default `SECRET_KEY` (A1, verified);
- an unmaintained JWT/hash stack, `python-jose` and `passlib` (A2);
- no revocation (A3);
- tokens kept in `localStorage` (A4);
- no password policy (A5);
- no rate limiting (A6).

Foundation has to deliver:

- household accounts with roles;
- 2FA (TOTP, ideally passkeys too), strongest for owner/admin;
- admin-driven credential resets **without a mail server**;
- step-up re-authentication for admin actions;
- remote access through whatever exposure the operator chooses (ADR-003);
- **SSO into Immich**, including the Immich mobile app. Immich supports OIDC login, and its mobile app works through a redirect-URI override ([Immich OAuth docs](https://docs.immich.app/administration/oauth)).

Atlas is a public project that invites others to self-host. **Installation must stay one command** for an operator with no existing identity infrastructure. Operators who already run an OIDC provider should be able to use it.

To SSO into Immich, *something* must act as an **OIDC provider (OP)**. The question is whether we build one, bundle one, or require one.

## Options

### A. Build it into Atlas on proven libraries

Libraries: `authlib` (as OP and RP), `argon2-cffi`, `pyotp`, `py_webauthn`, plus our own session store.

- Atlas would hold the passwords and TOTP secrets, and implement authorization-code + PKCE, discovery, JWKS, key rotation, consent and logout as an OP for Immich.
- Pros: a single service and full UX control.
- Cons: we'd write and own an internet-facing OP and credential store. That is the most security-critical code in the system, and a small project has the least capacity to get it right. The audit shows how easily small auth mistakes reach `main`. Passkeys, recovery and brute-force protection would all be ours to build.

### B. Authentik

- Full-featured IdP, Python/Django.
- Its own web admin, visual "flows" and an enrolment/recovery UI.
- Password + TOTP + WebAuthn. Admins can generate recovery links. Rich REST API.
- Needs only PostgreSQL since 2025.10, after Redis was removed ([authentik blog](https://goauthentik.io/blog/2025-11-13-we-removed-redis/)).
- Immich's own docs use it as an example.
- Pros: the most flexible option, with the best end-user and admin UI and a large community.
- Cons: the largest attack surface and the heaviest footprint (server + worker, roughly 1 GB RAM). A history of advisories, as you'd expect from a big feature set. Configuring flows is a project of its own. Bundling it would roughly double Atlas's resource needs.

### C. Pocket ID

- A small, passkey-only OIDC provider ([sign-in methods](https://pocket-id.org/docs/guides/sign-in-methods)).
- Admin-generated, expiring one-time login codes cover onboarding and recovery without mail ([user management](https://pocket-id.org/docs/setup/user-management)).
- Groups restrict which OIDC clients each user may reach.
- Pros: the simplest option, phishing-resistant by construction, lightweight.
- Cons: **no passwords and no TOTP.** Every household member needs a passkey-capable device, which is hard for young children, shared tablets and guests, and it fails the TOTP requirement.

### D. Kanidm

- An identity server written in Rust, with security-first defaults.
- Password + TOTP, passkeys and per-group credential policies.
- Admin-issued credential-reset tokens cover enrolment and recovery without mail.
- An OIDC provider that **requires PKCE S256 and ES256 by default**, with per-client "legacy" escape hatches ([Kanidm OAuth2 examples](https://kanidm.github.io/kanidm/stable/integrations/oauth2/examples.html)).
- A REST API plus a CLI.
- Immich working with ES256 + `client_secret_post` has been reported ([kanidm discussion #3994](https://github.com/kanidm/kanidm/discussions/3994)).
- Pros: the least risk per feature, a small footprint (well suited to bundling), and both credential styles.
- Cons:
  - Admin UX is mostly CLI and API. Atlas's console covers the household tasks.
  - It insists on TLS on its own listener, which means an internal certificate.
  - It needs its own origin, a hostname (see below).
  - Its community is smaller than Authentik's.

### Packaging options (orthogonal to the choice of IdP)

| Packaging | One-command install | Works for operators with an existing IdP | Effort for us |
|---|---|---|---|
| **P1. Bundled IdP in the default compose** | ✓ | Needs an opt-out | Medium: bootstrap, internal TLS, upgrades |
| **P2. Require an external IdP** | ✗ (install an IdP first) | ✓ | Low |
| **P3. Bundled by default + bring-your-own OIDC (BYO) as a supported alternative** | ✓ | ✓ | Medium + one adapter seam |

## Comparison

| Criterion | A. Build in Atlas | B. Authentik | C. Pocket ID | D. Kanidm |
|---|---|---|---|---|
| **Security** (attack surface, maturity, defaults) | ✗ Our own OP on the internet | ~ Mature but large | ✓ Small, passkeys only | ✓✓ Small, memory-safe, strict |
| **Effort** (build + run) | ✗✗ | ~ Low code, high configuration | ✓✓ | ✓ |
| **Household UX: adults** | ✓ | ✓✓ | ✓ | ✓ Passkey or password+TOTP |
| **Household UX: children/guests** | ✓ | ✓ | ✗ Need a passkey device | ✓ Per-group policy |
| **Reset without mail** | Build it | ✓ Recovery link | ✓ One-time code | ✓ Reset token |
| **TOTP / passkeys** | Build it | ✓ / ✓ | ✗ / ✓✓ | ✓ / ✓ |
| **Immich SSO: web / mobile** | Build an OP | ✓ / ✓ | ✓ / ✓ | ✓ reported / **to verify in the spike** |
| **API for the Atlas console** | Native | ✓✓ | ✓ | ✓ REST + service accounts |
| **Step-up (`max_age`/`prompt=login`, `auth_time`)** | Build it | ✓ | To verify | **To verify in the spike** |
| **Footprint when bundled** | — | Heavy | Tiny | Small |

## Decision

1. **Atlas is a pure OIDC relying party.** It never stores credentials, and it never becomes an OP.
2. **Packaging P3.**
   - The default `docker-compose.yml` includes **Kanidm** as a bundled service, controlled by the compose profile `bundled-idp`, which is on by default. That keeps the install to one command.
   - **Bring-your-own OIDC** is a supported alternative: turn the profile off and set the issuer and client settings.
3. **Kanidm is the bundled IdP, provisionally.** It's the only small, security-first option that offers both passkeys and password+TOTP, which lets credential policy follow the role. Pocket ID fails TOTP and the child/guest case. Authentik is too heavy to bundle by default (it remains a fine BYO choice). Building our own (A) is rejected.
4. **The pre-Phase-1 spike** ([PLAN-v1](../PLAN-v1.md), "Spike S") validates Kanidm behind a reverse proxy. It covers web and mobile Immich login, TOTP, step-up and the admin API. If a **must-pass** item fails with no workaround, the bundled IdP switches to Authentik under a superseding ADR. The rest of Atlas is IdP-agnostic (plain OIDC plus a small `IdentityAdmin` adapter), so a switch changes only the adapter and the compose service.

## How it works

### Responsibilities

| Concern | Owned by |
|---|---|
| Credentials (passwords, TOTP, passkeys), MFA policy, brute-force lockout, reset tokens | **IdP** |
| Who someone is (`sub`, username, display name) | **IdP**. Atlas stores `(issuer, sub)` as the stable key |
| Household role, module permissions, object ownership/sharing, account expiry, enable/disable inside Atlas | **Atlas** ([ADR-002](002-authorization.md)) |
| Which IdP clients a person may use (Atlas, Immich) | **IdP groups.** With the bundled IdP, the Atlas console writes them through `IdentityAdmin`. With BYO, the operator manages them in their IdP |

### Atlas as relying party

- Authorization-code flow with PKCE, through `authlib`'s client.
- Strict ID-token validation: `iss`, `aud`, `exp`, `nonce`, the signature against the cached JWKS, and `auth_time`.
- After the callback, Atlas creates its **own server-side session**. It uses an opaque `__Host-` cookie that is HttpOnly, Secure and SameSite=Lax, with a row in Postgres. IdP tokens never reach the browser ([ADR-003](003-internet-exposure.md)).
- **No local passwords exist in Atlas.** The legacy `users.hashed_password`, `/auth/register` and HS256 JWTs are retired ([PLAN-v1](../PLAN-v1.md) Phase 5).
- **No just-in-time sign-up.** A login from an unknown `(issuer, sub)` is rejected unless it completes an admin-issued **account link** (below).

### Provisioning

**Bundled IdP (`IdentityAdmin` = Kanidm adapter):**
1. An admin creates a person in the Atlas console.
2. In one operation, Atlas creates the Kanidm person, sets their groups according to the role, and issues a credential-reset token.
3. The console shows the token once, as a link and a QR code, for in-person hand-over.
4. The person enrols a password + TOTP or a passkey.

**BYO OIDC (`IdentityAdmin` = `manual`):**
1. The operator creates the identity in their own IdP.
2. In Atlas, an admin creates the user and receives a one-time **link code** (a single-use, 24-hour URL).
3. The person opens it and signs in at the IdP, and Atlas binds that `(issuer, sub)` to the account.
4. Credential resets happen in the operator's IdP. The console shows guidance instead of a reset button.

Atlas never binds accounts by matching email or username claims, because those can be changed or spoofed in some IdPs.

### Immich SSO

- Immich is a separate OIDC client of the same IdP.
- Access is controlled by an IdP group (e.g. `immich_users`). With the bundled IdP, the console toggles that group per person.
- Immich-side admin rights stay in Immich. They can optionally be mapped through Immich's role claim.

### Step-up

- For permissions flagged `step_up`, Atlas re-runs the OIDC flow with `max_age=0` / `prompt=login`.
- It then requires `auth_time` to be no older than `ATLAS_STEP_UP_MAX_AGE` (default 300 s).
- Combined with an IdP policy of "admins must use passkey/MFA", this gives "fresh MFA" without Atlas handling credentials.
- If a BYO IdP doesn't honour `max_age`, Atlas refuses `step_up` actions with a clear configuration error rather than silently skipping step-up.

### Break-glass

If the IdP is down or misconfigured, the owner can still get in:

- Run `docker compose exec atlas-api atlas-admin break-glass --user <owner>`.
- It mints a one-time, 10-minute login URL that works only for the `owner` role and is audited.
- It requires shell access to the host, so it adds no remote attack surface.

### Mobile and API clients

- Atlas is a responsive web app/PWA, using cookies and the same flow.
- Immich mobile talks to the IdP directly.
- Future API clients (e.g. Hermes) would use scoped, revocable Atlas personal access tokens ([ARCHITECTURE](../ARCHITECTURE.md) §9). They never receive raw IdP tokens.

## The bundled IdP's hostname and TLS requirements

These requirements come from how OIDC and passkeys work. Operators must plan for them before installing.

1. **The IdP needs its own public origin.**
   - It needs a dedicated hostname such as `id.example.com`, set via `ATLAS_IDP_PUBLIC_URL`.
   - Kanidm can't be served under a sub-path of the Atlas hostname.
   - Browsers, Atlas and Immich must all reach the IdP at this **same HTTPS URL**, because it's the OIDC issuer.
   - Immich's mobile app reaches it from outside the home, so the URL must be reachable wherever family members are, not just on the LAN.
2. **Choose the IdP hostname carefully and keep it.** Passkeys are bound to the IdP's domain (the WebAuthn relying-party ID). Changing that hostname later invalidates every enrolled passkey, and users must re-enrol. Password + TOTP survive the change.
3. **Public TLS is terminated in front of Atlas.** The tunnel, your reverse proxy or your load balancer handles it. Atlas's bundled proxy speaks plain HTTP on its (loopback by default) port. Both public hostnames need valid certificates, because Immich mobile and the browsers' WebAuthn APIs reject untrusted ones.
4. **Kanidm also requires TLS on its own listener.**
   - On first start, the `atlas-init` service generates a private internal CA and a certificate for the internal `idp` service name, stored under the secrets directory.
   - The bundled proxy re-encrypts to Kanidm and verifies that CA.
   - Atlas's back-channel calls (discovery, token, JWKS, admin API) go to the internal URL and verify the same CA, while still validating `iss` against the public issuer.
   - Operators don't manage this certificate. It's rotated by re-running `atlas-init --rotate-idp-cert`.
5. **Client IPs.** Kanidm's own lockout and logging need the real client address. Configure it to trust forwarded-address headers **only** from the bundled proxy. The exact option name depends on the Kanidm version and is confirmed in the spike.
6. **Hairpin access.** The Immich server (a separate stack) must reach the IdP's public URL server-to-server. If your network can't hairpin to its own public hostname, add a local DNS override that points that hostname to your reverse proxy. Details are in [SELF_HOSTING](../SELF_HOSTING.md).

With **BYO OIDC**, none of these Kanidm specifics apply. The operator's IdP just needs to be HTTPS and reachable by browsers, Atlas and Immich.

## Consequences

**Positive:**
- Atlas carries no credential storage or OP code.
- One login for Atlas and Immich.
- Passkeys and TOTP come built in.
- Resets work without mail.
- The install stays one command.
- Operators with an IdP can use it.
- Audit findings A1/A2/A4/A5/A7/A8 disappear structurally, because the code that had them is removed.

**Negative:**
- One more bundled service to upgrade and back up.
- The IdP is a single point of failure for login (mitigated by break-glass).
- Operators need a **second public hostname** and valid TLS for it.
- Kanidm's internal TLS adds bootstrap steps (automated by `atlas-init`).
- With BYO, some console features (create/reset in the IdP) become manual.

**Follow-ups:**
- The spike (below).
- An `IdentityAdmin` interface with `kanidm`, `manual` and `fake` implementations (`authentik` possible later).
- `atlas-init` IdP bootstrap: create the Atlas OAuth2 client, the service account for the console, role groups and the first owner's reset token.
- IdP backup and restore in [SELF_HOSTING](../SELF_HOSTING.md).

## Spike results

**Status: _pending_.** The spike runs before Phase 1 ([PLAN-v1](../PLAN-v1.md), "Spike S"). Results are filled in here, with evidence: commands, screenshots, versions tested, and no hostnames or secrets.

- **Test setup:**
  - Kanidm `<version>` and Immich server `<version>`.
  - Immich mobile `<platform/version>`.
  - Reverse proxy: the bundled nginx, re-encrypting to Kanidm's internal TLS.
  - Public TLS in front: `<tunnel or TLS proxy>`.
  - Two hostnames, for Atlas and the IdP.

| # | Check | Must pass? | Result | Evidence / notes |
|---|---|---|---|---|
| 1 | Kanidm behind the reverse proxy: public origin over HTTPS, internal TLS re-encrypted, real client IP logged, lockout works per client | Must | _pending_ | |
| 2 | Per-group credential policy: admin group requires passkey or MFA; child group allows password | Must | _pending_ | |
| 3 | No-mail enrolment: admin-issued reset token → user enrols **password + TOTP**; separately a **passkey** | Must | _pending_ | |
| 4 | **TOTP login** works end to end in the browser (and lockout after N bad codes) | Must | _pending_ | |
| 5 | Atlas RP (authlib) with PKCE + ES256: login, ID-token validation, `nonce`, logout via end-session | Must | _pending_ | |
| 6 | **Step-up:** `prompt=login`/`max_age=0` forces re-auth, and the returned `auth_time` is fresh and verifiable | Must | _pending_ | If not supported: fallback design or Authentik |
| 7 | **Immich web** SSO via Kanidm (ES256, `client_secret_post`), access restricted by group | Must | _pending_ | |
| 8 | **Immich mobile** SSO via Kanidm (Immich's mobile redirect override), from outside the LAN through the reverse proxy | Must | _pending_ | |
| 9 | **Admin API** via service account: create/disable/enable person, set group membership, issue reset token | Must | _pending_ | |
| 10 | Admin API: list and revoke a person's sessions | Should | _pending_ | If missing: Atlas revokes only its own sessions and documents the gap |
| 11 | Backup and restore of the Kanidm volume (online backup or stopped-volume copy) | Must | _pending_ | |
| 12 | `atlas-init` bootstrap is idempotent: re-running changes nothing, and an existing CA/cert is never overwritten | Must | _pending_ | |
| 13 | Resource use (idle RSS, CPU) on a small host | Info | _pending_ | |

**Outcome:** _pending_. One of:
- (a) Kanidm confirmed: status → Accepted.
- (b) Kanidm with documented workarounds: Accepted, with the workarounds listed here.
- (c) Superseded by ADR-00X (Authentik bundled).

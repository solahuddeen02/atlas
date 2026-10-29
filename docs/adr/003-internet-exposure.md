# ADR-003: Internet exposure: one loopback entry point, operator-chosen exposure (Cloudflare Tunnel recommended), 2FA, step-up, rate limiting, revocable sessions

- **Status:** Proposed
- **Date:** 2026-10-29. Revised for generic self-hosting.
- **Related:** [AUDIT](../AUDIT.md) §3.1, §3.4, [ADR-001](001-identity.md), [ADR-002](002-authorization.md), [ADR-004](004-config-and-secrets.md), [SELF_HOSTING](../SELF_HOSTING.md)

## Context

Household members reach Atlas, the IdP and Immich from outside the home. Atlas is self-hosted by people with very different networks: some behind CGNAT, some with an existing reverse proxy, and some who want VPN-only access.

Users include children and older relatives on phones, and Immich's mobile app must keep working.

Today's app has:
- no rate limiting (audit A6);
- no revocation (A3);
- tokens in `localStorage` (A4);
- public `/docs` and no security headers (I3);
- CORS hard-coded to a dev origin (I2).

## Decision

### 1. One entry point inside the stack; exposure is the operator's choice

- The compose stack has exactly **one container that publishes a port**: the bundled reverse proxy, `atlas-proxy` (nginx).
- It publishes `${ATLAS_BIND_ADDRESS:-127.0.0.1}:${ATLAS_HTTP_PORT:-8080}` and speaks plain HTTP.
- `atlas-api`, the bundled IdP and Postgres have **no published ports** and are reachable only on the internal compose network.
- **The default bind is loopback.** Nothing is reachable from the network until the operator deliberately puts something in front.
- Binding to all interfaces (`ATLAS_BIND_ADDRESS=0.0.0.0`) is allowed, but the docs warn about it. It only makes sense behind a firewall with your own TLS proxy, because browsers require HTTPS for Secure cookies and passkeys.

```
                    ┌──────────── public TLS terminates here (operator's choice) ────────────┐
Internet / LAN ──▶  │  Option 1: Cloudflare Tunnel connector  (recommended)                     │
                    │  Option 2: your reverse proxy (Caddy / Traefik / nginx) with ACME TLS     │
                    │  Option 3: VPN only (WireGuard / Tailscale-style) + TLS proxy on the VPN  │
                    └───────────────────────────────┬───────────────────────────────────────────┘
                                                    ▼  http://<bind>:<port>   (loopback by default)
                                       atlas-proxy (nginx, only published port)
                                          ├─ Host = ATLAS_PUBLIC_URL host     ─▶ SPA static + atlas-api
                                          └─ Host = ATLAS_IDP_PUBLIC_URL host ─▶ idp (internal TLS, ADR-001)
Immich: its own stack and hostname. Uses OIDC against the IdP's public URL. Atlas talks to it as a connector.
```

### Exposure options

| Option | When to choose it | Notes |
|---|---|---|
| **1. Cloudflare Tunnel** (recommended) | No inbound ports, CGNAT, no static IP, want edge rate limiting/WAF | The connector runs wherever you like (host package or container) and forwards each public hostname to `http://<bind>:<port>`. Add Cloudflare Access on admin paths (§6). Example in [SELF_HOSTING](../SELF_HOSTING.md) |
| **2. Your own reverse proxy with TLS** | You already run Caddy/Traefik/nginx with certificates | Proxy each hostname to `http://<bind>:<port>` and pass `Host` and the client address. Do edge rate limiting yourself |
| **3. VPN only** | Family devices can all run a VPN client; you want zero public exposure | Still needs HTTPS hostnames with trusted certificates (e.g. DNS-01 ACME) for passkeys and Immich mobile |
| **4. LAN only** | Testing | Not for real use without HTTPS. Secure cookies and passkeys won't work over plain HTTP, except on `localhost` |

**Requirements on whatever sits in front** (documented in SELF_HOSTING):
- Terminate valid public TLS for both hostnames (Atlas and IdP).
- Forward the original `Host`.
- Pass the client IP in a header.
- Don't cache `/api/*`.

**Client IP trust:**
- The bundled proxy takes the client IP from `ATLAS_REAL_IP_HEADER`: `CF-Connecting-IP` for option 1, `X-Forwarded-For` (rightmost trusted hop) for options 2 and 3.
- It trusts that header **only** from peers in `ATLAS_TRUSTED_PROXY_CIDRS` (default: loopback and the compose network gateway).
- It forwards a single, overwritten `X-Real-IP` to the app.
- The app trusts forwarded headers only from the bundled proxy.

**Host allow-list:**
- The proxy serves only the hostnames of `ATLAS_PUBLIC_URL` and `ATLAS_IDP_PUBLIC_URL`. Any other `Host` gets a 444.
- Hostnames live in the operator's untracked `.env`, never in the repo.

**Docs:**
- `/docs`, `/redoc` and `/openapi.json` are disabled when `ATLAS_ENV=production`, which is the default.

### 2. Authentication strength by role

The IdP's account policy enforces these minimums. With the bundled IdP, `atlas-init` configures them. With BYO, the operator does, and Atlas documents what's expected. Lifetimes are configurable; the defaults are:

| Role | Minimum credential | Session idle / absolute (defaults) |
|---|---|---|
| owner, admin | Passkey, or password + **TOTP** | 12 h / 7 d |
| adult | Password + TOTP, or passkey | 14 d / 30 d |
| child | Password (MFA encouraged), or passkey | 14 d / 30 d |
| guest | Password + TOTP, or passkey | 1 d / until `expires_at` |

- With the bundled IdP, Atlas keeps the person's IdP group in sync with their Atlas role.
- Login is refused if the group and the role disagree, which avoids a "child" account that got admin MFA exemptions.

### 3. Step-up authentication for admin actions

- Permissions flagged `step_up` ([ADR-002](002-authorization.md) rule 6) require `strong_auth_at` to be newer than `ATLAS_STEP_UP_MAX_AGE` (default 300 s). Examples: user create/disable/reset, role and permission edits, module enable/disable, connector edits, revoking others' sessions.
- Re-auth works like this:
  1. Atlas starts an OIDC request with `prompt=login`, `max_age=0`.
  2. It verifies the returned `auth_time`.
  3. It updates `strong_auth_at`.
  4. It **rotates the session ID**.
- Step-up is per session.

### 4. Sessions and revocation

**Storage:**
- Server-side sessions in Postgres: `sessions(id_hash, user_id, created_at, last_seen_at, strong_auth_at, ip, user_agent, revoked_at)`.
- The cookie carries a 256-bit random ID, and the DB stores only its SHA-256.

**Cookie:**
- `__Host-atlas_session`, with `Secure; HttpOnly; SameSite=Lax; Path=/`. No `Domain`.
- Nothing auth-related goes in `localStorage`, which closes A4.

**CSRF:**
- Unsafe methods require `X-CSRF-Token` matching a per-session token (synchronizer pattern), on top of SameSite.

**Revocation:**
- Users can list and end their own sessions.
- Admins can end all sessions for a user.
- Disabling a user, changing their role, or reaching a guest's `expires_at` ends all of that user's Atlas sessions immediately, and asks the IdP to end its sessions too where the adapter supports it (spike item 10).
- Logout deletes the session and calls the IdP's end-session endpoint.
- Every request checks the session row, so revocation is immediate. That closes A3.

**Rotation:**
- New session ID on login, on step-up and on role change.

### 5. Rate limiting (layered)

1. **Edge (if your exposure option has one):** with Cloudflare, add rate-limit rules on the IdP's authentication endpoints and on Atlas `/api/auth/*`, and enable managed WAF rules. With your own proxy, add the equivalent (e.g. a Caddy/Traefik rate-limit plugin) if available.
2. **Bundled proxy (always on):** `limit_req` keyed by the real client IP. There's a strict zone for `/api/auth/*` and step-up callbacks (`ATLAS_RATE_LIMIT_AUTH`, default 10 requests/min, small burst) and a general zone for `/api/*`. `client_max_body_size` comes from `ATLAS_MAX_BODY_SIZE`.
3. **App (always on):** a per-account and per-IP counter in Postgres for sensitive actions (auth callbacks, step-up, admin mutations, break-glass). It returns 429 with `Retry-After`.
   - Credential brute-force lockout belongs to the IdP, which Kanidm handles natively.

### 6. Cloudflare Access (only relevant to option 1)

**Trade-off:**
- Access adds a strong pre-authentication layer at the edge.
- It also breaks or degrades non-browser clients: Immich mobile, OIDC back-channel and discovery calls, PWA background requests, future API tokens.
- It adds a second login for household members.

**Recommendation when using the tunnel:**

| Surface | Cloudflare Access? | Reason |
|---|---|---|
| Atlas user app and API | **No** | Household UX, PWA requests. Protected by IdP login and Atlas sessions |
| IdP user-facing endpoints (authorize, token, JWKS, discovery, login UI) | **No** | Immich mobile and server-to-server OIDC calls must reach them without a challenge |
| Immich | **No** | The mobile app can't pass Access. Protected by its own SSO login |
| **Atlas admin console paths** (`/admin*`, `/api/v1/admin/*`) | **Yes (recommended)** | Browser-only, used by 1–2 people. Policy: owner/admin identities only. Defense in depth |
| **IdP admin surfaces** (if routed at all) | **Yes**, or don't route them publicly | Prefer administering the bundled IdP via `docker compose exec` |

**Rules:**
- Atlas must be fully secure **without** Access. No security decision in the app reads Access headers.
- Operators of options 2–4 can get a similar effect by restricting `/admin*` at their own proxy (e.g. to VPN/LAN source ranges).

## Additional hardening (bundled proxy and app)

**Headers:**
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: same-origin`
- A minimal `Permissions-Policy`
- `Strict-Transport-Security` when `ATLAS_PUBLIC_URL` is HTTPS (that's required in production)

**CORS:**
- Off in production, because the SPA and API are same-origin.
- Dev origins come from `ATLAS_DEV_CORS_ORIGINS`.

**Other:**
- Bounded pagination (`limit ≤ 100`).
- Uniform error bodies with no exception class names (audit F7).
- **`/health`** returns exactly `{"status":"ok"}`, with no auth and no dependency details.
- `/health/ready` checks DB and IdP reachability, and the bundled proxy answers it only for requests from `ATLAS_TRUSTED_PROXY_CIDRS`.

## Consequences

- Operators need two public HTTPS hostnames (Atlas, IdP) plus Immich's own. They're all served through the same entry point.
- Revocation is immediate, at the cost of one indexed lookup per request, which is trivial at household scale.
- Admin work prompts a re-login every few minutes, which is intentional and configurable.
- Security doesn't depend on the exposure option. The edge layers only add defense in depth.

## Verification

- `nginx -t` in CI.
- Compose test:
  - only `atlas-proxy` publishes a port;
  - the default bind is loopback;
  - unknown `Host` is dropped;
  - the real-IP header is ignored from untrusted peers.
- Session tests:
  - revoke → 401;
  - disable user → all sessions gone;
  - rotation on login and step-up;
  - cookie flags.
- CSRF: an unsafe request without the token gives 403.
- Rate limit: the N+1-th auth callback in a window gives 429.
- Headers present on HTML and API responses.
- Docs → 404 in production.

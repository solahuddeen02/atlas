# ADR-003: Internet exposure: Cloudflare Tunnel, 2FA, step-up, rate limiting, revocable sessions; Cloudflare Access only on admin surfaces

- **Status:** Proposed
- **Date:** 2026-10-29
- **Related:** [AUDIT](../AUDIT.md) §3.1, §3.4, [ADR-001](001-identity.md), [ADR-002](002-authorization.md), [ADR-004](004-config-and-secrets.md)

## Context

The family reaches Atlas, the IdP and Immich from outside the home.

- The deploy contract fixes nginx on `127.0.0.1:8081` as the only listener.
- There are no open inbound ports. A Cloudflare Tunnel connector on the host dials out and forwards public hostnames to that loopback port.
- Users include children and older relatives on phones.
- Immich's mobile app must keep working.

Today's app has no rate limiting, no revocation, tokens in `localStorage`, public `/docs` and no security headers (audit A3, A4, A6, I3).

## Decision

### 1. Network path

```
Internet ──HTTPS──▶ Cloudflare edge (TLS, WAF/rate rules, optional Access on admin paths)
                         │  outbound-only tunnel
                         ▼
              cloudflared (on the VM) ──▶ http://127.0.0.1:8081  nginx  (only listener)
                                                   ├─ Host: <atlas-host> ─▶ atlas-web (static) + atlas-api
                                                   └─ Host: <idp-host>   ─▶ idp (internal TLS)
Immich: its own stack and hostname. Talks OIDC to <idp-host>; Atlas reaches it as a connector.
```

**Ports:**
- Only nginx publishes a port, and only on `127.0.0.1:8081`.
- `atlas-api`, the IdP and Postgres have **no published ports**. They're reachable only on the internal compose network.

**Client IP:**
- Cloudflare sends `CF-Connecting-IP`.
- nginx trusts it *only* from the loopback peer (`set_real_ip_from 127.0.0.1; real_ip_header CF-Connecting-IP;`) and forwards a single, overwritten `X-Real-IP` to the app.
- The app trusts forwarded headers only from the nginx container.

**Host allow-list:**
- nginx serves only the configured hostnames. Any other `Host` gets a 444.
- Hostnames come from config, never from the repo.

**Docs:**
- `/docs`, `/redoc` and `/openapi.json` are disabled in production (`ATLAS_ENV=production`).

### 2. Authentication strength by role (enforced in the IdP, checked by Atlas)

| Role | Minimum credential (IdP account policy) | Session lifetime (idle / absolute) |
|---|---|---|
| owner, admin | Passkey, or password + TOTP | 12 h / 7 d |
| adult | Password + TOTP, or passkey | 14 d / 30 d |
| child | Password (MFA encouraged), or passkey | 14 d / 30 d |
| guest | Password + TOTP, or passkey | 1 d / until `expires_at` |

- TOTP and passkeys are handled by the IdP ([ADR-001](001-identity.md)). Atlas never sees credentials.
- Atlas keeps a mapping from role to required IdP group/policy. If a person's role changes, the console updates their IdP group. Login is refused if the IdP group and the Atlas role disagree.

### 3. Step-up authentication for admin actions

- Permissions flagged `step_up` require the session's `strong_auth_at` to be under **5 minutes** old ([ADR-002](002-authorization.md) rule 6). Examples: user create/disable/reset, role and permission edits, module enable/disable, connector edits, session revocation for others, break-glass review.
- Re-auth works like this:
  1. Atlas starts an OIDC request with `prompt=login`, `max_age=0`.
  2. On return, it verifies the ID token's `auth_time`.
  3. It updates `strong_auth_at`.
  4. It **rotates the session ID**.
- Step-up is per session, never shared across devices.

### 4. Sessions and revocation

**Storage:**
- Server-side sessions in Postgres: `sessions(id_hash, user_id, created_at, last_seen_at, strong_auth_at, ip, user_agent, revoked_at)`.
- The cookie carries a 256-bit random ID. The DB stores only its SHA-256.

**Cookie:**
- `__Host-atlas_session`, with `Secure; HttpOnly; SameSite=Lax; Path=/`. No `Domain`.
- Nothing auth-related goes in `localStorage`. That closes audit A4.

**CSRF:**
- Unsafe methods require an `X-CSRF-Token` header that matches a per-session token (synchronizer pattern), as well as SameSite.
- The SPA reads the token from a non-HttpOnly endpoint response, not from a cookie.

**Revocation:**
- Users see their active sessions and can end any of them.
- Admins can end all sessions for a user.
- Disabling a user, changing their role, or reaching a guest's `expires_at` **ends all of that user's Atlas sessions immediately**, and asks the IdP to end its sessions too where the IdP supports it.
- Logout deletes the session row and calls the IdP's end-session endpoint.
- Every authenticated request checks the session row, so revocation takes effect on the next request with no token-expiry wait. That closes audit A3.

**Rotation:**
- New session ID on login, on step-up, and on role change.

### 5. Rate limiting (three layers)

1. **Cloudflare edge:** rate-limit rules on the IdP's authentication endpoints and on Atlas `/api/auth/*`. Bot-fight / WAF managed rules stay on for both hostnames.
2. **nginx:** `limit_req` keyed by the real client IP. There's a strict zone for `/api/auth/*` and step-up callbacks (around 10 requests/min, small burst) and a general zone for `/api/*`. Upload size is capped with `client_max_body_size`, which comes from config; v1 has no uploads, so it starts small.
3. **App:** a per-account and per-IP counter in Postgres for sensitive actions (login callbacks, step-up, admin mutations, break-glass). It returns 429 with `Retry-After`. Credential brute-force lockout belongs to the IdP, which Kanidm does natively.

### 6. Cloudflare Access: in front, or not?

**Trade-off:**
- Access (a Zero-Trust login wall at the edge) adds a strong pre-authentication layer.
- It also breaks or degrades non-browser and API clients: Immich mobile, OIDC back-channel/discovery fetches, PWA background requests, and future Hermes and API tokens.
- It also forces a second login for family members.

**Decision:**

| Surface | Cloudflare Access? | Reason |
|---|---|---|
| Atlas user app and API (`<atlas-host>/*`) | **No** | Family UX; PWA requests; the app is already protected by IdP login + sessions |
| IdP user-facing endpoints (authorize, token, JWKS, discovery, login UI) | **No** | Immich mobile and Atlas server-to-server OIDC calls must reach them without an Access challenge |
| Immich (`<immich-host>`) | **No** | The mobile app can't pass Access. Protected by its own login via SSO |
| **Atlas admin console paths** (`/admin*`, `/api/v1/admin/*`) | **Yes (recommended)**, added in Phase 11 | Browser-only and used by 1–2 people. Policy: owner/admin identities only. Defense in depth if an app-layer bug ever exposes admin routes |
| **IdP admin UI/API paths** (if exposed at all) | **Yes**, or not routed through the tunnel at all | Keep IdP administration on-host via CLI or behind Access |

**Rules:**
- Atlas must be fully secure **without** Access. Access is an extra layer and is never relied on.
- No security decision in the app reads Access headers.

## Additional hardening (nginx and app)

**Response headers:**
- `Strict-Transport-Security` (set at the edge or in nginx).
- `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'`, plus connect-src limited to self.
- `X-Content-Type-Options: nosniff`.
- `Referrer-Policy: same-origin`.
- `Permissions-Policy` minimal.

**CORS:**
- Not needed: the SPA and API are same-origin.
- CORS is disabled in production; dev origins come from config (audit I2).

**Pagination:**
- Bounded everywhere (`limit ≤ 100`), so no endpoint can dump a table (audit I4).

**Errors:**
- Uniform error bodies with no exception class names (audit F7).

**`/health`:**
- Returns exactly `{"status":"ok"}` with no auth and no dependency details.
- A separate `/health/ready` checks DB and IdP reachability. nginx routes it only from loopback.

## Consequences

- One public hostname each for Atlas and the IdP (plus Immich's own), all through the same tunnel and nginx.
- Revocation is immediate, at the cost of one indexed session lookup per request. That's trivial at family scale.
- Admin flows need a re-login every 5 minutes of admin work. This is intentional, and the window is configurable.
- Without Access on user paths, the edge doesn't stop credential-stuffing at the door. The IdP's lockout, MFA requirements and edge rate rules cover it.

## Verification

- nginx config test in CI: `nginx -t`. A container test asserts that only `127.0.0.1:8081` is bound and that unknown `Host` is dropped.
- Session tests:
  - revoke → next request 401;
  - disable user → all sessions gone;
  - session ID rotates on login and step-up;
  - cookie flags asserted.
- CSRF test: an unsafe request without or with a wrong token gives 403.
- Rate-limit test: the N+1-th auth callback in a window gives 429.
- Headers test: CSP, nosniff and frame-ancestors are present on HTML and API responses.
- Docs disabled in production settings (a test asserts 404).

# ADR-004: Config and secrets: fail closed, secrets only from env files, generated on first start, nothing secret in compose or the repo

- **Status:** Proposed
- **Date:** 2026-10-29. Revised for generic self-hosting.
- **Related:** [AUDIT](../AUDIT.md) §3.1 A1, §3.5, §4, §2.3 (coupling 7), [SELF_HOSTING](../SELF_HOSTING.md)

## Context

The audit found:

- `SECRET_KEY` silently defaults to a public constant, and tokens forged with it are accepted (A1, verified).
- `docker-compose.yml` commits a literal `SECRET_KEY` and the MinIO root credentials.
- Config is read by `os.getenv` in four modules at import time, so tests can't isolate the DB.
- `STORAGE_BACKEND` vs `ATLAS_STORAGE_BACKEND` don't match.
- `.env` is missing from the root `.gitignore` and from `.dockerignore`.

Atlas is public and meant to be self-hosted by others. Configuration has to:

- be **fail-closed** (insecure configs don't start);
- keep **install to one command** (a newcomer shouldn't hand-craft keys);
- keep **every path, port and time zone configurable**, with generic defaults.

## Decision

### 1. One typed `Settings` object, built once and injected

- `atlas.kernel.config.Settings` is a `pydantic-settings` model built in `create_app(settings)`, passed through dependencies and the kernel container.
- Modules get validated sub-settings ([ARCHITECTURE](../ARCHITECTURE.md) §6).
- **No module calls `os.getenv`.** A ruff banned-api rule forbids `os.getenv`/`os.environ` outside `atlas/kernel/config.py`.
- Tests construct `Settings(...)` directly, so there's no global engine and no stray `./atlas.db`.
- Every variable is prefixed `ATLAS_`, except the conventional `SECRET_KEY`, `DATABASE_URL`, `TZ` and the Postgres image's own variables. One name per setting. The full list is in [SELF_HOSTING](../SELF_HOSTING.md#configuration-reference).
- Secret fields are `SecretStr`. They're never logged or shown in `repr`, errors or `/health/ready`, and never returned by any API.

### 2. Two kinds of configuration files, both untracked

| File (default path) | Contents | Read by |
|---|---|---|
| **`.env`** next to `docker-compose.yml` (Compose's standard project env file) | **Non-secret** deployment settings: bind address, port, time zone, public URLs, secrets dir path, compose profiles, image tag | Docker Compose (variable interpolation) |
| **`${ATLAS_SECRETS_DIR:-./secrets}/`** | **Secrets only**, one sub-directory per consumer (§3) | The containers, from read-only mounts |

- The repo ships `.env.example` (non-secret, generic defaults, `example.com` placeholders).
- The secrets directory is **never** in the repo. Both paths are in `.gitignore` and `.dockerignore`.

### 3. Secrets: generated on first start, read from env files, least privilege

- A one-shot **`atlas-init`** service runs first on every `docker compose up`. Every other service depends on it completing successfully.
- `atlas-init` creates any **missing** secret using a cryptographic RNG. It **never overwrites an existing file**, and is idempotent.
- It creates directories with mode `0700` and files with mode `0600`.
- Operators who want to supply their own values (e.g. from a password manager or their own secrets tooling) just create the files first, and `atlas-init` leaves them alone.
- **Ownership (amended in Phase 3b).** In Compose, `atlas-init` is the **only service that runs as root**. It's a one-shot with no network, and it uses root only to set ownership on what it writes (`atlas-admin init --chown`), so each container can read exactly its own secrets:

  | Path | Owner : group | Mode | Read by |
  |---|---|---|---|
  | `atlas/` and its files | 10001 : 10001 (the Atlas image's user) | 0700 / 0600 | `atlas-api`, `atlas-migrate`, `atlas-db-init` |
  | `postgres/` / `postgres/password` | 999 (the Postgres image's user) : 10001 | 0750 / 0640 | `postgres`, and `atlas-db-init` through the group |
  | the backups dir | 10001 : 10001 | 0700 | `atlas-migrate`, `atlas-backup` |

  - The group read on `postgres/` exists for `atlas-db-init`, the one step that needs the superuser password ([ADR-005](005-database-and-migrations.md) §3).
  - Least privilege is enforced by **mounts**: `atlas-api` mounts only `atlas/`, so it can't read the Postgres password even though it runs as uid 10001.
  - The uids are flags (`--app-uid`, `--app-gid`, `--postgres-uid`). Without `--chown`, for example when run by hand on the host, `init` never changes an owner.
  - `--chown` sets owner and mode on existing files too. It never changes their contents.

| Path under `ATLAS_SECRETS_DIR` | Contents (generated if missing) | Mounted read-only into |
|---|---|---|
| `atlas/app.env` | `SECRET_KEY` (32 random bytes, base64url), `DATABASE_URL` (with the generated `atlas_app` password), `ATLAS_MIGRATOR_DATABASE_URL`, `ATLAS_OIDC_CLIENT_SECRET`, `ATLAS_IDP_ADMIN_TOKEN` (bundled IdP only, written after IdP bootstrap) | `atlas-api`, `atlas-migrate` |
| `atlas/connectors.env` | Connector secrets the admin adds by hand (e.g. `ATLAS_CONNECTOR_IMMICH_API_KEY`). Created empty | `atlas-api` |
| `postgres/password` | The Postgres superuser password (consumed via the image's `POSTGRES_PASSWORD_FILE`) | `postgres` (and `atlas-init` for role setup) |
| `idp/` | Bundled Kanidm: internal CA, its key and the server certificate/key ([ADR-001](001-identity.md)) | `idp`, `atlas-proxy` (CA cert only), `atlas-api` (CA cert only) |
| `cloudflared/tunnel.env` | Optional `TUNNEL_TOKEN` for the bundled Cloudflare Tunnel connector. **Created by the operator, never generated** | `cloudflared` (profile `tunnel`) |

**The app loads its own env files.**
- Atlas reads `ATLAS_ENV_FILES` (default `/run/secrets/atlas/app.env,/run/secrets/atlas/connectors.env`) at startup, through `pydantic-settings`.
- So the API sees secrets that `atlas-init` created moments earlier in the same `up`. Compose-level `env_file:` would be read before `atlas-init` runs.
- Real environment variables override file values, for operators who inject secrets differently, e.g. via an orchestrator.

**Least privilege per container.** Postgres never sees `SECRET_KEY`, and the API never sees the Postgres superuser password. The app connects as `atlas_app` (DML only) and migrations run as `atlas_migrator` ([ADR-005](005-database-and-migrations.md)).

**Repo and compose contain no secret values and no site details.** Compose uses `${VAR:-default}` interpolation only for non-secret settings, and mounts the secrets subdirectories. A CI linter rejects any `environment:` key matching `(?i)(secret|password|token|key)` that has a literal value.

**Connector secrets are not stored in the DB in Foundation.** The console stores only non-secret connector config plus `secret_ref` names, and shows each as "set" or "missing". If console-entered secrets are ever needed, they'll be encrypted at rest with a key derived from `SECRET_KEY` (HKDF with a purpose label), under a new ADR.

### 4. Fail closed at startup

The process exits non-zero **before binding a socket** if any check fails. The message names the variable, never its value.

| Check | Rule |
|---|---|
| `SECRET_KEY` present | Required. There's no default anywhere in code (`atlas-init` *writes* one to the file; the app never invents one) |
| `SECRET_KEY` strong | ≥ 32 bytes of entropy: at least 43 base64url chars or 64 hex chars. Rejects keys with fewer than 16 distinct characters |
| `SECRET_KEY` not a known placeholder | Denylist containing every placeholder ever committed to this repo (`dev-secret-change-in-production`, `change-this-in-production`), plus `changeme`, `secret`, `password` and anything containing `change` + `production` |
| `DATABASE_URL` | Required. `postgresql+psycopg://…` in production. Password not on the denylist (includes `atlas`, `postgres`, `password`) |
| `ATLAS_PUBLIC_URL`, `ATLAS_IDP_PUBLIC_URL` / `ATLAS_OIDC_ISSUER` | Required. Must be `https://` in production and must not contain `example.com`/`example.org`, so the placeholder from `.env.example` can't be shipped |
| OIDC client | Client ID and client secret required. Secret not denylisted |
| `ATLAS_ENV` | `production` (**default**), `development` or `test`. Forgetting it gives strict mode |
| Production-only | Docs disabled, CORS off, `Secure` cookies, HTTPS issuer |
| Connector secrets | An enabled connector with an unresolved `secret_ref` is marked `misconfigured` (the app still starts) |

- No auto-generated fallback key exists inside the app, in any mode. Development uses `atlas-init` too (or `make dev-secrets`).
- `test` mode is only for `Settings` constructed in tests.

### 5. Repo hygiene (prevents re-leaks)

- `.gitignore` / `.dockerignore`: `.env`, `.env.*`, `!.env.example`, `*.env`, `secrets/`, `backups/`.
- **gitleaks** runs in CI on every push and on the full history weekly, and as an optional pre-commit hook.
- The compose-secret linter (§3) runs in CI.
- `.env.example` uses only RFC 2606 placeholder domains (`example.com`) and generic defaults.

### 6. Rotation

| Secret | Effect | Procedure |
|---|---|---|
| `SECRET_KEY` | All sessions and CSRF tokens become invalid, so everyone logs in again | Delete the line or file, re-run `docker compose up -d` (`atlas-init` regenerates it), or edit it manually. Optional `SECRET_KEY_PREVIOUS` is accepted for verification for a configurable window |
| DB passwords | — | `atlas-admin rotate-db-password` (`ALTER ROLE` + rewrite `app.env`), then restart |
| OIDC client secret / IdP admin token | — | `atlas-admin idp-rotate-client` (bundled IdP), or rotate in your IdP, update `app.env` and restart |
| Internal IdP certificate | — | `docker compose run --rm atlas-init --rotate-idp-cert`, then restart the proxy and IdP |

**Historic placeholders:** any deployment that ever ran with one of the placeholder `SECRET_KEY`s must treat it as compromised. The denylist makes those keys unusable from now on.

## Consequences

- One-command install with strong secrets by default. Advanced operators can supply their own.
- Misconfiguration is loud and immediate rather than silently insecure.
- Secrets live on the host's disk under `ATLAS_SECRETS_DIR` with strict permissions. Protecting that host (disk encryption, backups of the secrets dir) is the operator's responsibility, and SELF_HOSTING says so.

## Verification

- `Settings` unit tests:
  - missing, short, low-variety or denylisted `SECRET_KEY` each fail startup;
  - the value never appears in the error;
  - production is the default;
  - placeholder `example.com` URLs are rejected in production;
  - env vars override file values.
- `atlas-init` tests:
  - generates every file with the right modes;
  - is idempotent;
  - never overwrites;
  - uses an RNG (no constants).
- CI: no `os.getenv` outside `config.py`; gitleaks; compose-secret linter; `docker compose config` renders from `.env.example`.
- Container smoke test: a placeholder `SECRET_KEY` gives exit ≠ 0 and `/health` unreachable.

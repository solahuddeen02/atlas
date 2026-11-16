# Changelog

All notable changes to Atlas are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Atlas follows [Semantic Versioning](https://semver.org/) (see [README → Versioning](README.md#versioning)):

- **0.x may break anything.** Any 0.x release can contain breaking changes, so always read its upgrade notes.
- **1.0.0 completes the Foundation milestone** (PLAN Phase 10).
- After 1.0.0, a breaking change is a MAJOR release, new functionality (including a new module) is MINOR, and a fix is PATCH.

**Every release section has an "Upgrade notes" subsection.** It lists manual steps, new or renamed settings, and migrations that need action, or says "None". Read it before you upgrade ([SELF_HOSTING → Upgrading](docs/SELF_HOSTING.md#upgrading)).

## [Unreleased]

There are no releases yet. This section covers PLAN Phases 0–4, Spike S, Phase 5a (the bundled identity provider's infrastructure) and its follow-ups (5b-0).

### Added

- **Repository and CI baseline** (Phase 1):
  - the new platform's layout (`atlas/`, `modules/`, `web/`, `deploy/`), with the frozen app moved to `legacy/`;
  - uv with a lockfile, ruff, `mypy --strict` and import-linter module boundaries;
  - required CI checks (see [CONTRIBUTING](CONTRIBUTING.md#checks-ci-runs)), including gitleaks, dependency audits and a linter that rejects literal secrets in compose files;
  - grouped weekly Dependabot updates.
- **Fail-closed configuration and generated secrets** (Phase 2, [ADR-004](docs/adr/004-config-and-secrets.md)):
  - one validated `Settings` object; weak, placeholder or missing values stop the app from starting, and error messages never print values;
  - `atlas-admin init` generates missing secrets with strict file modes and never overwrites an existing value;
  - `create_app()`, `/health`, and `deploy/.env.example` with generic defaults.
- **PostgreSQL and migrations** (Phase 3, [ADR-005](docs/adr/005-database-and-migrations.md)):
  - the kernel schema baseline, with an app role that can't change the schema and can only append to the audit log;
  - `atlas-admin db-init`, `migrate` (with a pre-migration dump), `backup` and `restore`, with backup retention;
  - the compose stack, whose external database volume survives `docker compose down -v`.
- **Deploy skeleton** (Phase 4, [ADR-003](docs/adr/003-internet-exposure.md)):
  - `atlas-proxy`, the only published port, on loopback by default;
  - unknown hosts dropped, security headers, rate and body-size limits, and fail-closed client-IP trust;
  - `/health/ready` for trusted peers only;
  - multi-stage, multi-arch (amd64/arm64), non-root images, and `deploy/install.sh`.
- **Bundled identity provider: infrastructure** (Phase 5a, [ADR-001](docs/adr/001-identity.md)):
  - the `idp` service (Kanidm 1.12.0, pinned by digest) under the `bundled-idp` profile, on by default. It runs as uid 10002 with a read-only root filesystem, and its healthcheck verifies TLS;
  - `atlas-idp-init`, which writes an internal CA, the IdP's TLS certificate and Kanidm's `server.toml`. The CA key is mounted into no running service. `--rotate-idp-cert` issues a new IdP certificate from the same CA;
  - internal networks `idp-front` (atlas-proxy + idp; the only source Kanidm trusts `X-Forwarded-For` from) and `idp-back` (atlas-api + idp, for Phase 5b);
  - an IdP virtual host in atlas-proxy for `ATLAS_IDP_PUBLIC_URL`: TLS re-encryption verified against the internal CA, the client address overwritten into `X-Forwarded-For`, and per-IP rate limits on Kanidm's credential endpoints;
  - `atlas-backup` copies Kanidm's newest online backup into each backup set. `atlas-admin restore` checks the Kanidm version and prints the offline IdP restore steps;
  - settings `ATLAS_IDP_PUBLIC_URL`, `ATLAS_IDP_FRONT_SUBNET`, `ATLAS_IDP_BACKUP_SCHEDULE`, `ATLAS_IDP_BACKUP_VERSIONS`, `ATLAS_RATE_LIMIT_IDP_AUTH` and `ATLAS_IDENTITY_ADMIN`. The OIDC issuer defaults from the IdP URL;
  - not yet: the IdP bootstrap (OAuth2 clients, service account, groups) and login (Phase 5b). Until then `ATLAS_OIDC_CLIENT_SECRET` is still set by hand.
- **Kanidm client library** (Phase 5b-1, [ADR-001](docs/adr/001-identity.md)), `atlas.kernel.identity.kanidm`:
  - REST calls to the bundled IdP, always verified against its internal CA: login for the built-in service accounts, service accounts and API tokens, groups, OAuth2 clients;
  - the admin unix socket (`RecoverAccount`, `DisableAccount`);
  - typed errors, and no credential in a log, an error message or a `repr`;
  - no service uses it yet: the IdP bootstrap (5b-2) is its first caller;
  - for the bootstrap (5b-2a), checked against the real IdP: redirect URLs with an app's own scheme (`app.immich:///oauth-callback`; `http://` is still refused), OAuth2 client settings (landing URL, strict redirects, PKCE, legacy crypto) in one idempotent call, deleting an API token, and groups and service accounts by UUID. An OAuth2 client is still addressed by name only: Kanidm answers a UUID there with `200 null`, a 404 or a silent no-op.
- **Kanidm's admin socket on its own volume** (Phase 5b-2a): `server.toml` gains `adminbindpath`, and the socket lives on a new volume, `idp-admin`, that only uid 10002 can use. The IdP bootstrap (5b-2b) can then reach the socket without mounting Kanidm's database. Nothing uses the socket yet.
- `SECURITY.md` (private vulnerability reporting) and this changelog.
- Spike S recorded: Kanidm 1.12.0 confirmed as bundled IdP ([ADR-001](docs/adr/001-identity.md#spike-results) outcome (b)).

### Changed

- The pre-redesign Drive/Photos app now lives in `legacy/` and gets security fixes only ([legacy/README.md](legacy/README.md)).
- Milestones are named Foundation, First modules and Household apps instead of v1/v2/v3, and releases follow SemVer.

- `atlas-init` now creates `secrets/idp-trust/` itself (root, `0755`) in both modes, because atlas-proxy always mounts it. Before, in bring-your-own OIDC mode the directory appeared only as a side effect of that mount. Without the bundled IdP it stays empty.
- `httpx` is now a runtime dependency of the Atlas image (the Kanidm client uses it). It was a development dependency before.
- The check that the pinned Kanidm digest still matches its tag moved out of the `compose` CI job into its own workflow (`kanidm-digest`). It runs when `deploy/docker-compose.yml` changes, weekly and on demand, retries registry errors, and is not a required check, so a Docker Hub rate limit no longer fails unrelated pull requests.

### Fixed

- `legacy/README.md` now describes the legacy app accurately:
  - ownership returns 404, and only since Phase 0;
  - the storage setting is `STORAGE_BACKEND`;
  - the database is SQLite only.

### Security

- **Legacy app** (Phase 0):
  - refuses to start without a strong `SECRET_KEY`, and rejects the placeholder keys that were once committed;
  - no secrets in its compose file;
  - another user's objects, and referenced parent folders, return 404;
  - moves can't create cycles;
  - permanent delete works only on trashed objects;
  - responses don't expose storage paths;
  - uploads are size-capped while streaming;
  - refreshed frontend lockfile, clearing high-severity npm advisories.
- **Bundled IdP: the host can no longer connect to Kanidm on `idp-front`** (5b-0). The network's gateway address belonged to the host and lay inside the subnet Kanidm trusts for `X-Forwarded-For`, so a program on the host could connect to Kanidm directly and name any client address. `idp-front` now has the bridge option `com.docker.network.bridge.inhibit_ipv4`, so the host has no address on it. Existing installs must recreate the network: see the upgrade notes.
- **New platform:** see the Phase 2–4 entries above. In short:
  - no secret has a default;
  - the API never sees the database superuser password;
  - every container runs as non-root with only its own secrets mounted;
  - the proxy trusts a forwarded client IP only from loopback, from the Docker gateway on a loopback bind, or from explicitly listed peers.

### Upgrade notes

- **New platform:** nothing to upgrade from, since nothing has been released yet. For a first install, follow [SELF_HOSTING](docs/SELF_HOSTING.md). Parts of it, such as login, don't work yet.
- **If you run a pre-release checkout from before Phase 5a**, the bundled IdP is now on by default:
  - **New settings** in `deploy/.env` (see `.env.example`): `COMPOSE_PROFILES=bundled-idp`, `ATLAS_IDP_PUBLIC_URL` (its own HTTPS hostname, different from `ATLAS_PUBLIC_URL`), `ATLAS_IDP_VOLUME`, `ATLAS_IDP_FRONT_SUBNET` (default `10.249.73.0/28`; change it if it collides with one of your networks), `ATLAS_IDP_BACKUP_SCHEDULE`, `ATLAS_IDP_BACKUP_VERSIONS` and `ATLAS_RATE_LIMIT_IDP_AUTH`. To stay on bring-your-own OIDC, leave out `bundled-idp` and `ATLAS_IDP_PUBLIC_URL`;
  - **new volume:** run `./install.sh` (or `docker volume create atlas_idp`) before `docker compose up -d`;
  - **new files:** `atlas-idp-init` creates `secrets/idp-ca/` (the internal CA: back it up with the other secrets), `secrets/idp/`, `secrets/idp-trust/`, `backups/idp-online/` and `backups/idp-restore/`;
  - `ATLAS_OIDC_CLIENT_SECRET` must still be set by hand until Phase 5b.
- **If you run a pre-release checkout from before the admin-socket volume** (5b-2a), the bundled IdP's admin socket moves to a new volume. Run `./install.sh` as usual (or `docker compose up -d`); in `deploy/`:

  ```bash
  ./install.sh
  ```

  - Compose creates the volume `atlas_idp-admin` (`<project>_idp-admin`). It holds only the socket, so it needs no backup and no setting; `install.sh` does not need to create it.
  - `atlas-idp-init` rewrites `secrets/idp/server.toml` with the new `adminbindpath` line and gives the new volume to uid 10002.
  - The `idp` service restarts once to move its socket. Expect a short IdP outage; the data volume is not touched.
  - A stale `kanidmd.sock` file stays in the `atlas_idp` volume. It is harmless and can stay.
  - Bring-your-own OIDC: nothing changes.
- **If you run a pre-release checkout from Phase 5a** (before 5b-0), recreate the `idp-front` network once so that it gets its new option. This applies in bring-your-own OIDC mode too. No setting changes, and the data volumes are not touched. In `deploy/`:

  ```bash
  docker compose down
  ```

  ```bash
  ./install.sh
  ```

  Never add `-v` to `down`. Then check that this prints `true` (replace `atlas` if you changed the project name):

  ```bash
  docker network inspect atlas_idp-front --format '{{index .Options "com.docker.network.bridge.inhibit_ipv4"}}'
  ```

  Recent Docker Compose versions recreate the network on `docker compose up -d` alone; the two commands above work with every version.
- **Legacy app:**
  - it now runs from `legacy/` (`cd legacy` before any command);
  - `SECRET_KEY` must be set to a strong value, or the app won't start; tokens signed with an old placeholder key no longer work;
  - the MinIO credentials come from `legacy/.env`.

[Unreleased]: https://github.com/solahuddeen02/atlas/commits/main

<!--
Template for a release. Copy it above the previous release, fill it in, and delete
empty subsections (except "Upgrade notes", which every release keeps).

## [X.Y.Z] - YYYY-MM-DD

### Added

- 

### Changed

- 

### Deprecated

- 

### Removed

- 

### Fixed

- 

### Security

- 

### Upgrade notes

- None.
  (Or list: manual steps; new, renamed or removed settings; migrations that need
  action, such as a long-running data migration or a required backup first.)

[X.Y.Z]: https://github.com/solahuddeen02/atlas/releases/tag/vX.Y.Z
-->

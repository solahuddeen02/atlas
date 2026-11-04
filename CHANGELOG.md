# Changelog

All notable changes to Atlas are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Atlas follows [Semantic Versioning](https://semver.org/) (see [README → Versioning](README.md#versioning)):

- **0.x may break anything.** Any 0.x release can contain breaking changes, so always read its upgrade notes.
- **1.0.0 completes the Foundation milestone** (PLAN Phase 10).
- After 1.0.0, a breaking change is a MAJOR release, new functionality (including a new module) is MINOR, and a fix is PATCH.

**Every release section has an "Upgrade notes" subsection.** It lists manual steps, new or renamed settings, and migrations that need action, or says "None". Read it before you upgrade ([SELF_HOSTING → Upgrading](docs/SELF_HOSTING.md#upgrading)).

## [Unreleased]

There are no releases yet. This section covers PLAN Phases 0–4, Spike S and Phase 5a (the bundled identity provider's infrastructure).

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
- `SECURITY.md` (private vulnerability reporting) and this changelog.
- Spike S recorded: Kanidm 1.12.0 confirmed as bundled IdP ([ADR-001](docs/adr/001-identity.md#spike-results) outcome (b)).

### Changed

- The pre-redesign Drive/Photos app now lives in `legacy/` and gets security fixes only ([legacy/README.md](legacy/README.md)).
- Milestones are named Foundation, First modules and Household apps instead of v1/v2/v3, and releases follow SemVer.

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

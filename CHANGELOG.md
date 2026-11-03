# Changelog

All notable changes to Atlas are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Atlas follows [Semantic Versioning](https://semver.org/) (see [README → Versioning](README.md#versioning)):

- **0.x may break anything.** Any 0.x release can contain breaking changes, so always read its upgrade notes.
- **1.0.0 completes the Foundation milestone** (PLAN Phase 10).
- After 1.0.0, a breaking change is a MAJOR release, new functionality (including a new module) is MINOR, and a fix is PATCH.

**Every release section has an "Upgrade notes" subsection.** It lists manual steps, new or renamed settings, and migrations that need action, or says "None". Read it before you upgrade ([SELF_HOSTING → Upgrading](docs/SELF_HOSTING.md#upgrading)).

## [Unreleased]

There are no releases yet. This section covers PLAN Phases 0–4 and Spike S. The parts of Phases 2–4 that depend on the bundled identity provider are scheduled for Phase 5a.

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

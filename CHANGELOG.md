# Changelog

All notable changes to Atlas are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Atlas follows [Semantic Versioning](https://semver.org/) (see [README → Versioning](README.md#versioning)):

- **0.x may break anything.** Any 0.x release can contain breaking changes, so always read its upgrade notes.
- **1.0.0 completes the Foundation milestone** (PLAN Phase 10).
- After 1.0.0, a breaking change is a MAJOR release, new functionality (including a new module) is MINOR, and a fix is PATCH.

**Every release section has an "Upgrade notes" subsection.** It lists manual steps, new or renamed settings, and migrations that need action, or says "None". Read it before you upgrade ([SELF_HOSTING → Upgrading](docs/SELF_HOSTING.md#upgrading)).

## [Unreleased]

There are no releases yet. This section covers PLAN Phases 0–4, Spike S, Phase 5a (the bundled identity provider's infrastructure) and its follow-ups (5b-0 to 5b-3b: the Kanidm client library, the IdP bootstrap, the console's least privilege and the identity adapter).

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
  - the IdP bootstrap (OAuth2 clients, service account, groups) came with Phase 5b-2b, below; login follows in Phase 5c.
- **Kanidm client library** (Phase 5b-1, [ADR-001](docs/adr/001-identity.md)), `atlas.kernel.identity.kanidm`:
  - REST calls to the bundled IdP, always verified against its internal CA: login for the built-in service accounts, service accounts and API tokens, groups, OAuth2 clients;
  - the admin unix socket (`RecoverAccount`, `DisableAccount`);
  - typed errors, and no credential in a log, an error message or a `repr`;
  - no service uses it yet: the IdP bootstrap (5b-2) is its first caller;
  - for the bootstrap (5b-2a), checked against the real IdP: redirect URLs with an app's own scheme (`app.immich:///oauth-callback`; `http://` is still refused), OAuth2 client settings (landing URL, strict redirects, PKCE, legacy crypto) in one idempotent call, deleting an API token, and groups and service accounts by UUID. An OAuth2 client is still addressed by name only: Kanidm answers a UUID there with `200 null`, a 404 or a silent no-op.
- **Kanidm's admin socket on its own volume** (Phase 5b-2a): `server.toml` gains `adminbindpath`, and the socket lives on a new volume, `idp-admin`, that only uid 10002 can use. The IdP bootstrap (5b-2b) can then reach the socket without mounting Kanidm's database. Nothing uses the socket yet.
- **The IdP bootstrap** (Phase 5b-2b, [ADR-001](docs/adr/001-identity.md)): the one-shot service `atlas-idp-bootstrap` and the command `atlas-admin idp-bootstrap`. On every `up`, after `idp` is healthy and before `atlas-api`, it sets Kanidm up for Atlas, idempotently ([SELF_HOSTING → The IdP bootstrap](docs/SELF_HOSTING.md#the-idp-bootstrap)):
  - the groups `atlas_users`, `atlas_role_owner`, `atlas_role_admin`, `atlas_role_adult`, `atlas_role_child`, `atlas_role_guest` (and `immich_users` with Immich) and their credential policies (owner and admin: `mfa` and 8-hour sessions; adult: `mfa`; child and guest: `any`). `idm_all_persons` is lowered to `any` only after every role group carries its policy;
  - the service account `atlas-console` with one API token, and Atlas's OIDC client `atlas` (landing URL = the OIDC callback and its only redirect URL, strict redirects, PKCE required, legacy crypto off);
  - with the new setting `ATLAS_IMMICH_PUBLIC_URL`, Immich's client `immich` with exactly the three documented redirect URIs, whose secret you read with `sudo cat secrets/idp-clients/immich.env`;
  - `secrets/atlas-idp/idp.env` (`ATLAS_OIDC_CLIENT_SECRET`, `ATLAS_IDP_ADMIN_TOKEN`), `groups.json` and `bootstrap.json`, all written atomically with their final mode, and loaded last through `ATLAS_ENV_FILES`. Nothing secret is ever set by hand, logged or printed;
  - it runs as uid 10002 with no capabilities and a read-only root filesystem, on `idp-back` only, with Kanidm's admin socket volume mounted read-only: never the IdP's database, never the Docker socket;
  - `idm_admin` is disabled again on every way out, including errors and SIGTERM, and on the fast path (a run with nothing to change, which resets no password and does not sign in);
  - `atlas-admin idp-bootstrap --verify` (a full run that repairs drift, and removes scope maps nobody here set from the two clients) and `--rotate-console-token`;
  - `atlas-api` does not start until a bootstrap has succeeded, every time: each run writes `complete: false` into `bootstrap.json` first and `complete: true` last, and `atlas-api` refuses to start on anything else (the new setting `ATLAS_IDP_BOOTSTRAP_RECORD`). Compose can't do this alone: a dependency declared optional only warns when it fails;
  - settings `ATLAS_IMMICH_PUBLIC_URL`, `ATLAS_IDP_ADMIN_TOKEN` (optional until the console uses it) and `ATLAS_IDP_BOOTSTRAP_RECORD`; the client's removal of one OAuth2 scope map and of a redirect URL of any scheme (to remove an `http://` one set by hand).
- **The console's least privilege** (Phase 5b-3a, [ADR-001](docs/adr/001-identity.md)): bootstrap version 2. The console's service account no longer belongs to `idm_group_admins`, whose members can change every group that is not high-privilege (even delete `atlas_users`). It manages the five role groups and `immich_users` through `entry_managed_by` instead, and nothing else ([SELF_HOSTING → The console's limited rights](docs/SELF_HOSTING.md#the-consoles-limited-rights)).
  - the bootstrap sets the manager on all six groups, reads each one back, and only then removes the console from `idm_group_admins`; a failure in between fails the run and the next run resumes;
  - `atlas-admin idp-bootstrap --verify` also removes members of the six groups that are not persons (it logs the group, the name and the UUID);
  - a compose test pins what the token can do and what it must be refused, against the real Kanidm, with an `idm_admin` snapshot around every refused write;
  - the Kanidm client gains `remove_group_members`;
  - `atlas-api` runs as one process on purpose, with a test that fails if more are configured (role changes need a cross-process lock first).
- **The identity adapter** (Phase 5b-3b, [ADR-001](docs/adr/001-identity.md)): `IdentityAdmin`, the interface the admin console will use to manage the people of a household, with the `kanidm` adapter (the bundled IdP) and the `fake` adapter (for tests). Nothing calls it yet: the console is Phase 8.
  - `create_person`, `get_person`, `set_role`, `set_app_access`, `issue_credential_reset`, `disable`, `enable`, `delete` and `check`. A person is addressed by the IdP's id (the OIDC `sub`), never by username;
  - **every person is in exactly one role group**, found by UUID from `groups.json` (a group that only has the same name never counts). A person in none or several is refused by every method but `set_role(..., repair=True)` and `delete`;
  - `set_role` adds the new group before it removes the old one, serialised per person, undone once on a failure and otherwise a person that is refused until repaired;
  - **reset links** use `ATLAS_IDP_PUBLIC_URL`, show nothing of their token in a log, an error or a `repr`, live 15 minutes by default and 5 minutes to 1 hour at most (not clamped), and are refused for a disabled person. A link cannot be revoked and a new one does not cancel an older one ([SELF_HOSTING → Reset links](docs/SELF_HOSTING.md#reset-links));
  - `check()` reports a missing group, a refused token and any member of the six groups that is not a person (name and UUID);
  - usernames are 2 to 32 lowercase letters, digits, `_` and `-` (reserved names refused); display names take Thai and any other script;
  - the Kanidm client gains the person calls (the person's attributes are allowlisted, no credential among them);
  - settings `ATLAS_IDP_INTERNAL_URL`, `ATLAS_IDP_CA_FILE` and `ATLAS_IDP_GROUPS_FILE` (defaults match the compose file); `ATLAS_IDP_ADMIN_TOKEN` is now **required** with the bundled IdP;
  - `atlas-api` mounts `secrets/idp-trust/` (the CA certificate, public) read-only;
  - tests: one contract suite run against both adapters, and a compose test of a whole lifecycle against the real Kanidm that also searches every service's log for each secret it handled.
- `SECURITY.md` (private vulnerability reporting) and this changelog.
- Spike S recorded: Kanidm 1.12.0 confirmed as bundled IdP ([ADR-001](docs/adr/001-identity.md#spike-results) outcome (b)).

### Changed

- The pre-redesign Drive/Photos app now lives in `legacy/` and gets security fixes only ([legacy/README.md](legacy/README.md)).
- Milestones are named Foundation, First modules and Household apps instead of v1/v2/v3, and releases follow SemVer.

- `atlas-init` now creates `secrets/idp-trust/` itself (root, `0755`) in both modes, because atlas-proxy always mounts it. Before, in bring-your-own OIDC mode the directory appeared only as a side effect of that mount. Without the bundled IdP it stays empty.
- `atlas-init` now also creates `secrets/atlas-idp/` (root, group 10001, `0750`) in both modes, because `atlas-api` always mounts it. With the bundled IdP, `atlas-idp-init` gives it to uid 10002 (`2750`) and creates `secrets/idp-clients/` (`0700`). In bring-your-own OIDC mode `atlas-idp/` stays empty.
- With the bundled IdP, `ATLAS_ENV_FILES` for `atlas-api` ends with `/run/secrets/atlas-idp/idp.env`, so its client secret wins over one in `app.env`.
- `httpx` is now a runtime dependency of the Atlas image (the Kanidm client uses it). It was a development dependency before.
- The check that the pinned Kanidm digest still matches its tag moved out of the `compose` CI job into its own workflow (`kanidm-digest`). It runs when `deploy/docker-compose.yml` changes, weekly and on demand, retries registry errors, and is not a required check, so a Docker Hub rate limit no longer fails unrelated pull requests.
- `atlas-api` and `atlas-proxy` are probed every second while they start (`start_period: 30s`, `start_interval: 1s`), so `docker compose up -d` reports a healthy stack about 8 seconds sooner. Their intervals, timeouts and retries are unchanged. This needs Docker Engine 25.0 or newer and Compose 2.20.2 or newer, which the `idp` service already needed.

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
  - `ATLAS_OIDC_CLIENT_SECRET` is written by the IdP bootstrap from Phase 5b-2b (see the next note).
- **Faster start:** no action needed beyond `docker compose up -d`. Docker Engine 25.0 or newer and Compose 2.20.2 or newer are required (the bundled IdP already needed them; they are now stated in [SELF_HOSTING](docs/SELF_HOSTING.md#requirements)).
- **If you run a pre-release checkout from before the identity adapter** (5b-3b), run `docker compose up -d` as usual:
  - **`atlas-api` is recreated** with one new read-only mount, `secrets/idp-trust/` (the IdP's public CA certificate). The directory already exists; nothing to create.
  - **`ATLAS_IDP_ADMIN_TOKEN` is now required** with the bundled IdP. An install that has run the IdP bootstrap already has it in `secrets/atlas-idp/idp.env`; nothing to do. Without it `atlas-api` refuses to start and says which variable.
  - **New optional settings** with defaults that match the compose file: `ATLAS_IDP_INTERNAL_URL`, `ATLAS_IDP_CA_FILE`, `ATLAS_IDP_GROUPS_FILE`. Leave them alone.
  - **Bring-your-own OIDC: nothing changes.**
  - **Reset links** (when the console exists): short-lived, not revocable, and Kanidm's own log records one when it is opened ([SELF_HOSTING](docs/SELF_HOSTING.md#reset-links)).
- **If you run a pre-release checkout from before the console's least privilege** (5b-3a), run `docker compose up -d` as usual:
  - **The IdP bootstrap runs once more** (a few seconds; its version is now 2) and `atlas-api` waits for it. It makes the console's service account the manager of the five role groups and `immich_users`, checks each one, and only then removes the account from `idm_group_admins`. If it stops in between, the next `up` finishes the job.
  - **Nothing to change by hand**, no new setting and no new file.
  - **After the upgrade** the console's token can no longer change any other group, create or delete groups, or hand a group over; run `docker compose run --rm atlas-idp-bootstrap atlas-admin idp-bootstrap --verify` to also remove anything that is not a person from the six groups.
  - **Disabling a person in Kanidm:** clearing the expiry makes their old sessions valid again for up to their lifetime; changing the credential ends them for good ([SELF_HOSTING](docs/SELF_HOSTING.md#the-consoles-limited-rights)).
- **If you run a pre-release checkout from before the IdP bootstrap** (5b-2b), the bundled IdP is now configured for you. Run `./install.sh` as usual (or `docker compose up -d`):
  - **A new one-shot service, `atlas-idp-bootstrap`,** creates Atlas's OIDC client, the console's service account, the role groups and their credential policies, and writes `secrets/atlas-idp/idp.env`, `groups.json` and `bootstrap.json`. **`atlas-api` does not start until it has succeeded**, on this and on every later start. If it fails, read `docker compose logs atlas-idp-bootstrap`, fix the cause and run `docker compose up -d` again.
  - **New files and directories**, created by `atlas-init` and `atlas-idp-init`: `secrets/atlas-idp/` (also in bring-your-own mode, empty there) and `secrets/idp-clients/`. Back them up with the rest of `secrets/`.
  - **If you set `ATLAS_OIDC_CLIENT_SECRET` by hand** in `secrets/atlas/app.env`, the generated value now takes its place (`idp.env` is loaded last). You may delete your line.
  - **A client named `atlas` or `immich` that you created by hand in Kanidm is adopted:** the bootstrap enforces its settings (landing URL, strict redirects, PKCE, no legacy crypto), **removes every redirect URI other than the documented ones**, and reads its secret. Scope maps it did not set are removed only by `atlas-admin idp-bootstrap --verify`. Any other client is left alone. If a hand-made client relied on other redirect URIs, they are gone after the bootstrap, and adding them again is undone by its next run.
  - **Children and guests sign in with a password of at least 15 characters** (Kanidm's rule for an account without a second factor; a passphrase or a passkey satisfies it). Owners, admins and adults need a second factor.
  - **To connect Immich**, set `ATLAS_IMMICH_PUBLIC_URL` (Immich's own HTTPS origin, no path) in `deploy/.env`, run `docker compose up -d`, and read its client secret with `sudo cat secrets/idp-clients/immich.env`. This replaces the unimplemented `idp-add-client` command that earlier documentation named.
  - **To rotate the console's token:** `docker compose run --rm atlas-idp-bootstrap atlas-admin idp-bootstrap --rotate-console-token`, then `docker compose restart atlas-api`. Kanidm keeps accepting the old token for up to five minutes.
  - **Bring-your-own OIDC: nothing changes.** No bootstrap runs, `atlas-api` skips the check, and `ATLAS_OIDC_CLIENT_SECRET` stays in `app.env` as before.
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

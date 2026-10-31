# Atlas

[![CI](https://github.com/solahuddeen02/atlas/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/solahuddeen02/atlas/actions/workflows/ci.yml)

**Atlas is a self-hosted home platform for one household:** a single secure front door that knows who each family member is, decides what they may use, and hosts or connects the apps the household runs ([VISION](docs/VISION.md)).

> **Status: pre-1.0, not usable yet.** The foundation is being built phase by phase ([PLAN](docs/PLAN-v1.md)). There is no login yet, so there's nothing for a household to use. The previous Drive/Photos app is frozen in [`legacy/`](legacy/README.md).

## Status

The phases of the Foundation plan ([PLAN](docs/PLAN-v1.md)), in order:

| # | Phase | Status |
|---|---|---|
| 0 | Emergency security patches on the legacy app | Done |
| **S** | **Identity spike: validate Kanidm as the bundled IdP** | **Next** |
| 1 | Repo hygiene & CI baseline | Done |
| 2 | Config & secrets | Done (IdP-agnostic part)¹ |
| 3 | Database & migrations | Done (IdP-agnostic part)¹ |
| 4 | Deploy skeleton (proxy, images, one-command install) | Done (IdP-agnostic part)¹ |
| 5 | Identity & sessions | Planned |
| 6 | Authorization core | Planned |
| 7 | Module registry | Planned |
| 8 | Admin console | Planned |
| 9 | Connectors, Immich, events | Planned |
| 10 | Hardening, docs & release **1.0.0** | Planned |

¹ Phases 1–4 went ahead before the spike because they don't depend on which identity provider is bundled. What does depend on it waits for Spike S and is marked `TODO(spike-s)` in the code:
- the bundled IdP's secrets (internal CA and certificates, admin token, OIDC client secret);
- its compose service, external volume and proxy virtual host, with internal TLS;
- its backup and restore.

Until then, Atlas is set up the "bring your own OIDC provider" way.

## Architecture in brief

- **A modular monolith.** A Python kernel (`atlas/kernel`) provides identity, authorization, the module registry, events and the admin console. Modules (`modules/`) may import only the stable SDK (`atlas/sdk`), and CI enforces that boundary ([ARCHITECTURE](docs/ARCHITECTURE.md)).
- **One front door.** `atlas-proxy` (nginx) is the only container that publishes a port, on loopback by default. You put a Cloudflare Tunnel or your own TLS proxy in front of it ([ADR-003](docs/adr/003-internet-exposure.md)).
- **Identity through OIDC.** A bundled IdP (Kanidm, pending the spike) or your own provider; Atlas keeps revocable server-side sessions ([ADR-001](docs/adr/001-identity.md)).
- **Fail-closed configuration.** Secrets are generated into env files on first start, never committed, and weak or placeholder values stop the app from starting ([ADR-004](docs/adr/004-config-and-secrets.md)).
- **PostgreSQL** with migrations run by a one-shot service at start-up, a pre-migration dump, backups, and a data volume that survives `docker compose down -v` ([ADR-005](docs/adr/005-database-and-migrations.md)).
- **Compose stack:** `atlas-init` → `postgres` → `atlas-db-init` → `atlas-migrate` → `atlas-api` → `atlas-proxy` ([deploy/](deploy/README.md)).

## Documentation

| Document | What it covers |
|---|---|
| [VISION](docs/VISION.md) | What Atlas is for, who uses it, the milestones and what is out of scope |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Code layout, module manifest, connectors, events, data and storage |
| ADRs: [001](docs/adr/001-identity.md) · [002](docs/adr/002-authorization.md) · [003](docs/adr/003-internet-exposure.md) · [004](docs/adr/004-config-and-secrets.md) · [005](docs/adr/005-database-and-migrations.md) | Decisions on identity, authorization, internet exposure, config and secrets, and the database |
| [PLAN](docs/PLAN-v1.md) | The Foundation plan: every phase with its work, acceptance criteria and tests |
| [SELF_HOSTING](docs/SELF_HOSTING.md) | **The planned 1.0 install.** It describes the finished Foundation; parts of it don't work yet |
| [CONTRIBUTING](CONTRIBUTING.md) | Repo layout, local setup, and every check CI runs |
| [SECURITY](SECURITY.md) | How to report a vulnerability (privately, never in a public issue) |
| [legacy/](legacy/README.md) | The frozen Drive/Photos app: features, how to run it, configuration |

## Versioning

- **Releases follow [Semantic Versioning](https://semver.org/).** There are no releases yet.
- **0.x until 1.0.0.** Any 0.x release may contain breaking changes. **1.0.0** is the release that completes the Foundation milestone (Phase 10).
- **After 1.0.0:**
  - **MAJOR** for a change that breaks an existing install or module: a removed or renamed setting, an incompatible SDK or manifest change, or a migration that needs a manual step.
  - **MINOR** for new functionality, which includes **adding a module**.
  - **PATCH** for fixes.
- **Milestones have names, not numbers,** so they don't read as major versions:

  | Milestone | What it brings | Released as |
  |---|---|---|
  | **Foundation** | Identity, family roles, permissions, admin console, module registry, connectors | 1.0.0 |
  | **First modules** | Files and Drive (ported from `legacy/`), Photos (leaning: Immich link + SSO) | 1.x minor releases |
  | **Household apps** | `lift` (tasks, habits, journal), Nextcloud-replacement features, Hermes (if decided) | Later minor releases |

## License

See [LICENSE](LICENSE).

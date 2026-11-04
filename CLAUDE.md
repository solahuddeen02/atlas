# CLAUDE.md — Atlas

Working rules for Claude Code. **This file is public, like the rest of the repo:** never put hostnames, IPs,
domains, machine names or anyone's site details in it. The docs below are the source of truth —
this file only points to them and adds working rules. If this file and a doc disagree, the doc wins.

## What Atlas is
A self-hosted home platform for one household: one front door (OIDC login), family roles and permissions,
an admin console, and a module registry. Pre-1.0. Status per phase: `README.md` → Status.
**Spike S is done** (ADR-001 outcome (b): Kanidm is the bundled IdP, with the workarounds recorded there).
**Phase 5a is done** (bundled IdP service, volume, networks, vhost, internal CA, backup).
**Next: Phase 5b**: the IdP bootstrap (the work marked `TODO(phase-5b)`), then the RP, sessions and adapters.
Follow ADR-001 → "Workarounds and design consequences".

## Read first
| Need | Doc |
|---|---|
| Phases, acceptance criteria, rules for every phase | `docs/PLAN-v1.md` |
| Code layout, module boundaries, API/log/test conventions (§10) | `docs/ARCHITECTURE.md` |
| Decisions (identity, authz, exposure, config/secrets, database) | `docs/adr/` |
| Setup, layout, every CI check and how to run it locally | `CONTRIBUTING.md` |
| What an installer needs | `docs/SELF_HOSTING.md` |
| Release history + upgrade notes | `CHANGELOG.md` |

## Layout (short)
`atlas/kernel/` platform · `atlas/sdk/` stable API for modules · `modules/` (import `atlas.sdk` only) ·
`deploy/` compose stack + proxy · `web/` new SPA (from Phase 7) · `tests/` · `scripts/ci/` ·
`legacy/` frozen old Drive/Photos app — **security fixes only**.
Old plugin-architecture work (May 2026) is archived on branch `archive/legacy-plugins` — reference only, never merge.

## Commands
```bash
uv sync
uv sync --locked && uv run ruff check && uv run ruff format --check && uv run mypy && uv run lint-imports && uv run pytest
```
DB tests need `TEST_POSTGRES_URL`; compose tests need Docker + `RUN_COMPOSE_TESTS=1` (see CONTRIBUTING).

## Hard rules
- **Public repo: no real hostnames, IPs, domains, VM/host names or anyone's site details** — in code, tests,
  docs, this file, commit messages or PR text. Use `example.com` and RFC 5737 / RFC 3849 addresses (`192.0.2.x`, `2001:db8::`).
- **Never commit** `.env` files, `secrets/`, `backups/`, databases, or real credentials. Never print secret values.
- **Environment is read only in `atlas/kernel/config.py`**; everything else receives `Settings` (ADR-004, enforced by ruff TID251).
- **Module boundaries** are enforced by import-linter — no `modules.*` ↔ `atlas.kernel.*` imports.
- **Later phases never weaken earlier guarantees**; existing tests stay.
- Never suggest or run `docker compose down -v`.

## How work is done here
- **Develop only — never deploy** (not even to a lab). Deployment is done by the installer from the docs.
- Anything an installer needs (settings, volumes, ports, migrations, manual steps) goes into
  `docs/SELF_HOSTING.md` **written for any user**, plus an "Upgrade notes" entry in `CHANGELOG.md`.
- Flow: plan → branch → PR → **all CI checks green** → Deen merges. No force push, no history rewrite,
  no merging with a red check.
- **Stop and ask Deen** before: deleting files outside the change, rewriting history, changing an ADR's decision,
  or anything outside the task's stated scope.
- Report back with real command output (tests, grep, `git log -1`, PR URL) — "done" without output doesn't count.

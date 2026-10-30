# Contributing

Atlas is being rebuilt as the v1 platform described in [docs/PLAN-v1.md](docs/PLAN-v1.md).
Today's app lives in [`legacy/`](legacy/) and only gets security fixes.

## Layout

| Path | What it is |
|---|---|
| `atlas/kernel/`, `atlas/sdk/` | The v1 platform ([ARCHITECTURE §2](docs/ARCHITECTURE.md#2-code-layout-and-module-boundaries)) |
| `modules/` | v1 modules. They import `atlas.sdk` only |
| `web/` | v1 SPA (from Phase 7) |
| `deploy/` | v1 compose stack (from Phases 2–4) |
| `legacy/` | Today's app: `core/`, `frontend/`, `tests/`, `alembic/`, its Dockerfile and compose file |
| `scripts/ci/` | Checks run by CI |

## Setup

You need [uv](https://docs.astral.sh/uv/) and Node.js 22.

```bash
uv sync
```

## Checks CI runs

Every job in `.github/workflows/ci.yml` must be green before a PR merges.

| CI job | What it enforces | Run it locally |
|---|---|---|
| `backend` | `uv.lock` is current; ruff lint and format; `mypy --strict` on `atlas/`; import-linter contracts; the v1 test suite | `uv sync --locked && uv run ruff check && uv run ruff format --check && uv run mypy && uv run lint-imports && uv run pytest` |
| `legacy` | The legacy test suite still passes | `cd legacy && pip install -r requirements.txt && pytest` |
| `web` | The frontend builds from `package-lock.json` (lint is informational while it's the legacy frontend) | `cd legacy/frontend && npm ci && npm run lint && npm run build` |
| `gitleaks` | No secrets in the commits being pushed. A weekly workflow (`gitleaks-weekly`) scans the full history | `gitleaks git --redact .` |
| `audit` | No known vulnerabilities in `uv.lock` or the frontend's runtime deps (`legacy/requirements.txt` is reported, not enforced) | `uv export --all-groups --no-emit-project --format requirements-txt -o /tmp/req.txt && uv run pip-audit --strict --require-hashes --disable-pip -r /tmp/req.txt`, `cd legacy/frontend && npm audit --omit=dev` |
| `compose-lint` | No literal secret values in any compose file's `environment:` ([ADR-004 §3](docs/adr/004-config-and-secrets.md)) | `uv run python scripts/ci/lint_compose_secrets.py` |

### Rules that the linters enforce

- **No environment access outside `atlas/kernel/config.py`** ([ADR-004 §1](docs/adr/004-config-and-secrets.md)). Ruff's banned-api rule (`TID251`) rejects `os.getenv`, `os.environ`, `os.putenv` and `os.unsetenv` everywhere else. Read configuration through `Settings`.
- **Module boundaries** ([ARCHITECTURE §2](docs/ARCHITECTURE.md#2-code-layout-and-module-boundaries)). import-linter forbids `modules.*` → `atlas.kernel.*` and `atlas.kernel.*` → `modules.*`. A module-to-module independence contract is added once there are two modules.
- **Compose secrets.** A compose `environment:` entry whose name contains `secret`, `password`, `token` or `key` may only be empty, `${VAR}`, `${VAR:?message}` or a `*_FILE` path. `${VAR:-default}` is rejected, because the default is a literal.

The linters test themselves: `tests/ci/test_banned_api.py` and `tests/ci/test_compose_secret_lint.py` check that each one fails on a bad input and passes on a good one.

## Dependencies

- Python: edit `pyproject.toml`, then `uv lock`. Commit `uv.lock`.
- Dependabot opens weekly updates for `uv.lock` and GitHub Actions. Legacy dependencies get security updates only.

## Never commit

`.env` files, anything under `secrets/` or `backups/`, real hostnames or IPs. Use `example.com` placeholders ([PLAN-v1](docs/PLAN-v1.md) rules).

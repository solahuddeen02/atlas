"""Reject literal secrets in Docker Compose files (ADR-004 §3).

A service `environment:` entry whose name looks secret (secret, password, token, key)
must not carry a literal value. Allowed forms:

- no value at all (`- SECRET_KEY`, `SECRET_KEY:`), passed through from the shell;
- a required interpolation: `${VAR}`, `${VAR:?message}` or `${VAR?message}`;
- a `*_FILE` variable, whose value is a path to a mounted secret file.

`${VAR:-default}` is rejected for secret names, because the default is a literal.

Usage: lint_compose_secrets.py [FILE ...]
With no arguments, every compose file in the repository is checked.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

SECRET_NAME = re.compile(r"(?i)(secret|password|token|key)")
REQUIRED_INTERPOLATION = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:?\?[^}]*)?\}$")
COMPOSE_GLOBS = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
SKIP_DIRS = {".git", ".venv", "node_modules"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def find_compose_files(root: Path) -> list[Path]:
    found = {
        path
        for pattern in COMPOSE_GLOBS
        for path in root.rglob(pattern)
        if not SKIP_DIRS.intersection(path.relative_to(root).parts)
    }
    return sorted(found)


def iter_environment(services: dict[str, Any]) -> Iterator[tuple[str, str, str | None]]:
    """Yield (service, name, value) for every environment entry."""
    for service, spec in services.items():
        env = (spec or {}).get("environment")
        if isinstance(env, dict):
            for name, value in env.items():
                yield service, str(name), None if value is None else str(value)
        elif isinstance(env, list):
            for item in env:
                name, sep, value = str(item).partition("=")
                yield service, name, value if sep else None


def is_allowed(name: str, value: str | None) -> bool:
    if not SECRET_NAME.search(name) or name.upper().endswith("_FILE"):
        return True
    if value is None or value == "":
        return True
    return REQUIRED_INTERPOLATION.match(value.strip()) is not None


def lint_file(path: Path) -> list[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"{path}: cannot parse YAML: {exc}"]
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return []
    return [
        f"{path}: service '{service}' sets {name} to a literal value. "
        "Use ${" + name + "} or a *_FILE secret mount (ADR-004 §3)."
        for service, name, value in iter_environment(services)
        if not is_allowed(name, value)
    ]


def main(argv: list[str]) -> int:
    files = [Path(arg) for arg in argv] or find_compose_files(REPO_ROOT)
    errors = [error for path in files for error in lint_file(path)]
    for error in errors:
        print(error, file=sys.stderr)
    print(f"compose-lint: {len(files)} file(s) checked, {len(errors)} problem(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

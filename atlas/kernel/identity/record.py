"""The IdP bootstrap's completion record, `atlas-idp/bootstrap.json` (PLAN Phase 5b-2b).

Every run of `atlas-admin idp-bootstrap`, the fast path included, first writes
`complete: false` and writes `complete: true` only as its last act, after everything
else succeeded. Any failure, signal or kill leaves `false`. With the bundled IdP,
atlas-api refuses to start unless the record says `complete: true` for this bootstrap
version (`record_problem`, called by `atlas.kernel.config.load_settings`).

That is what makes a failed bootstrap block the API every time, not only the first
time: Compose cannot do it. A dependency declared `required: false` (needed so that
BYO OIDC, which has no such service, still starts) only warns when it fails, and
atlas-api starts regardless, with whatever idp.env an earlier run left behind.

Not secret, and read by atlas-api (group read). Nothing here reads the environment.
"""

from __future__ import annotations

import json
from pathlib import Path

# Bump when the desired state changes (a new group, a new client setting, ...): an
# install that ran an older bootstrap is then not complete until this one has run.
BOOTSTRAP_VERSION = 1
RECORD_FILE = "bootstrap.json"
LOGS_HINT = (
    "read `docker compose logs atlas-idp-bootstrap`, fix what it reports, then run "
    "`docker compose up -d` again"
)


def read_record(path: Path) -> dict[str, object] | None:
    """The record as a dict, or None if it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return data if isinstance(data, dict) else None


def is_current_version(version: object) -> bool:
    # `type(...) is int`: in Python True == 1, and a record saying `true` is not version 1.
    return type(version) is int and version == BOOTSTRAP_VERSION


def record_text(*, digest: str, complete: bool) -> str:
    record: dict[str, object] = {
        "version": BOOTSTRAP_VERSION,
        "digest": digest,
        "complete": complete,
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def record_problem(path: Path) -> str | None:
    """Why the bootstrap counts as not complete, or None when it is.

    The text names the cause and what to do, and holds nothing from the record but its
    version number.
    """
    if not path.is_file():
        return (
            f"the bundled IdP's bootstrap has not run yet ({path.name} does not exist); {LOGS_HINT}"
        )
    record = read_record(path)
    if record is None:
        return f"the bundled IdP's bootstrap record {path.name} is unreadable; {LOGS_HINT}"
    version = record.get("version")
    if not is_current_version(version):
        shown = version if isinstance(version, int) and not isinstance(version, bool) else "?"
        return (
            f"the bundled IdP's bootstrap record is from version {shown}, this release needs "
            f"version {BOOTSTRAP_VERSION}; {LOGS_HINT}"
        )
    if record.get("complete") is not True:
        return f"the bundled IdP's bootstrap has not completed (its last run failed); {LOGS_HINT}"
    return None

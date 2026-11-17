"""Run the API: `python -m atlas.kernel.web`.

Configuration is validated before the app is built or any socket is bound.
An invalid configuration exits with status 2 and prints only variable names.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.kernel.web")
    # All interfaces inside the container; only atlas-proxy publishes a host port (Phase 4).
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    from atlas.kernel.config import ConfigError, load_settings

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"atlas: refusing to start. {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from atlas.kernel.web.app import create_app
    from atlas.kernel.web.logging import configure_logging

    configure_logging(settings)
    # ONE process, on purpose: the app object is passed (not an import string) and
    # `workers` is 1, so uvicorn cannot start more. The IdentityAdmin adapter (Phase 5b-3b)
    # serialises role changes per person with an in-process lock, and a race between two
    # processes leaves a person in two role groups (probe P5: 20 of 20 rounds). Before
    # ever running more than one process, role changes need a cross-process lock first: a
    # Postgres advisory lock around the change (pg_advisory_xact_lock on the person).
    # tests/kernel/test_single_process.py fails if this is changed without that.
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        workers=1,
        log_config=None,
        log_level=settings.log_level,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_config=None,
        log_level=settings.log_level,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

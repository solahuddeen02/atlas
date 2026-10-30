"""Process logging from Settings (ATLAS_LOG_LEVEL, ATLAS_LOG_FORMAT)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from atlas.kernel.config import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    if settings.is_production and settings.dev_cors_origins_raw:
        logging.getLogger("atlas.config").warning("ATLAS_DEV_CORS_ORIGINS is ignored in production")

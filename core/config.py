from __future__ import annotations

import os

ATLAS_DOMAIN   = os.getenv("ATLAS_DOMAIN", "atlas.local")
COOKIE_DOMAIN  = f".{ATLAS_DOMAIN}"
COOKIE_SECURE  = os.getenv("ATLAS_COOKIE_SECURE", "false").lower() == "true"
COOKIE_NAME    = "atlas_token"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

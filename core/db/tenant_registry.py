from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import sessionmaker, Session

# ATLAS_TENANT_MODE controls the isolation level:
#   single   — one default tenant, no multi-tenancy (family at home)
#   shared   — multiple tenants in one DB, row-level isolation (default)
#   isolated — each tenant has its own database, zero data bleed
#
# Upgrade path: single → shared → isolated, no code changes needed.
ATLAS_TENANT_MODE = os.getenv("ATLAS_TENANT_MODE", "shared")


class TenantRegistry:
    """Routes DB sessions to the correct engine per tenant.

    In 'shared' mode:   all tenants share the default engine.
    In 'isolated' mode: each tenant uses a dedicated engine resolved from:
      1. Engines registered via TenantRegistry.register()
      2. Env var ATLAS_TENANT_<SLUG>_DB_URL  (auto-registered on first access)
      3. Fallback: default engine
    """

    _engines: dict[str, Any] = {}

    @classmethod
    def register(cls, tenant_slug: str, db_url: str) -> None:
        """Register a dedicated DB engine for a tenant (isolated mode)."""
        from core.db.engine import make_engine
        cls._engines[tenant_slug] = make_engine(db_url)

    @classmethod
    def get_engine(cls, tenant_slug: str | None = None) -> Any:
        from core.db.engine import engine as _default
        if ATLAS_TENANT_MODE == "isolated" and tenant_slug:
            if tenant_slug not in cls._engines:
                env_key = f"ATLAS_TENANT_{tenant_slug.upper()}_DB_URL"
                db_url = os.getenv(env_key)
                if db_url:
                    from core.db.engine import make_engine
                    cls._engines[tenant_slug] = make_engine(db_url)
            if tenant_slug in cls._engines:
                return cls._engines[tenant_slug]
        return _default

    @classmethod
    def get_session(cls, tenant_slug: str | None = None) -> Session:
        eng = cls.get_engine(tenant_slug)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=eng)
        return factory()

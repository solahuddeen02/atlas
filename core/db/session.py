from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from core.db.engine import engine
from core.db.tenant_registry import TenantRegistry

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def get_db():
    """FastAPI dependency: one session per request using the default (shared) engine."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_tenant_db(tenant_slug: str):
    """FastAPI dependency factory: session routed to the correct engine for a tenant.

    In 'shared' mode  → same as get_db().
    In 'isolated' mode → routes to the tenant's dedicated engine if registered.

    Usage (static tenant):
        @router.get("/")
        def endpoint(db = Depends(get_tenant_db("family"))): ...

    Usage (dynamic, from JWT):
        def get_db_for_user(user = Depends(get_current_user)):
            yield from get_tenant_db(user.tenant_slug)()
    """
    def _dep():
        db = TenantRegistry.get_session(tenant_slug)
        try:
            yield db
        finally:
            db.close()
    return _dep

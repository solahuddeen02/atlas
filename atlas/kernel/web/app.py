"""The FastAPI application factory. Everything comes from the `Settings` passed in."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

import atlas
from atlas.kernel.config import Settings
from atlas.kernel.db.engine import make_engine

logger = logging.getLogger("atlas.health")


def create_app(settings: Settings, *, engine: Engine | None = None) -> FastAPI:
    """Build the API. `engine` is created from Settings on first use unless given (tests)."""
    state: dict[str, Engine] = {}
    if engine is not None:
        state["engine"] = engine

    def get_engine() -> Engine:
        # Lazy, so the API starts (and /health answers) even while the DB is down.
        if "engine" not in state:
            state["engine"] = make_engine(settings.database_url)
        return state["engine"]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is None and "engine" in state:
            state["engine"].dispose()

    app = FastAPI(
        lifespan=lifespan,
        title="Atlas",
        version=atlas.__version__,
        # ADR-004 §4: no interactive docs or schema in production.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        # Exactly this body: nothing about versions, config or dependencies.
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        # Readiness for trusted peers only (the proxy enforces that, ADR-003).
        # Never says why it isn't ready: details go to the logs, not the response.
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            logger.warning("readiness check failed: database unreachable")
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app

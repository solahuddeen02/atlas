"""The FastAPI application factory. Everything comes from the `Settings` passed in."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import atlas
from atlas.kernel.config import Settings


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
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

    return app

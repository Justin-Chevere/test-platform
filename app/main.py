import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import engine
from app.migrate import check_schema
from app.routers import health, projects, runs


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO)
    # Refuse to start on an outdated schema, rather than fail later on the first request
    # that touches a missing column. Migrating is its own step: `alembic upgrade head`.
    check_schema(engine)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    # The API makes this machine run commands and has no logins yet, so it only answers
    # requests addressed to this machine by name. That blocks DNS rebinding: a web page
    # pointing its own domain at 127.0.0.1 to reach local services through your browser.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(runs.router)
    return app


app = create_app()

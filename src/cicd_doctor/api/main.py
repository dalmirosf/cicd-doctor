"""FastAPI application: ``GET /health``, ``GET /ready`` and ``POST /webhooks/github``.

``create_app`` receives its dependencies; the composition root (``app/container.py``) builds the
real ones from the settings (D-55). Tests pass in-memory ones.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cicd_doctor.api import webhooks
from cicd_doctor.api.deps import ApiDependencies, get_dependencies

DependencyProvider = Callable[[], AbstractAsyncContextManager[ApiDependencies]]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ready", "unavailable"]


def create_app(dependencies: ApiDependencies | DependencyProvider) -> FastAPI:
    """``dependencies``: ready-made (tests) or a provider opened in the lifespan (production)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if isinstance(dependencies, ApiDependencies):
            yield
            return
        async with dependencies() as deps:
            app.state.dependencies = deps
            yield

    app = FastAPI(title="CICD Doctor", lifespan=lifespan)
    if isinstance(dependencies, ApiDependencies):
        app.state.dependencies = dependencies

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness: the process is up and serving requests (D-40)."""
        return HealthResponse(status="ok")

    @app.get(
        "/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ReadyResponse, "description": "The database does not answer"}},
    )
    async def ready(request: Request) -> JSONResponse:
        """Readiness: the database answers (D-53)."""
        ok = await get_dependencies(request).ready()
        body = ReadyResponse(status="ready" if ok else "unavailable")
        return JSONResponse(body.model_dump(), status_code=200 if ok else 503)

    app.include_router(webhooks.router)
    return app

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

from .config import Settings
from .service import BridgeService
from .upstream import UpstreamClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(service: BridgeService | None = None) -> FastAPI:
    settings = Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.bridge_service = service
            yield
            return

        timeout = httpx.Timeout(
            settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        )
        async with httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "aihot-data-bridge/0.1"},
        ) as client:
            app.state.bridge_service = BridgeService(
                UpstreamClient(client, max_retries=settings.max_retries),
                settings,
            )
            yield

    app = FastAPI(
        title="AIHOT Data Bridge",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/aihot/today")
    async def today(request: Request) -> dict:
        bridge: BridgeService = request.app.state.bridge_service
        return await bridge.today()

    return app


app = create_app()

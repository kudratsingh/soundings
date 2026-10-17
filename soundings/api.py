"""HTTP entry point.

Phase 0 exposes only ``GET /health`` and, when the frontend has been built, serves
``web/dist`` at ``/``. The static mount is registered after the API routes so that
API paths always win.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from soundings import __version__

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def create_app(web_dist: Path = WEB_DIST) -> FastAPI:
    """Build the FastAPI app. ``web_dist`` is mounted only if it exists."""
    application = FastAPI(title="Soundings", version=__version__)

    @application.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "model_mode": os.environ.get("MODEL_MODE", "replay")}

    # --- WO-020 begin: registry (router + form; before the web/dist mount) ---
    from soundings.registry.router import registry_page
    from soundings.registry.router import router as registry_router

    application.include_router(registry_router, prefix="/api/plans")
    application.add_api_route("/registry", registry_page, methods=["GET"], include_in_schema=False)
    # --- WO-020 end ---

    if web_dist.is_dir():
        application.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return application


app = create_app()

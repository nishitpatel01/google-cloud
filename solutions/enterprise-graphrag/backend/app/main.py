"""FastAPI application factory for Enterprise GraphRAG & Contagion Explorer."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.core.config import Settings, get_settings
from app.core.security import (
    PromptBlockedError,
    SecurityMiddleware,
    SlidingWindowRateLimiter,
)
from app.ingestion.upload_validator import UploadValidationError
from app.packs.loader import LoadedPack, load_all_packs

logger = logging.getLogger("enterprise_graphrag")


def create_app(
    *,
    settings: Settings | None = None,
    packs: dict[str, LoadedPack] | None = None,
    write_limiter: SlidingWindowRateLimiter | None = None,
    read_limiter: SlidingWindowRateLimiter | None = None,
) -> FastAPI:
    """Create a hardened FastAPI instance with zero blocking GCP calls at startup."""
    cfg = settings or get_settings()
    loaded_packs = packs if packs is not None else load_all_packs(cfg.packs_dir)

    app = FastAPI(
        title="Enterprise GraphRAG & Contagion Explorer",
        version=cfg.app_version,
        docs_url="/api/docs" if cfg.demo_mode != "live" else None,
        redoc_url=None,
    )
    app.state.settings = cfg
    app.state.packs = loaded_packs
    app.state.engine_overrides = {}
    app.state.iap_certs_override = None
    app.state.extractor_override = None
    app.state.persist_override = None

    # Strict CORS allowlist (Settings.allowed_origins_list rejects wildcard '*')
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    app.add_middleware(
        SecurityMiddleware,
        settings=cfg,
        write_limiter=write_limiter,
        read_limiter=read_limiter,
    )

    @app.exception_handler(PromptBlockedError)
    async def _handle_prompt_blocked(_request: Request, exc: PromptBlockedError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(UploadValidationError)
    async def _handle_upload_invalid(_request: Request, exc: UploadValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Log detailed diagnostic server-side; never expose SQL traces or secrets to callers
        logger.exception("Unhandled error during request: %s", type(exc).__name__)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    app.include_router(api_router)

    # Serve compiled React SPA bundle when present (Phase 3+ & Cloud Run multi-stage image)
    dist_dir = cfg.solution_root / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = dist_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(full_path: str) -> Any:
            if full_path.startswith("api/") or full_path == "health":
                return JSONResponse({"detail": "Not found"}, status_code=404)
            resolved = (dist_dir / Path(full_path)).resolve()
            if (
                full_path
                and str(resolved).startswith(str(dist_dir.resolve()) + "/")
                and resolved.is_file()
            ):
                return FileResponse(str(resolved))
            return FileResponse(str(index_html))

    return app


app = create_app()

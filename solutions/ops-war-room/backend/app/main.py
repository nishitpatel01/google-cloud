"""FastAPI application factory with security middleware, non-wildcard CORS, and safe SPA mounting."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router as api_router
from app.core.config import Settings
from app.core.gcp_clients import GCPClientPool
from app.core.security import SecurityHeadersAndGuardMiddleware
from app.engines.mcp_registry import MCPRegistry
from app.engines.workflows_executor import WorkflowsSagaExecutor
from app.packs.loader import load_all_packs


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    app = FastAPI(
        title="Operations & Incident War Room (ACC-04)",
        version=cfg.app_version,
        docs_url="/api/docs" if cfg.gcp_env != "prod" else None,
        redoc_url=None,
    )

    app.add_middleware(SecurityHeadersAndGuardMiddleware, settings=cfg)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    packs = load_all_packs(cfg.packs_dir, verify_manifest=True)
    gcp_clients = GCPClientPool(cfg) if cfg.live_capable else None
    mcp_registry = MCPRegistry(cfg.packs_dir / "mcp_registry.yaml")
    workflows_executor = WorkflowsSagaExecutor(cfg, gcp_clients)

    app.state.settings = cfg
    app.state.packs = packs
    app.state.gcp_clients = gcp_clients
    app.state.mcp_registry = mcp_registry
    app.state.workflows_executor = workflows_executor

    app.include_router(api_router)

    dist_dir = (cfg.solution_root / "frontend" / "dist").resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        if not dist_dir.is_dir():
            raise HTTPException(
                status_code=404,
                detail="Frontend dist/ not built yet. Run `cd frontend && npm run build`.",
            )
        candidate = (dist_dir / full_path).resolve()
        # Strict path traversal boundary check
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_dir):
            return FileResponse(candidate)
        index_html = dist_dir / "index.html"
        if index_html.is_file():
            return FileResponse(index_html)
        raise HTTPException(status_code=404, detail="SPA index.html missing")

    return app


app = create_app()

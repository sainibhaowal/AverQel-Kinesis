from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.auth import api as auth
from app.auth.dependencies import AuthContext, get_auth_context
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.deepspace.api import acp as deepspace_acp
from app.deepspace.api import artifacts as deepspace_artifacts
from app.deepspace.api import browser as deepspace_browser
from app.deepspace.api import chats as deepspace_chats
from app.deepspace.api import client_storage, codex_runtime
from app.deepspace.api import documents as deepspace_documents
from app.deepspace.api import export as deepspace_export
from app.deepspace.api import library as deepspace_library
from app.deepspace.api import model_selection as deepspace_model_selection
from app.deepspace.api import plugins as deepspace_plugins
from app.deepspace.api import sandbox as deepspace_sandbox
from app.deepspace.api import schedules as deepspace_schedules
from app.deepspace.api import session_export as deepspace_session_export
from app.deepspace.api import session_feedback as deepspace_session_feedback
from app.deepspace.api import session_query as deepspace_session_query
from app.deepspace.api import ssh_hosts as deepspace_ssh_hosts
from app.deepspace.api import web_search as deepspace_web_search
from app.deepspace.api import webhooks as deepspace_webhooks
from app.deepspace.api import workspace as deepspace_workspace
from app.deepspace.services.plugin_hmr import get_plugin_hmr_service
from app.documents.api import collection_notifications, documents, organization
from app.documents.models.collection import DocumentCollection
from app.integrations.api import integrations, mcp
from app.integrations.api import voice as voice_routes
from app.platform.database.session import get_db, get_engine
from app.providers.api import providers
from app.providers.services.searxng_runtime import SearXNGRuntime
from app.query.api import chats, intelligence, queries
from app.realtime.api import router as realtime_router
from app.system.api import (
    admin,
    app_feedback,
    capabilities,
    dashboard,
    feedback,
    health,
    metrics,
    notifications,
    plans,
    storage,
    support,
)
from app.system.services.otel import (
    configure_telemetry,
    instrument_sqlalchemy,
    telemetry_span,
)

logger = logging.getLogger(__name__)


def _build_cors_kwargs(settings: Settings) -> dict[str, object]:
    origins = list(settings.cors_origins or [])

    # Production-safe behavior:
    # - If explicit origins are configured, use them.
    # - If wildcard is configured, allow all origins explicitly.
    # - Do not silently fall back to a permissive regex.
    if origins == ["*"]:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["*"],
            "allow_headers": ["*"],
        }

    for origin in (
        "http://localhost:11030",
        "http://127.0.0.1:11030",
        "http://localhost:10030",
        "http://127.0.0.1:10030",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ):
        if origin not in origins:
            origins.append(origin)

    return {
        "allow_origins": origins,
        # The packaged desktop renderer uses a private random loopback port.
        # Keep that renderer local while allowing it to call the isolated
        # Harness API without requiring a fixed production origin.
        "allow_origin_regex": r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?$",
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": ["*"],
        "max_age": 600,
    }


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_telemetry(settings)
    instrument_sqlalchemy(get_engine())

    # Real production version: prefer release_version (git tag), fallback to app_version
    effective_version = settings.release_version or settings.app_version

    app = FastAPI(
        title=settings.app_name,
        version=effective_version,
    )

    async def start_deepspace_plugin_hmr() -> None:
        """Start the development plugin watcher when enabled."""
        if settings.deepspace_plugin_hmr_enabled:
            app.state.deepspace_plugin_hmr = await get_plugin_hmr_service(settings)

    async def stop_deepspace_plugin_hmr() -> None:
        """Stop the development plugin watcher before the API exits."""
        service = getattr(app.state, "deepspace_plugin_hmr", None)
        if service is not None:
            await service.shutdown()

    async def start_local_searxng() -> None:
        """Start the Docker-free local SearXNG runtime when enabled."""
        runtime = SearXNGRuntime(settings)
        app.state.searxng_runtime = runtime
        if not settings.searxng_local_runtime_enabled:
            return
        try:
            await asyncio.to_thread(runtime.ensure_running)
        except Exception:  # noqa: BLE001
            # The API remains usable for local file and model work. The
            # provider status endpoint and the next web-search error expose
            # the bounded runtime diagnostic instead of hiding the failure.
            logger.warning("Local SearXNG did not start", exc_info=True)

    async def stop_local_searxng() -> None:
        """Stop only the SearXNG process started by this API process."""
        runtime = getattr(app.state, "searxng_runtime", None)
        if runtime is not None:
            await asyncio.to_thread(runtime.shutdown)

    app.router.add_event_handler("startup", start_deepspace_plugin_hmr)
    app.router.add_event_handler("startup", start_local_searxng)
    app.router.add_event_handler("shutdown", stop_deepspace_plugin_hmr)
    app.router.add_event_handler("shutdown", stop_local_searxng)

    app.add_middleware(
        CORSMiddleware,
        **_build_cors_kwargs(settings),
    )

    app.add_middleware(RequestContextMiddleware)

    @app.middleware("http")
    async def opentelemetry_http_middleware(request: Request, call_next):
        """Trace every API request without recording request bodies or secrets."""
        with telemetry_span(
            "http.server",
            {
                "http.request.method": request.method,
                "url.path": request.url.path,
                "server.address": request.url.hostname,
            },
        ) as span:
            try:
                response = await call_next(request)
                span.set_attribute("http.response.status_code", response.status_code)
                return response
            except Exception as exc:  # noqa: BLE001
                span.record_exception(exc)
                raise

    register_exception_handlers(app)

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(capabilities.router, prefix=settings.api_prefix)
    app.include_router(auth.router, prefix=settings.api_prefix)
    app.include_router(documents.router, prefix=settings.api_prefix)
    app.include_router(collection_notifications.router, prefix=settings.api_prefix)
    app.include_router(organization.router, prefix=settings.api_prefix)
    app.include_router(client_storage.router, prefix=settings.api_prefix)
    app.include_router(codex_runtime.router, prefix=settings.api_prefix)
    app.include_router(queries.router, prefix=settings.api_prefix)
    app.include_router(intelligence.router, prefix=settings.api_prefix)
    app.include_router(providers.router, prefix=settings.api_prefix)
    app.include_router(
        feedback.router,
        prefix=settings.api_prefix + "/feedback",
        tags=["feedback"],
    )
    app.include_router(chats.router, prefix=settings.api_prefix)
    app.include_router(deepspace_chats.router, prefix=settings.api_prefix)
    app.include_router(deepspace_export.router, prefix=settings.api_prefix)
    app.include_router(deepspace_library.router, prefix=settings.api_prefix)
    app.include_router(deepspace_artifacts.router, prefix=settings.api_prefix)
    app.include_router(deepspace_documents.router, prefix=settings.api_prefix)
    app.include_router(deepspace_sandbox.router, prefix=settings.api_prefix)
    app.include_router(deepspace_schedules.router, prefix=settings.api_prefix)
    app.include_router(deepspace_acp.router, prefix=settings.api_prefix)
    app.include_router(deepspace_workspace.router, prefix=settings.api_prefix)
    app.include_router(deepspace_web_search.router, prefix=settings.api_prefix)
    app.include_router(deepspace_webhooks.router, prefix=settings.api_prefix)
    app.include_router(deepspace_browser.router, prefix=settings.api_prefix)
    app.include_router(deepspace_model_selection.router, prefix=settings.api_prefix)
    app.include_router(deepspace_plugins.router, prefix=settings.api_prefix)
    app.include_router(deepspace_ssh_hosts.router, prefix=settings.api_prefix)
    app.include_router(deepspace_session_query.router, prefix=settings.api_prefix)
    app.include_router(deepspace_session_feedback.router, prefix=settings.api_prefix)
    app.include_router(deepspace_session_export.router, prefix=settings.api_prefix)
    app.include_router(integrations.router, prefix=settings.api_prefix)
    app.include_router(support.router, prefix=settings.api_prefix)
    app.include_router(admin.router, prefix=settings.api_prefix)
    app.include_router(app_feedback.router, prefix=settings.api_prefix)
    app.include_router(notifications.router, prefix=settings.api_prefix)
    app.include_router(metrics.router, prefix=settings.api_prefix)
    app.include_router(plans.router, prefix=settings.api_prefix)
    app.include_router(storage.router, prefix=settings.api_prefix)
    app.include_router(dashboard.router, prefix=settings.api_prefix)
    app.include_router(mcp.router, prefix=settings.api_prefix)
    app.include_router(voice_routes.router, prefix=settings.api_prefix)
    app.include_router(realtime_router, prefix=settings.api_prefix)

    @app.api_route(
        f"{settings.api_prefix}/collections",
        methods=["GET"],
    )
    def list_collections_compat(
        request: Request,
        auth_context: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> list[dict[str, object]]:
        del request
        tenant_id = auth_context.tenant_id
        items: list[dict[str, object]] = []
        try:
            from sqlalchemy import select

            from app.documents.models.collection import DocumentCollection
            from app.documents.models.organization import DocumentSmartCollection

            query = select(DocumentSmartCollection)
            if tenant_id:
                query = query.where(DocumentSmartCollection.tenant_id == tenant_id)
            smart_rows = db.scalars(query.order_by(DocumentSmartCollection.name)).all()
            for r in smart_rows:
                items.append({"id": str(r.id), "name": r.name, "description": r.description or ""})

            doc_query = select(DocumentCollection)
            if tenant_id:
                doc_query = doc_query.where(DocumentCollection.tenant_id == tenant_id)
            doc_rows = db.scalars(doc_query.order_by(DocumentCollection.name)).all()
            for d in doc_rows:
                if not any(item["id"] == str(d.id) for item in items):
                    items.append(
                        {"id": str(d.id), "name": d.name, "description": d.description or ""}
                    )
        except Exception:
            pass
        return items

    @app.api_route(
        f"{settings.api_prefix}/collections",
        methods=["POST"],
        status_code=201,
    )
    async def create_collection_compat(
        request: Request,
        auth_context: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_db),
    ) -> dict[str, object]:
        raw_payload = await request.json()
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Collection name is required")
        description = str(payload.get("description") or "").strip()
        collection = DocumentCollection(
            tenant_id=auth_context.tenant_id,
            name=name[:255],
            connection_code=uuid.uuid4().hex[:16],
            description=description[:1024],
        )
        db.add(collection)
        db.commit()
        return {
            "id": str(collection.id),
            "name": collection.name,
            "description": collection.description,
        }

    return app


app = create_app()

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .clients import GitHubClient, JenkinsClient, UpstreamError
from .config import Settings, get_settings
from .extensions import ExtensionCatalog, ExtensionRunnerClient
from .mcp_server import create_mcp_server
from .registry import OperationRegistry
from .request_context import current_principal
from .security import (
    Redactor,
    SlidingWindowLimiter,
    TokenAuthenticator,
    bearer_token,
    verify_webhook_signature,
)
from .service import JenkinsService
from .store import PostgresStore, Store
from .webhook import dispatch_github_webhook, process_github_webhook


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    authenticator: TokenAuthenticator | None = None,
    service: JenkinsService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    owns_store = store is None
    store = store or PostgresStore(settings.postgres_dsn())
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)

    if service is None:
        jenkins_token = (
            settings.read_secret(settings.jenkins_token_file)
            if settings.jenkins_token_file.exists()
            else "unconfigured"
        )
        github_write_token = (
            settings.read_secret(settings.github_write_token_file)
            if settings.github_write_token_file.exists()
            else "unconfigured"
        )
        installed_catalog = Path("/app/extensions")
        source_catalog = Path(__file__).parents[2] / "extensions"
        catalog = ExtensionCatalog.from_directory(
            installed_catalog if installed_catalog.exists() else source_catalog,
            set(settings.extension_allowlist),
        )
        service = JenkinsService(
            store=store,
            jenkins=JenkinsClient(
                settings.jenkins_url,
                settings.jenkins_user,
                jenkins_token,
                github_web_url=settings.github_web_url,
            ),
            github=GitHubClient(settings.github_api_url, github_write_token),
            extension_runner=ExtensionRunnerClient(
                settings.extension_runner_url,
                settings.max_extension_output_bytes,
            ),
            extension_catalog=catalog,
            github_allowlist=settings.github_allowlist,
            service_version=settings.service_version,
            max_artifact_bytes=settings.max_artifact_bytes,
            redactor=Redactor(settings.log_redaction_patterns),
        )
    if authenticator is None and settings.api_tokens_file.exists():
        authenticator = TokenAuthenticator.from_file(settings.api_tokens_file)

    registry = OperationRegistry()
    service.operation_ids = [operation.id for operation in registry.operations]
    mcp = create_mcp_server(service, registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await store.connect()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if owns_store:
                await store.close()
            await service.jenkins.close()
            await service.github.close()
            await service.extension_runner.close()

    app = FastAPI(
        title="JenkinsService API",
        version=settings.service_version,
        description=(
            "Curated REST and MCP control plane for Jenkins. Raw Jenkins passthrough, "
            "credentials, job XML, plugin management, and script console are not exposed."
        ),
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.service = service
    app.state.registry = registry

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        origin = request.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            return JSONResponse(
                {"detail": "Origin is not allowed"},
                status_code=403,
                headers={"X-Request-ID": request_id},
            )
        exempt = request.url.path in {
            "/healthz",
            "/readyz",
            "/webhooks/github",
        }
        if settings.require_https and not exempt:
            forwarded = (
                request.headers.get("x-forwarded-proto") if settings.trust_proxy_headers else None
            )
            scheme = forwarded or request.url.scheme
            if scheme != "https":
                return JSONResponse(
                    {"detail": "HTTPS is required"},
                    status_code=400,
                    headers={"X-Request-ID": request_id},
                )

        context_token = None
        if not exempt:
            if authenticator is None:
                return JSONResponse(
                    {"detail": "API authentication is not configured"},
                    status_code=503,
                    headers={"X-Request-ID": request_id},
                )
            try:
                token = bearer_token(request)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers={
                        **(exc.headers or {}),
                        "X-Request-ID": request_id,
                    },
                )
            principal = authenticator.authenticate(token)
            if principal is None:
                return JSONResponse(
                    {"detail": "Invalid bearer token"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer realm="jenkins-service"',
                        "X-Request-ID": request_id,
                    },
                )
            if not limiter.allow(principal.token_id):
                return JSONResponse(
                    {"detail": "Rate limit exceeded"},
                    status_code=429,
                    headers={
                        "Retry-After": "60",
                        "X-Request-ID": request_id,
                    },
                )
            request.state.principal = principal
            context_token = current_principal.set(principal)

        try:
            response = await call_next(request)
        finally:
            if context_token is not None:
                current_principal.reset(context_token)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> JSONResponse:
        ready_state = await store.ping()
        return JSONResponse(
            {"status": "ready" if ready_state else "not-ready"},
            status_code=200 if ready_state else 503,
        )

    @app.post("/webhooks/github", status_code=202, include_in_schema=False)
    async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        body_buffer = bytearray()
        async for chunk in request.stream():
            body_buffer.extend(chunk)
            if len(body_buffer) > settings.max_webhook_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="webhook payload too large",
                )
        body = bytes(body_buffer)
        if not settings.github_webhook_secret_file.exists():
            raise HTTPException(
                status_code=503,
                detail="webhook secret is not configured",
            )
        secret = settings.read_secret(settings.github_webhook_secret_file)
        if not verify_webhook_signature(
            secret,
            body,
            request.headers.get("x-hub-signature-256"),
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid webhook signature",
            )
        delivery_id = request.headers.get("x-github-delivery")
        event = request.headers.get("x-github-event")
        if not delivery_id or not event:
            raise HTTPException(
                status_code=400,
                detail="missing GitHub delivery headers",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid JSON payload",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail="webhook JSON must be an object",
            )
        delivery, inserted = await process_github_webhook(
            service,
            delivery_id,
            event,
            payload,
        )
        if not inserted:
            return {
                "accepted": True,
                "duplicate": True,
                "delivery_id": delivery_id,
            }
        if not delivery.accepted:
            raise HTTPException(status_code=403, detail=delivery.reason)
        background_tasks.add_task(
            dispatch_github_webhook,
            service,
            event,
            payload,
        )
        return {
            "accepted": True,
            "duplicate": False,
            "delivery_id": delivery_id,
        }

    api_router = APIRouter(prefix="/api/v1")
    registry.install_api_routes(api_router, service)
    app.include_router(api_router)
    app.mount("/", mcp.streamable_http_app())
    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        "jenkins_service.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        proxy_headers=False,
    )

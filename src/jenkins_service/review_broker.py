from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .review import (
    DiffValidationError,
    OpenAIReviewClient,
    OpenAIReviewError,
    ReviewClientConfig,
    ReviewResult,
)

OPENAI_API_KEY_PATH = Path("/run/secrets/openai_api_key")
REVIEW_BROKER_TOKEN_PATH = Path("/run/secrets/review_broker_token")
_BEARER = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)]


class ReviewBrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    pull_request: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff: str = Field(min_length=1)
    relevant_context: str = ""


class ReviewBrokerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    prompt_version: str
    blocking: bool
    review: ReviewResult


def _integer_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    value = float(os.getenv(name, str(default)))
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return value


def config_from_environment() -> ReviewClientConfig:
    reasoning = os.getenv("OPENAI_REASONING_EFFORT", "medium")
    if reasoning not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("OPENAI_REASONING_EFFORT is invalid")
    return ReviewClientConfig(
        model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
        reasoning_effort=reasoning,
        prompt_version=os.getenv("REVIEW_PROMPT_VERSION", "v1"),
        max_diff_bytes=_integer_env("REVIEW_MAX_DIFF_BYTES", 250_000),
        max_context_bytes=_integer_env("REVIEW_MAX_CONTEXT_BYTES", 50_000),
        max_output_bytes=_integer_env("REVIEW_MAX_OUTPUT_BYTES", 64_000),
        max_output_tokens=_integer_env("OPENAI_MAX_OUTPUT_TOKENS", 4_096),
        max_attempts=_integer_env("OPENAI_MAX_RETRIES", 3),
        timeout_seconds=_float_env("OPENAI_TIMEOUT_SECONDS", 60.0),
        retry_base_seconds=_float_env(
            "OPENAI_RETRY_BASE_SECONDS",
            0.25,
            allow_zero=True,
        ),
    )


def _read_secret(path: Path, name: str) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} secret file is empty")
    return value


def create_review_broker_app(
    *,
    config: ReviewClientConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
    api_key_path: Path = OPENAI_API_KEY_PATH,
    broker_token_path: Path = REVIEW_BROKER_TOKEN_PATH,
) -> FastAPI:
    """Create the internal-only broker.

    Production callers use the fixed defaults. Path injection exists solely for
    tests; neither secret is ever accepted through an environment variable.
    """
    active_config = config or config_from_environment()
    api_key = _read_secret(api_key_path, "OpenAI API key")
    broker_token = _read_secret(broker_token_path, "review broker token")
    review_client = OpenAIReviewClient(
        api_key,
        config=active_config,
        client=http_client,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        await review_client.close()

    app = FastAPI(
        title="JenkinsService Internal Review Broker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authenticate(
        credentials: _BearerCredentials,
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, broker_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/internal/v1/review",
        response_model=ReviewBrokerResponse,
        dependencies=[Depends(authenticate)],
    )
    async def review(request: ReviewBrokerRequest) -> ReviewBrokerResponse:
        try:
            result = await review_client.review(request.diff, request.relevant_context)
        except DiffValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OpenAIReviewError as exc:
            raise HTTPException(status_code=502, detail="AI review failed") from exc
        return ReviewBrokerResponse(
            model=active_config.model,
            prompt_version=active_config.prompt_version,
            blocking=result.has_critical_findings,
            review=result,
        )

    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        "jenkins_service.review_broker:create_review_broker_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container listens only on its internal network
        port=8100,
    )

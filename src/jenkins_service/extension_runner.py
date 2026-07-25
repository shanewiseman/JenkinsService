from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from .extensions import ExtensionCatalog
from .models import ExtensionManifest, ExtensionOutput


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ExtensionManifest
    action: str
    payload: dict[str, Any]


class OutputLimitExceededError(Exception):
    pass


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> bytes:
    if stream is None:
        return b""
    content = bytearray()
    while chunk := await stream.read(65_536):
        content.extend(chunk)
        if len(content) > limit:
            raise OutputLimitExceededError
    return bytes(content)


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
    input_bytes: bytes,
    stdout_limit: int,
) -> tuple[bytes, bytes]:
    async def write_input() -> None:
        if process.stdin is None:
            return
        process.stdin.write(input_bytes)
        await process.stdin.drain()
        process.stdin.close()

    stdout, stderr, _ = await asyncio.gather(
        _read_bounded(process.stdout, stdout_limit),
        _read_bounded(process.stderr, 4096),
        write_input(),
    )
    await process.wait()
    return stdout, stderr


def create_runner_app(
    *,
    catalog_path: Path | None = None,
    allowlist: set[str] | None = None,
    max_output_bytes: int = 1_000_000,
) -> FastAPI:
    catalog_path = catalog_path or Path(os.getenv("EXTENSION_CATALOG_PATH", "/app/extensions"))
    if allowlist is None:
        allowlist = {
            value.strip()
            for value in os.getenv("EXTENSION_ALLOWLIST", "").split(",")
            if value.strip()
        }
    catalog = ExtensionCatalog.from_directory(catalog_path, allowlist)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield

    app = FastAPI(
        title="JenkinsService Extension Runner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/v1/run", response_model=ExtensionOutput)
    async def run_extension(request: RunRequest) -> ExtensionOutput:
        try:
            canonical = catalog.get(request.manifest.id)
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail="extension is not allowlisted",
            ) from exc
        if request.manifest != canonical:
            raise HTTPException(status_code=400, detail="manifest does not match catalog")
        if request.action not in canonical.actions:
            raise HTTPException(status_code=400, detail="action is not declared")
        try:
            catalog.validate_input(canonical.id, request.payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        command = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            f"{canonical.memory_mb}m",
            "--cpus",
            str(canonical.cpus),
            "--label",
            "dev.jenkinsservice.extension=true",
            canonical.image,
            request.action,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
                "DOCKER_TLS_VERIFY": os.environ.get("DOCKER_TLS_VERIFY", ""),
                "DOCKER_CERT_PATH": os.environ.get("DOCKER_CERT_PATH", ""),
            },
        )
        input_bytes = json.dumps(request.payload, separators=(",", ":")).encode()
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate_bounded(
                    process,
                    input_bytes,
                    max_output_bytes,
                ),
                timeout=canonical.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise HTTPException(status_code=504, detail="extension timed out") from exc
        except OutputLimitExceededError as exc:
            process.kill()
            await process.wait()
            raise HTTPException(status_code=422, detail="extension output too large") from exc
        if process.returncode != 0:
            raise HTTPException(status_code=502, detail="extension process failed")
        try:
            raw_output = json.loads(stdout)
            catalog.validate_output(canonical.id, raw_output)
            return ExtensionOutput.model_validate(raw_output)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid extension output") from exc

    return app


def run() -> None:
    import uvicorn

    uvicorn.run(
        "jenkins_service.extension_runner:create_runner_app",
        factory=True,
        host="0.0.0.0",
        port=8090,
    )

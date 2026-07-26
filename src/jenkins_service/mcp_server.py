from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .models import (
    CancelRequest,
    ContractValidationRequest,
    ExtensionActionRequest,
    RepositoryCreate,
    RepositoryUpdate,
    RetryRequest,
    ScanRequest,
    TriggerRequest,
)
from .registry import Exposure, OperationRegistry
from .request_context import current_principal


def _output(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_output(item) for item in value]
    return value


def create_mcp_server(service: Any, registry: OperationRegistry) -> FastMCP:
    mcp = FastMCP(
        "JenkinsService",
        instructions=(
            "Curated JenkinsService control plane. Only registry-approved operations "
            "are exposed; raw Jenkins APIs and credentials are unavailable."
        ),
        stateless_http=True,
        json_response=True,
    )

    async def resource(
        operation_id: str,
        *,
        path: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        operation = registry.by_id[operation_id]
        if operation.exposure is not Exposure.RESOURCE:
            raise ValueError(f"operation is not a resource: {operation_id}")
        actor = current_principal.get()
        if not actor.permits(operation.scope):
            raise PermissionError(f"{operation.scope} scope required")
        return _output(
            await registry.invoke(
                operation_id,
                service,
                actor,
                path_params=path,
                query_params=query,
            )
        )

    async def tool(
        operation_id: str,
        payload: Any = None,
        *,
        path: dict[str, str] | None = None,
    ) -> Any:
        operation = registry.by_id[operation_id]
        if operation.exposure is not Exposure.TOOL:
            raise ValueError(f"operation is not a tool: {operation_id}")
        actor = current_principal.get()
        if not actor.permits(operation.scope):
            raise PermissionError(f"{operation.scope} scope required")
        return _output(
            await registry.invoke(
                operation_id,
                service,
                actor,
                payload=payload,
                path_params=path,
            )
        )

    @mcp.resource("jenkinsservice://capabilities")
    async def capabilities() -> Any:
        return await resource("capabilities")

    @mcp.resource("jenkinsservice://contracts/v1/schema")
    async def pipeline_contract_schema() -> Any:
        return await resource("pipeline_contract_schema")

    @mcp.resource("jenkinsservice://contracts/v1/example")
    async def pipeline_contract_example() -> Any:
        return await resource("pipeline_contract_example")

    @mcp.resource("jenkinsservice://repositories")
    async def repositories() -> Any:
        return await resource("repositories")

    @mcp.resource("jenkinsservice://queue")
    async def queue_items() -> Any:
        return await resource("queue_items")

    @mcp.resource("jenkinsservice://builds")
    async def builds() -> Any:
        return await resource("builds")

    @mcp.resource("jenkinsservice://builds/{build_id}")
    async def build(build_id: str) -> Any:
        return await resource("build", path={"build_id": build_id})

    @mcp.resource("jenkinsservice://builds/{build_id}/artifacts/ai-review")
    async def build_ai_review_log(build_id: str) -> Any:
        return await resource("build_ai_review_log", path={"build_id": build_id})

    @mcp.resource("jenkinsservice://builds/{build_id}/logs/{start}")
    async def build_log(build_id: str, start: str) -> Any:
        return await resource(
            "build_log",
            path={"build_id": build_id},
            query={"start": start},
        )

    @mcp.resource("jenkinsservice://extensions")
    async def extension_catalog() -> Any:
        return await resource("extension_catalog")

    @mcp.resource("jenkinsservice://extensions/runs")
    async def extension_runs() -> Any:
        return await resource("extension_runs")

    @mcp.resource("jenkinsservice://webhooks/deliveries")
    async def webhook_deliveries() -> Any:
        return await resource("webhook_deliveries")

    @mcp.resource("jenkinsservice://audit")
    async def audit_records() -> Any:
        return await resource("audit_records")

    @mcp.tool(name="validate_pipeline_contract")
    async def validate_pipeline_contract(
        content: str,
        format: Literal["yaml", "json"] = "yaml",
    ) -> Any:
        return await tool(
            "validate_pipeline_contract",
            ContractValidationRequest(content=content, format=format),
        )

    @mcp.tool(name="register_repository")
    async def register_repository(
        owner: str,
        name: str,
        default_branch: str = "main",
        enabled: bool = True,
    ) -> Any:
        return await tool(
            "register_repository",
            RepositoryCreate(
                owner=owner,
                name=name,
                default_branch=default_branch,
                enabled=enabled,
            ),
        )

    @mcp.tool(name="update_repository")
    async def update_repository(
        repository_id: str,
        default_branch: str | None = None,
        enabled: bool | None = None,
    ) -> Any:
        return await tool(
            "update_repository",
            RepositoryUpdate(default_branch=default_branch, enabled=enabled),
            path={"repository_id": repository_id},
        )

    @mcp.tool(name="unregister_repository")
    async def unregister_repository(repository_id: str) -> Any:
        return await tool(
            "unregister_repository",
            path={"repository_id": repository_id},
        )

    @mcp.tool(name="scan_repository")
    async def scan_repository(repository_id: UUID) -> Any:
        return await tool(
            "scan_repository",
            ScanRequest(repository_id=repository_id),
        )

    @mcp.tool(name="trigger_pipeline")
    async def trigger_pipeline(
        repository_id: UUID,
        commit_sha: str,
        branch: str | None = None,
        pull_request: int | None = None,
    ) -> Any:
        return await tool(
            "trigger_pipeline",
            TriggerRequest(
                repository_id=repository_id,
                commit_sha=commit_sha,
                branch=branch,
                pull_request=pull_request,
            ),
        )

    @mcp.tool(name="retry_pipeline")
    async def retry_pipeline(build_id: UUID) -> Any:
        return await tool("retry_pipeline", RetryRequest(build_id=build_id))

    @mcp.tool(name="cancel_pipeline")
    async def cancel_pipeline(build_id: UUID) -> Any:
        return await tool("cancel_pipeline", CancelRequest(build_id=build_id))

    @mcp.tool(name="run_extension_action")
    async def run_extension_action(
        extension_id: str,
        action: str,
        repository_id: UUID,
        idempotency_key: str,
        build_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Any:
        return await tool(
            "run_extension_action",
            ExtensionActionRequest(
                extension_id=extension_id,
                action=action,
                repository_id=repository_id,
                build_id=build_id,
                inputs=inputs or {},
                idempotency_key=idempotency_key,
            ),
        )

    return mcp

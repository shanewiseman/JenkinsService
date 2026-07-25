from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from .models import (
    Build,
    CancelRequest,
    ContractValidation,
    ContractValidationRequest,
    ExtensionActionRequest,
    ExtensionRun,
    OperationAccepted,
    QueueItem,
    Repository,
    RepositoryCreate,
    RepositoryUpdate,
    RetryRequest,
    ScanRequest,
    Scope,
    ServiceCapabilities,
    TriggerRequest,
)
from .store import ConflictError, NotFoundError


class Exposure(StrEnum):
    RESOURCE = "resource"
    TOOL = "tool"


@dataclass(frozen=True)
class Operation:
    id: str
    http_method: str
    path: str
    handler: str
    scope: Scope
    exposure: Exposure
    summary: str
    input_model: type[BaseModel] | None = None
    response_model: Any = None
    mcp_uri: str | None = None


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "capabilities",
        "GET",
        "/capabilities",
        "capabilities",
        Scope.READ,
        Exposure.RESOURCE,
        "Service capabilities and version",
        response_model=ServiceCapabilities,
        mcp_uri="jenkinsservice://capabilities",
    ),
    Operation(
        "pipeline_contract_schema",
        "GET",
        "/contracts/v1/schema",
        "pipeline_contract_schema",
        Scope.READ,
        Exposure.RESOURCE,
        "Pipeline contract JSON Schema",
        response_model=dict[str, Any],
        mcp_uri="jenkinsservice://contracts/v1/schema",
    ),
    Operation(
        "pipeline_contract_example",
        "GET",
        "/contracts/v1/example",
        "pipeline_contract_example",
        Scope.READ,
        Exposure.RESOURCE,
        "Pipeline contract example",
        response_model=str,
        mcp_uri="jenkinsservice://contracts/v1/example",
    ),
    Operation(
        "validate_pipeline_contract",
        "POST",
        "/contracts/validate",
        "validate_pipeline_contract",
        Scope.READ,
        Exposure.TOOL,
        "Validate a pipeline contract",
        ContractValidationRequest,
        ContractValidation,
    ),
    Operation(
        "repositories",
        "GET",
        "/repositories",
        "repositories",
        Scope.READ,
        Exposure.RESOURCE,
        "Registered repositories",
        response_model=list[Repository],
        mcp_uri="jenkinsservice://repositories",
    ),
    Operation(
        "register_repository",
        "POST",
        "/repositories",
        "register_repository",
        Scope.OPERATE,
        Exposure.TOOL,
        "Register a repository",
        RepositoryCreate,
        Repository,
    ),
    Operation(
        "update_repository",
        "PATCH",
        "/repositories/{repository_id}",
        "update_repository",
        Scope.OPERATE,
        Exposure.TOOL,
        "Update a repository registration",
        RepositoryUpdate,
        Repository,
    ),
    Operation(
        "unregister_repository",
        "DELETE",
        "/repositories/{repository_id}",
        "unregister_repository",
        Scope.ADMIN,
        Exposure.TOOL,
        "Unregister a repository",
        response_model=OperationAccepted,
    ),
    Operation(
        "scan_repository",
        "POST",
        "/repositories/scan",
        "scan_repository",
        Scope.OPERATE,
        Exposure.TOOL,
        "Request a multibranch scan",
        ScanRequest,
        OperationAccepted,
    ),
    Operation(
        "queue_items",
        "GET",
        "/queue",
        "queue_items",
        Scope.READ,
        Exposure.RESOURCE,
        "Jenkins queue items",
        response_model=list[QueueItem],
        mcp_uri="jenkinsservice://queue",
    ),
    Operation(
        "trigger_pipeline",
        "POST",
        "/pipelines/trigger",
        "trigger_pipeline",
        Scope.OPERATE,
        Exposure.TOOL,
        "Trigger a pipeline",
        TriggerRequest,
        QueueItem,
    ),
    Operation(
        "retry_pipeline",
        "POST",
        "/pipelines/retry",
        "retry_pipeline",
        Scope.OPERATE,
        Exposure.TOOL,
        "Retry a pipeline",
        RetryRequest,
        QueueItem,
    ),
    Operation(
        "cancel_pipeline",
        "POST",
        "/pipelines/cancel",
        "cancel_pipeline",
        Scope.OPERATE,
        Exposure.TOOL,
        "Cancel a running pipeline",
        CancelRequest,
        OperationAccepted,
    ),
    Operation(
        "builds",
        "GET",
        "/builds",
        "builds",
        Scope.READ,
        Exposure.RESOURCE,
        "Pipeline builds",
        response_model=list[Build],
        mcp_uri="jenkinsservice://builds",
    ),
    Operation(
        "build",
        "GET",
        "/builds/{build_id}",
        "build",
        Scope.READ,
        Exposure.RESOURCE,
        "Pipeline result and artifact metadata",
        response_model=Build,
        mcp_uri="jenkinsservice://builds/{build_id}",
    ),
    Operation(
        "build_log",
        "GET",
        "/builds/{build_id}/log",
        "build_log",
        Scope.READ,
        Exposure.RESOURCE,
        "Progressive pipeline log",
        response_model=dict[str, Any],
        mcp_uri="jenkinsservice://builds/{build_id}/logs/{start}",
    ),
    Operation(
        "extension_catalog",
        "GET",
        "/extensions",
        "extension_catalog_resource",
        Scope.READ,
        Exposure.RESOURCE,
        "Allowlisted extension catalog",
        response_model=list[dict[str, Any]],
        mcp_uri="jenkinsservice://extensions",
    ),
    Operation(
        "extension_runs",
        "GET",
        "/extensions/runs",
        "extension_runs",
        Scope.READ,
        Exposure.RESOURCE,
        "Extension runs",
        response_model=list[ExtensionRun],
        mcp_uri="jenkinsservice://extensions/runs",
    ),
    Operation(
        "run_extension_action",
        "POST",
        "/extensions/run",
        "run_extension_action",
        Scope.EXTEND,
        Exposure.TOOL,
        "Run an allowlisted extension action",
        ExtensionActionRequest,
        ExtensionRun,
    ),
    Operation(
        "webhook_deliveries",
        "GET",
        "/webhooks/deliveries",
        "webhook_deliveries",
        Scope.READ,
        Exposure.RESOURCE,
        "GitHub webhook delivery audit",
        response_model=list[dict[str, Any]],
        mcp_uri="jenkinsservice://webhooks/deliveries",
    ),
    Operation(
        "audit_records",
        "GET",
        "/audit",
        "audit_records",
        Scope.ADMIN,
        Exposure.RESOURCE,
        "Security audit records",
        response_model=list[dict[str, Any]],
        mcp_uri="jenkinsservice://audit",
    ),
)


class OperationRegistry:
    def __init__(self, operations: tuple[Operation, ...] = OPERATIONS) -> None:
        self.operations = operations
        self.by_id = {operation.id: operation for operation in operations}
        if len(self.by_id) != len(operations):
            raise ValueError("duplicate service operation IDs")

    async def invoke(
        self,
        operation_id: str,
        service: Any,
        principal: Any,
        payload: BaseModel | None = None,
        path_params: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> Any:
        operation = self.by_id[operation_id]
        handler = getattr(service, operation.handler)
        kwargs: dict[str, Any] = {
            **(path_params or {}),
            **(query_params or {}),
        }
        if payload is not None:
            kwargs["payload"] = payload
        return await handler(principal, **kwargs)

    def install_api_routes(self, router: APIRouter, service: Any) -> None:
        for operation in self.operations:
            endpoint = self._endpoint(operation, service)
            openapi_extra = None
            if operation.input_model is not None:
                openapi_extra = {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": operation.input_model.model_json_schema()
                            }
                        },
                    }
                }
            router.add_api_route(
                operation.path,
                endpoint,
                methods=[operation.http_method],
                name=operation.id,
                operation_id=operation.id,
                summary=operation.summary,
                response_model=operation.response_model,
                openapi_extra=openapi_extra,
            )
        service.operation_ids = [operation.id for operation in self.operations]

    def _endpoint(self, operation: Operation, service: Any) -> Any:
        async def endpoint(request: Request) -> Any:
            principal = request.state.principal
            if not principal.permits(operation.scope):
                raise HTTPException(status_code=403, detail=f"{operation.scope} scope required")
            payload = None
            try:
                if operation.input_model is not None:
                    payload = operation.input_model.model_validate(await request.json())
                return await self.invoke(
                    operation.id,
                    service,
                    principal,
                    payload,
                    dict(request.path_params),
                    dict(request.query_params),
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        endpoint.__name__ = f"operation_{operation.id}"
        endpoint.__doc__ = operation.summary
        return endpoint

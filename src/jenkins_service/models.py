from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def validate_branch_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", value):
        raise ValueError("default_branch contains unsupported characters")
    parts = value.split("/")
    if (
        value == "HEAD"
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") for part in parts)
    ):
        raise ValueError("default_branch is not a safe Git branch name")
    return value


def validate_relative_path(value: str) -> str:
    if not value:
        raise ValueError("path must not be empty")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("path must be relative and may not traverse parent directories")
    return value


class Scope(StrEnum):
    READ = "read"
    OPERATE = "operate"
    EXTEND = "extend"
    ADMIN = "admin"


SCOPE_IMPLICATIONS: dict[Scope, set[Scope]] = {
    Scope.READ: {Scope.READ},
    Scope.OPERATE: {Scope.READ, Scope.OPERATE},
    Scope.EXTEND: {Scope.READ, Scope.EXTEND},
    Scope.ADMIN: set(Scope),
}


class Principal(BaseModel):
    token_id: str
    scopes: set[Scope]

    def permits(self, required: Scope) -> bool:
        return any(required in SCOPE_IMPLICATIONS[scope] for scope in self.scopes)


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    default_branch: str = "main"
    enabled: bool = True

    @field_validator("default_branch")
    @classmethod
    def safe_default_branch(cls, value: str) -> str:
        return validate_branch_name(value)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class RepositoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_branch: str | None = None
    enabled: bool | None = None

    @field_validator("default_branch")
    @classmethod
    def safe_default_branch(cls, value: str | None) -> str | None:
        return validate_branch_name(value) if value is not None else None


class Repository(RepositoryCreate):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


ReviewSeverity = Literal["critical"]


def _critical_review_severities() -> set[ReviewSeverity]:
    return {"critical"}


class ReviewPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    pull_requests_only: Literal[True] = True
    critical_severities: set[ReviewSeverity] = Field(
        default_factory=_critical_review_severities,
        min_length=1,
        max_length=1,
    )
    max_diff_bytes: int = Field(default=200_000, ge=1024, le=1_000_000)


class AIReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["github_pull_request_review", "github_commit_status"]
    status: Literal["performed", "not_performed", "failed"]
    detail: str = Field(min_length=1, max_length=500)
    response_id: str | None = Field(default=None, max_length=200)


class AIReviewLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ci.jenkinsservice.dev/ai-review-log/v1"] = (
        "ci.jenkinsservice.dev/ai-review-log/v1"
    )
    build_id: UUID
    review_run_id: UUID
    repository: str
    pull_request: int | None = Field(default=None, ge=1)
    base_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    head_sha: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    model: str
    prompt_version: str
    outcome: Literal["passed", "failed", "skipped"]
    summary: str = Field(min_length=1, max_length=4_000)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    actions: list[AIReviewAction] = Field(default_factory=list, max_length=20)
    failure_reason: str | None = Field(default=None, max_length=2_000)
    started_at: datetime
    completed_at: datetime


class QueueItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    repository_id: UUID
    build_id: UUID | None = None
    jenkins_queue_id: int | None = None
    state: Literal["queued", "started", "reused", "cancelled", "failed"] = "queued"
    commit_sha: str
    base_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    branch: str | None = None
    pull_request: int | None = None
    created_at: datetime = Field(default_factory=now_utc)

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value: str | None) -> str | None:
        return validate_branch_name(value) if value is not None else None


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: Literal["passed", "failed", "warning", "skipped"]
    required: bool
    exit_code: int | None = None
    duration_seconds: float = Field(ge=0)
    reports: list[str] = Field(default_factory=list)

    @field_validator("reports")
    @classmethod
    def safe_report_paths(cls, values: list[str]) -> list[str]:
        return [validate_relative_path(value) for value in values]


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    download_url: HttpUrl | None = None

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return validate_relative_path(value)


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ci.jenkinsservice.dev/result/v1"] = "ci.jenkinsservice.dev/result/v1"
    repository: str
    commit_sha: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    trusted_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    base_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    build_id: UUID | None = None
    pull_request: int | None = None
    status: Literal["queued", "running", "passed", "failed", "cancelled"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    extension_runs: list[UUID] = Field(default_factory=list)


class Build(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    repository_id: UUID
    jenkins_job: str
    queue_item_id: UUID | None = None
    jenkins_queue_id: int | None = None
    jenkins_build_number: int | None = None
    branch: str | None = None
    reused_from_build_id: UUID | None = None
    review_policy: ReviewPolicy | None = None
    ai_review_log: AIReviewLog | None = None
    result: PipelineResult
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value: str | None) -> str | None:
        return validate_branch_name(value) if value is not None else None


class ContractValidation(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    normalized: dict[str, Any] | None = None


class ContractValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    format: Literal["yaml", "json"] = "yaml"


class TriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    commit_sha: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    base_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    branch: str | None = None
    pull_request: int | None = Field(default=None, ge=1)

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value: str | None) -> str | None:
        return validate_branch_name(value) if value is not None else None


class BuildCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_id: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
    timestamp: int = Field(ge=0)
    build_id: UUID
    result: PipelineResult
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    diff: str | None = Field(default=None, max_length=1_000_000)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: UUID


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: UUID


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID


class ExtensionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extension_id: str
    action: str
    repository_id: UUID
    build_id: UUID | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ci.jenkinsservice.dev/extension/v1"]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    image: str
    compatible_contracts: list[Literal["ci.jenkinsservice.dev/v1"]]
    actions: list[str]
    input_schema: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.json$")
    output_schema: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.json$")
    github_actions: list[Literal["review_comment", "issue_comment", "check_run", "status"]] = Field(
        default_factory=list
    )
    required_secrets: list[str] = Field(default_factory=list)
    network: Literal["none"] = "none"
    timeout_seconds: int = Field(default=120, ge=1, le=900)
    memory_mb: int = Field(default=256, ge=64, le=4096)
    cpus: float = Field(default=0.5, gt=0, le=4)

    @field_validator("image")
    @classmethod
    def immutable_image(cls, value: str) -> str:
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/:+\-]*@sha256:[a-f0-9]{64}",
            value,
        ):
            raise ValueError("extension image must be pinned by sha256 digest")
        return value


class ExtensionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=500,
    )
    requested_github_actions: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=100,
    )
    review_log: AIReviewLog | None = None


class ExtensionRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    extension_id: str
    image_digest: str
    action: str
    target: str
    idempotency_key: str
    status: Literal["running", "succeeded", "failed", "skipped"] = "running"
    output: ExtensionOutput | None = None
    created_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None


class WebhookDelivery(BaseModel):
    delivery_id: str
    event: str
    repository: str
    accepted: bool
    reason: str | None = None
    received_at: datetime = Field(default_factory=now_utc)


class AuditRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    actor: str
    action: str
    target: str
    request_id: str
    outcome: Literal["accepted", "rejected", "succeeded", "failed"]
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class ServiceCapabilities(BaseModel):
    service_version: str
    contract_versions: list[str]
    operations: list[str]
    transport: Literal["streamable-http"] = "streamable-http"


class OperationAccepted(BaseModel):
    accepted: bool = True
    id: str
    detail: str

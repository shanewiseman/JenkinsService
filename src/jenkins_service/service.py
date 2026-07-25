from __future__ import annotations

from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from .clients import GitHubClient, JenkinsClient, UpstreamError
from .contract import contract_schema, validate_contract_content
from .extensions import ExtensionCatalog, ExtensionRunnerClient
from .models import (
    AuditRecord,
    Build,
    CancelRequest,
    ContractValidation,
    ContractValidationRequest,
    ExtensionActionRequest,
    ExtensionRun,
    OperationAccepted,
    PipelineResult,
    Principal,
    QueueItem,
    Repository,
    RepositoryCreate,
    RepositoryUpdate,
    RetryRequest,
    ScanRequest,
    ServiceCapabilities,
    TriggerRequest,
    now_utc,
)
from .request_context import current_request_id
from .security import Redactor
from .store import NotFoundError, Store

AuditOutcome = Literal["accepted", "rejected", "succeeded", "failed"]


class JenkinsService:
    def __init__(
        self,
        *,
        store: Store,
        jenkins: JenkinsClient,
        github: GitHubClient,
        extension_runner: ExtensionRunnerClient,
        extension_catalog: ExtensionCatalog,
        github_allowlist: list[str],
        service_version: str,
        max_artifact_bytes: int = 100_000_000,
        redactor: Redactor | None = None,
    ) -> None:
        self.store = store
        self.jenkins = jenkins
        self.github = github
        self.extension_runner = extension_runner
        self.extension_catalog = extension_catalog
        self.github_allowlist = github_allowlist
        self.service_version = service_version
        self.max_artifact_bytes = max_artifact_bytes
        self.redactor = redactor or Redactor([])
        self.operation_ids: list[str] = []

    def repository_allowed(self, full_name: str) -> bool:
        lowered = full_name.lower()
        return any(
            lowered == rule.lower()
            or (rule.endswith("/*") and lowered.startswith(rule[:-1].lower()))
            for rule in self.github_allowlist
        )

    async def _audit(
        self,
        principal: Principal,
        action: str,
        target: str,
        outcome: AuditOutcome = "succeeded",
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self.store.record_audit(
            AuditRecord(
                actor=principal.token_id,
                action=action,
                target=target,
                request_id=current_request_id.get(),
                outcome=outcome,
                detail=detail or {},
            )
        )

    async def capabilities(
        self,
        principal: Principal,
    ) -> ServiceCapabilities:
        return ServiceCapabilities(
            service_version=self.service_version,
            contract_versions=["ci.jenkinsservice.dev/v1"],
            operations=self.operation_ids,
        )

    async def pipeline_contract_schema(
        self,
        principal: Principal,
    ) -> dict[str, Any]:
        return contract_schema()

    async def pipeline_contract_example(
        self,
        principal: Principal,
    ) -> str:
        installed = Path("/app/examples/pipeline.yaml")
        source = Path(__file__).parents[2] / "examples" / "pipeline.yaml"
        packaged = files("jenkins_service").joinpath("examples/pipeline.yaml")
        if installed.exists():
            return installed.read_text(encoding="utf-8")
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8")
        return source.read_text(encoding="utf-8")

    async def validate_pipeline_contract(
        self,
        principal: Principal,
        payload: ContractValidationRequest,
    ) -> ContractValidation:
        result = validate_contract_content(
            payload.content,
            payload.format,
        )
        await self._audit(
            principal,
            "validate_pipeline_contract",
            "inline-contract",
            "succeeded" if result.valid else "rejected",
            {"errors": result.errors},
        )
        return result

    async def register_repository(
        self,
        principal: Principal,
        payload: RepositoryCreate,
    ) -> Repository:
        if not self.repository_allowed(payload.full_name):
            await self._audit(
                principal,
                "register_repository",
                payload.full_name,
                "rejected",
                {"reason": "outside configured allowlist"},
            )
            raise ValueError("repository is outside the configured allowlist")
        result = await self.store.create_repository(payload)
        try:
            await self.jenkins.ensure_job(
                result.owner,
                result.name,
                result.default_branch,
            )
        except Exception:
            await self.store.delete_repository(result.id)
            await self._audit(
                principal,
                "register_repository",
                result.full_name,
                "failed",
                {"reason": "Jenkins reconciliation failed"},
            )
            raise
        await self._audit(
            principal,
            "register_repository",
            result.full_name,
        )
        return result

    async def update_repository(
        self,
        principal: Principal,
        payload: RepositoryUpdate,
        repository_id: str,
    ) -> Repository:
        current = await self.store.get_repository(UUID(repository_id))
        desired = current.model_copy(update=payload.model_dump(exclude_none=True))
        await self.jenkins.ensure_job(
            desired.owner,
            desired.name,
            desired.default_branch,
        )
        result = await self.store.update_repository(
            current.id,
            payload,
        )
        await self._audit(
            principal,
            "update_repository",
            result.full_name,
        )
        return result

    async def unregister_repository(
        self,
        principal: Principal,
        repository_id: str,
    ) -> OperationAccepted:
        repository = await self.store.get_repository(UUID(repository_id))
        await self.jenkins.delete_job(
            repository.owner,
            repository.name,
        )
        await self.store.delete_repository(repository.id)
        await self._audit(
            principal,
            "unregister_repository",
            repository.full_name,
        )
        return OperationAccepted(
            id=str(repository.id),
            detail="repository unregistered",
        )

    async def repositories(
        self,
        principal: Principal,
    ) -> list[Repository]:
        return await self.store.list_repositories()

    async def scan_repository(
        self,
        principal: Principal,
        payload: ScanRequest,
    ) -> OperationAccepted:
        repository = await self.store.get_repository(payload.repository_id)
        await self.jenkins.scan(
            repository.owner,
            repository.name,
        )
        await self._audit(
            principal,
            "scan_repository",
            repository.full_name,
        )
        return OperationAccepted(
            id=str(repository.id),
            detail="scan requested",
        )

    async def trigger_pipeline(
        self,
        principal: Principal,
        payload: TriggerRequest,
    ) -> QueueItem:
        repository = await self.store.get_repository(payload.repository_id)
        queue_id = await self.jenkins.trigger(
            repository.owner,
            repository.name,
            payload.commit_sha,
            payload.pull_request,
        )
        queue = QueueItem(
            repository_id=repository.id,
            jenkins_queue_id=queue_id,
            commit_sha=payload.commit_sha,
            pull_request=payload.pull_request,
        )
        await self.store.save_queue_item(queue)
        result = PipelineResult(
            repository=repository.full_name,
            commit_sha=payload.commit_sha,
            pull_request=payload.pull_request,
            status="queued",
        )
        await self.store.save_build(
            Build(
                repository_id=repository.id,
                jenkins_job=(
                    "repositories/"
                    + self.jenkins.job_name(
                        repository.owner,
                        repository.name,
                    )
                ),
                queue_item_id=queue.id,
                jenkins_queue_id=queue_id,
                result=result,
            )
        )
        await self._audit(
            principal,
            "trigger_pipeline",
            repository.full_name,
            detail={"queue": str(queue.id)},
        )
        return queue

    async def retry_pipeline(
        self,
        principal: Principal,
        payload: RetryRequest,
    ) -> QueueItem:
        build = await self.store.get_build(payload.build_id)
        trigger = TriggerRequest(
            repository_id=build.repository_id,
            commit_sha=build.result.commit_sha,
            pull_request=build.result.pull_request,
        )
        result = await self.trigger_pipeline(
            principal,
            trigger,
        )
        await self._audit(
            principal,
            "retry_pipeline",
            str(payload.build_id),
            detail={"queue": str(result.id)},
        )
        return result

    async def cancel_pipeline(
        self,
        principal: Principal,
        payload: CancelRequest,
    ) -> OperationAccepted:
        build = await self._refresh_build(await self.store.get_build(payload.build_id))
        if build.jenkins_build_number is None:
            if build.jenkins_queue_id is None:
                raise ValueError("build has no Jenkins queue or build identifier")
            await self.jenkins.cancel_queue(build.jenkins_queue_id)
        else:
            await self.jenkins.cancel(
                build.jenkins_job,
                build.jenkins_build_number,
            )
        build.result.status = "cancelled"
        build.result.completed_at = now_utc()
        build.updated_at = now_utc()
        await self.store.save_build(build)
        await self._set_queue_state(build, "cancelled")
        await self._audit(
            principal,
            "cancel_pipeline",
            str(build.id),
        )
        return OperationAccepted(
            id=str(build.id),
            detail="cancellation requested",
        )

    async def queue_items(
        self,
        principal: Principal,
    ) -> list[QueueItem]:
        return await self.store.list_queue_items()

    async def builds(
        self,
        principal: Principal,
    ) -> list[Build]:
        builds = await self.store.list_builds()
        refreshed: list[Build] = []
        for build in builds:
            if build.result.status not in {"queued", "running"}:
                refreshed.append(build)
                continue
            try:
                refreshed.append(await self._refresh_build(build))
            except UpstreamError:
                refreshed.append(build)
        return refreshed

    async def build(
        self,
        principal: Principal,
        build_id: str,
    ) -> Build:
        return await self._refresh_build(await self.store.get_build(UUID(build_id)))

    async def build_log(
        self,
        principal: Principal,
        build_id: str,
        start: str = "0",
    ) -> dict[str, Any]:
        build = await self._refresh_build(await self.store.get_build(UUID(build_id)))
        if build.jenkins_build_number is None:
            return {
                "text": "",
                "next": 0,
                "more": build.result.status in {"queued", "running"},
            }
        offset = int(start)
        if offset < 0:
            raise ValueError("log start offset must be non-negative")
        text, next_offset, more = await self.jenkins.progressive_log(
            build.jenkins_job,
            build.jenkins_build_number,
            offset,
        )
        return {
            "text": self.redactor.redact(text),
            "next": next_offset,
            "more": more,
        }

    async def _set_queue_state(
        self,
        build: Build,
        state: Literal["queued", "started", "cancelled", "failed"],
    ) -> None:
        if build.queue_item_id is None:
            return
        try:
            queue = await self.store.get_queue_item(build.queue_item_id)
        except NotFoundError:
            return
        queue.state = state
        await self.store.save_queue_item(queue)

    async def _refresh_build(self, build: Build) -> Build:
        if build.result.status not in {"queued", "running"}:
            return build
        if build.jenkins_build_number is None and build.jenkins_queue_id is not None:
            queue_status = await self.jenkins.queue_status(build.jenkins_queue_id)
            if queue_status and queue_status.get("cancelled"):
                build.result.status = "cancelled"
                build.result.completed_at = now_utc()
                build.updated_at = now_utc()
                await self.store.save_build(build)
                await self._set_queue_state(build, "cancelled")
                return build
            executable = (queue_status or {}).get("executable")
            if isinstance(executable, dict) and isinstance(executable.get("number"), int):
                build.jenkins_build_number = executable["number"]
                build.result.status = "running"
                build.updated_at = now_utc()
                await self.store.save_build(build)
                await self._set_queue_state(build, "started")
        if build.jenkins_build_number is None:
            return build

        state = await self.jenkins.build_status(
            build.jenkins_job,
            build.jenkins_build_number,
        )
        if state is None:
            return build
        timestamp = state.get("timestamp")
        if isinstance(timestamp, int):
            build.result.started_at = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
        if state.get("building"):
            build.result.status = "running"
            build.updated_at = now_utc()
            await self.store.save_build(build)
            return build

        raw_result = await self.jenkins.pipeline_result(
            build.jenkins_job,
            build.jenkins_build_number,
            self.max_artifact_bytes,
        )
        if raw_result is not None:
            try:
                pipeline_result = PipelineResult.model_validate_json(raw_result)
            except ValueError as exc:
                raise UpstreamError("Jenkins returned an invalid PipelineResult") from exc
            if (
                pipeline_result.repository != build.result.repository
                or pipeline_result.commit_sha.lower() != build.result.commit_sha.lower()
                or pipeline_result.pull_request != build.result.pull_request
            ):
                raise UpstreamError("Jenkins PipelineResult identity does not match the build")
            build.result = pipeline_result
        else:
            jenkins_result = str(state.get("result") or "FAILURE").upper()
            build.result.status = (
                "passed"
                if jenkins_result == "SUCCESS"
                else "cancelled"
                if jenkins_result == "ABORTED"
                else "failed"
            )
            build.result.completed_at = now_utc()
        build.updated_at = now_utc()
        await self.store.save_build(build)
        await self._set_queue_state(
            build,
            "cancelled"
            if build.result.status == "cancelled"
            else "failed"
            if build.result.status == "failed"
            else "started",
        )
        return build

    async def run_extension_action(
        self,
        principal: Principal,
        payload: ExtensionActionRequest,
    ) -> ExtensionRun:
        previous = await self.store.get_extension_run_by_key(payload.idempotency_key)
        if previous is not None:
            return previous
        repository = await self.store.get_repository(payload.repository_id)
        manifest = self.extension_catalog.get(payload.extension_id)
        run = ExtensionRun(
            extension_id=manifest.id,
            image_digest=manifest.image.rsplit("@", 1)[1],
            action=payload.action,
            target=repository.full_name,
            idempotency_key=payload.idempotency_key,
        )
        stored = await self.store.save_extension_run(run)
        if stored.id != run.id:
            return stored
        try:
            output = await self.extension_runner.run(
                manifest,
                payload.action,
                {
                    "repository": repository.model_dump(mode="json"),
                    "build_id": (str(payload.build_id) if payload.build_id else None),
                    "inputs": payload.inputs,
                },
            )
            self.extension_catalog.validate_output(
                manifest.id,
                output.model_dump(mode="json"),
            )
            for action_request in output.requested_github_actions:
                action_type = action_request.get("type", "")
                response = await self.github.execute_action(
                    repository.full_name,
                    action_type,
                    action_request,
                    set(manifest.github_actions),
                )
                await self._audit(
                    principal,
                    f"github.{action_type}",
                    repository.full_name,
                    detail={
                        "extension": manifest.id,
                        "image": manifest.image,
                        "idempotency_key": (payload.idempotency_key),
                        "response_id": response.get("id"),
                    },
                )
            run.output = output
            run.status = "succeeded"
            run.completed_at = now_utc()
            await self.store.save_extension_run(run)
        except Exception:
            run.status = "failed"
            run.completed_at = now_utc()
            await self.store.save_extension_run(run)
            await self._audit(
                principal,
                "run_extension_action",
                repository.full_name,
                "failed",
            )
            raise
        await self._audit(
            principal,
            "run_extension_action",
            repository.full_name,
        )
        return run

    async def extension_catalog_resource(
        self,
        principal: Principal,
    ) -> list[dict[str, Any]]:
        return [manifest.model_dump(mode="json") for manifest in self.extension_catalog.list()]

    async def extension_runs(
        self,
        principal: Principal,
    ) -> list[ExtensionRun]:
        return await self.store.list_extension_runs()

    async def webhook_deliveries(
        self,
        principal: Principal,
    ) -> list[Any]:
        return await self.store.list_webhooks()

    async def audit_records(
        self,
        principal: Principal,
    ) -> list[AuditRecord]:
        return await self.store.list_audit()

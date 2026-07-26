from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from .clients import GitHubClient, JenkinsClient, ReviewBrokerClient, UpstreamError
from .contract import contract_schema, validate_contract_content
from .extensions import ExtensionCatalog, ExtensionRunnerClient
from .models import (
    AuditRecord,
    Build,
    BuildCompletion,
    CancelRequest,
    ContractValidation,
    ContractValidationRequest,
    ExtensionActionRequest,
    ExtensionOutput,
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
    WebhookDelivery,
    now_utc,
)
from .request_context import current_request_id
from .review import DiffValidationError, parse_unified_diff
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
        review_broker: ReviewBrokerClient | None = None,
        extension_runner: ExtensionRunnerClient,
        extension_catalog: ExtensionCatalog,
        github_allowlist: list[str],
        service_version: str,
        max_artifact_bytes: int = 100_000_000,
        public_base_url: str | None = None,
        openai_model: str = "gpt-5.6-terra",
        review_prompt_version: str = "v1",
        redactor: Redactor | None = None,
    ) -> None:
        self.store = store
        self.jenkins = jenkins
        self.github = github
        self.review_broker = review_broker
        self.extension_runner = extension_runner
        self.extension_catalog = extension_catalog
        self.github_allowlist = github_allowlist
        self.service_version = service_version
        self.max_artifact_bytes = max_artifact_bytes
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.openai_model = openai_model
        self.review_prompt_version = review_prompt_version
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

    async def _publish_ci_status(
        self,
        principal: Principal,
        repository: str,
        commit_sha: str,
        context: str,
        state: str,
        description: str,
        build_id: UUID,
    ) -> bool:
        target_url = (
            f"{self.public_base_url}/api/v1/builds/{build_id}" if self.public_base_url else None
        )
        last_error: UpstreamError | None = None
        for attempt in range(3):
            try:
                await self.github.set_status(
                    repository,
                    commit_sha,
                    context=context,
                    state=state,
                    description=description,
                    target_url=target_url,
                )
                return True
            except UpstreamError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        await self._audit(
            principal,
            "github.status",
            repository,
            "failed",
            {"context": context, "state": state, "reason": str(last_error)},
        )
        return False

    async def trigger_pipeline(
        self,
        principal: Principal,
        payload: TriggerRequest,
    ) -> QueueItem:
        repository = await self.store.get_repository(payload.repository_id)
        result = PipelineResult(
            repository=repository.full_name,
            commit_sha=payload.commit_sha,
            base_sha=payload.base_sha,
            pull_request=payload.pull_request,
            status="queued",
        )
        build = Build(
            repository_id=repository.id,
            jenkins_job=(
                "repositories/"
                + self.jenkins.job_name(
                    repository.owner,
                    repository.name,
                )
            ),
            result=result,
        )
        build.result.build_id = build.id
        queue_id = await self.jenkins.trigger(
            repository.owner,
            repository.name,
            payload.commit_sha,
            payload.pull_request,
            payload.base_sha,
            str(build.id),
        )
        queue = QueueItem(
            repository_id=repository.id,
            build_id=build.id,
            jenkins_queue_id=queue_id,
            commit_sha=payload.commit_sha,
            base_sha=payload.base_sha,
            pull_request=payload.pull_request,
        )
        build.queue_item_id = queue.id
        build.jenkins_queue_id = queue_id
        await self.store.save_queue_item(queue)
        await self.store.save_build(build)
        await self._publish_ci_status(
            principal,
            repository.full_name,
            payload.commit_sha,
            "jenkinsservice/native-ci",
            "pending",
            "Native Jenkins validation is queued",
            build.id,
        )
        await self._publish_ci_status(
            principal,
            repository.full_name,
            payload.commit_sha,
            "jenkinsservice/ai-review",
            "pending",
            "Critical-only AI review is queued",
            build.id,
        )
        await self._audit(
            principal,
            "trigger_pipeline",
            repository.full_name,
            detail={"queue": str(queue.id), "build": str(build.id)},
        )
        return queue

    async def complete_build(
        self,
        principal: Principal,
        completion: BuildCompletion,
    ) -> bool:
        build = await self.store.get_build(completion.build_id)
        expected = build.result
        result = completion.result
        if (
            result.build_id != build.id
            or result.repository.lower() != expected.repository.lower()
            or result.commit_sha.lower() != expected.commit_sha.lower()
            or (result.base_sha or "").lower() != (expected.base_sha or "").lower()
            or result.pull_request != expected.pull_request
        ):
            raise ValueError("build completion identity does not match the queued build")
        if result.status not in {"passed", "failed", "cancelled"}:
            raise ValueError("build completion must contain a terminal result")
        delivery = WebhookDelivery(
            delivery_id=f"build-completion:{completion.callback_id}",
            event="build_completion",
            repository=result.repository,
            accepted=True,
        )
        if any(
            item.delivery_id == delivery.delivery_id for item in await self.store.list_webhooks()
        ):
            return False
        build.result = result
        build.updated_at = now_utc()
        await self.store.save_build(build)
        published = await self._publish_ci_status(
            principal,
            result.repository,
            result.commit_sha,
            "jenkinsservice/native-ci",
            "success" if result.status == "passed" else "failure",
            "Native Jenkins validation passed"
            if result.status == "passed"
            else f"Native Jenkins validation {result.status}",
            build.id,
        )
        if not published:
            raise UpstreamError("native CI status publication failed after bounded retries")
        if not await self.store.record_webhook_once(delivery):
            return False
        await self._set_queue_state(
            build,
            "cancelled"
            if result.status == "cancelled"
            else "failed"
            if result.status == "failed"
            else "started",
        )
        await self._audit(
            principal,
            "build_completion",
            str(build.id),
            detail={"status": result.status, "callback_id": completion.callback_id},
        )
        return True

    async def process_ai_review(
        self,
        principal: Principal,
        completion: BuildCompletion,
    ) -> None:
        result = completion.result
        policy = completion.review
        if not policy.enabled or result.pull_request is None:
            await self._publish_ci_status(
                principal,
                result.repository,
                result.commit_sha,
                "jenkinsservice/ai-review",
                "success",
                "AI review is not required for this build",
                completion.build_id,
            )
            return
        if result.base_sha is None or completion.diff is None or self.review_broker is None:
            await self._publish_ci_status(
                principal,
                result.repository,
                result.commit_sha,
                "jenkinsservice/ai-review",
                "failure",
                "AI review prerequisites are unavailable",
                completion.build_id,
            )
            return

        identity = "|".join(
            [
                result.repository.lower(),
                str(result.pull_request),
                result.commit_sha.lower(),
                self.openai_model,
                self.review_prompt_version,
            ]
        )
        idempotency_key = "ai-review:" + hashlib.sha256(identity.encode()).hexdigest()
        previous = await self.store.get_extension_run_by_key(idempotency_key)
        if previous is not None:
            blocking = bool(
                previous.output
                and previous.output.requested_github_actions
                and previous.output.requested_github_actions[0].get("blocking")
            )
            state = (
                "pending"
                if previous.status == "running"
                else "failure"
                if previous.status == "failed" or blocking
                else "success"
            )
            await self._publish_ci_status(
                principal,
                result.repository,
                result.commit_sha,
                "jenkinsservice/ai-review",
                state,
                "Deduplicated critical-only AI review",
                completion.build_id,
            )
            return

        run = ExtensionRun(
            extension_id="openai-review",
            image_digest=self.openai_model,
            action=self.review_prompt_version,
            target=f"{result.repository}#{result.pull_request}",
            idempotency_key=idempotency_key,
        )
        stored = await self.store.save_extension_run(run)
        if stored.id != run.id:
            return
        try:
            diff = completion.diff
            if len(diff.encode("utf-8")) > policy.max_diff_bytes:
                raise DiffValidationError("trusted review diff exceeds configured byte limit")
            parsed = parse_unified_diff(diff)
            broker = await self.review_broker.review(
                repository=result.repository,
                pull_request=result.pull_request,
                base_sha=result.base_sha,
                head_sha=result.commit_sha,
                diff=diff,
            )
            if (
                broker.model != self.openai_model
                or broker.prompt_version != self.review_prompt_version
            ):
                raise ValueError("review broker identity does not match configured model/prompt")
            parsed.validate_result(broker.review)
            blocking = any(
                finding.severity.value in policy.critical_severities
                for finding in broker.review.findings
            )
            comments = [
                {
                    "path": finding.path,
                    "line": finding.line,
                    "side": "RIGHT",
                    "body": (
                        f"**{finding.severity.value.upper()}: {finding.title}**\n\n{finding.body}"
                    ),
                }
                for finding in broker.review.findings
            ]
            review_response = await self.github.create_review(
                result.repository,
                result.pull_request,
                commit_sha=result.commit_sha,
                body=broker.review.summary,
                comments=comments,
            )
            run.output = ExtensionOutput(
                findings=[item.model_dump(mode="json") for item in broker.review.findings],
                requested_github_actions=[
                    {
                        "blocking": blocking,
                        "review_id": review_response.get("id"),
                        "summary": broker.review.summary,
                    }
                ],
            )
            run.status = "succeeded"
            run.completed_at = now_utc()
            await self.store.save_extension_run(run)
            await self._publish_ci_status(
                principal,
                result.repository,
                result.commit_sha,
                "jenkinsservice/ai-review",
                "failure" if blocking else "success",
                "Critical AI review finding requires changes"
                if blocking
                else "AI review found no critical issues",
                completion.build_id,
            )
        except (UpstreamError, DiffValidationError, ValueError) as exc:
            run.status = "failed"
            run.completed_at = now_utc()
            await self.store.save_extension_run(run)
            await self._publish_ci_status(
                principal,
                result.repository,
                result.commit_sha,
                "jenkinsservice/ai-review",
                "failure",
                "AI review failed after bounded retries",
                completion.build_id,
            )
            await self._audit(
                principal,
                "ai_review",
                run.target,
                "failed",
                {"reason": str(exc), "idempotency_key": idempotency_key},
            )

    async def retry_pipeline(
        self,
        principal: Principal,
        payload: RetryRequest,
    ) -> QueueItem:
        build = await self.store.get_build(payload.build_id)
        trigger = TriggerRequest(
            repository_id=build.repository_id,
            commit_sha=build.result.commit_sha,
            base_sha=build.result.base_sha,
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

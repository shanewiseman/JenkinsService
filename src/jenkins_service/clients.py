from __future__ import annotations

import asyncio
import hashlib
import re
from base64 import b64encode
from dataclasses import dataclass
from html import escape
from typing import Any, cast
from urllib.parse import quote

import httpx

from .review import ReviewResult


class UpstreamError(RuntimeError):
    pass


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        github_web_url: str = "https://github.com",
        reconcile_delay_seconds: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, token)
        self.client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self.github_web_url = github_web_url.rstrip("/")
        self.reconcile_delay_seconds = reconcile_delay_seconds
        self._repository_locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _crumb(self) -> dict[str, str]:
        response = await self.client.get(f"{self.base_url}/crumbIssuer/api/json", auth=self.auth)
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        value = response.json()
        return {value["crumbRequestField"]: value["crumb"]}

    async def _post(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                auth=self.auth,
                headers=await self._crumb(),
                params=params,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Jenkins request failed: {path}") from exc

    @staticmethod
    def job_name(owner: str, name: str) -> str:
        return f"{owner}--{name}"

    @staticmethod
    def legacy_job_name(owner: str, name: str) -> str:
        return f"{JenkinsClient.job_name(owner, name)}--legacy"

    @staticmethod
    def branch_job_name(branch: str, pull_request: int | None = None) -> str:
        if pull_request is not None:
            return f"PR-{pull_request}"
        slug = branch.replace("/", "-")[:120]
        digest = hashlib.sha256(branch.encode()).hexdigest()[:16]
        return f"branch-{slug}-{digest}"

    @classmethod
    def managed_job_path(
        cls,
        owner: str,
        name: str,
        branch: str,
        pull_request: int | None = None,
    ) -> str:
        return (
            f"repositories/{cls.job_name(owner, name)}/{cls.branch_job_name(branch, pull_request)}"
        )

    @classmethod
    def legacy_job_path(cls, owner: str, name: str) -> str:
        return f"repositories/{cls.legacy_job_name(owner, name)}"

    @staticmethod
    def _job_path(job: str) -> str:
        return "/job/".join(quote(part, safe="") for part in job.split("/"))

    @staticmethod
    def _groovy_single_quoted(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    async def _item(self, path: str) -> httpx.Response:
        return await self.client.get(
            f"{self.base_url}/job/{self._job_path(path)}/api/json",
            params={"tree": "_class"},
            auth=self.auth,
        )

    async def _item_class(self, path: str) -> str | None:
        response = await self._item(path)
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
            item_class = response.json().get("_class")
        except (httpx.HTTPError, AttributeError, ValueError) as exc:
            raise UpstreamError(f"Jenkins item state is unavailable: {path}") from exc
        if not isinstance(item_class, str) or not item_class:
            raise UpstreamError(f"Jenkins item has no class metadata: {path}")
        return item_class

    async def _reconcile_pause(self, attempt: int) -> None:
        if self.reconcile_delay_seconds > 0:
            await asyncio.sleep(self.reconcile_delay_seconds * (2**attempt))

    async def _create_item(self, parent: str, name: str, xml: str) -> None:
        headers = {**await self._crumb(), "Content-Type": "application/xml"}
        response = await self.client.post(
            f"{self.base_url}/job/{self._job_path(parent)}/createItem",
            auth=self.auth,
            headers=headers,
            params={"name": name},
            content=xml,
        )
        response.raise_for_status()

    async def _ensure_repository_folder(self, owner: str, name: str) -> None:
        folder_class = "com.cloudbees.hudson.plugins.folder.Folder"
        workflow_class = "org.jenkinsci.plugins.workflow.job.WorkflowJob"
        attempts = 6
        repository_name = self.job_name(owner, name)
        repository_path = f"repositories/{repository_name}"
        legacy_path = f"repositories/{self.legacy_job_name(owner, name)}"
        item_class = await self._item_class(repository_path)
        if item_class == folder_class:
            return
        if item_class not in {None, workflow_class}:
            raise UpstreamError(
                f"repository Jenkins path has an unexpected item type: {repository_path}"
            )
        if item_class == workflow_class:
            legacy_class = await self._item_class(legacy_path)
            if legacy_class not in {None, workflow_class}:
                raise UpstreamError(
                    f"legacy Jenkins path has an unexpected item type: {legacy_path}"
                )
            if legacy_class is None:
                await self._post(
                    f"/job/{self._job_path(repository_path)}/doRename",
                    params={"newName": self.legacy_job_name(owner, name)},
                )

            for attempt in range(attempts):
                current_class = await self._item_class(repository_path)
                legacy_class = await self._item_class(legacy_path)
                if current_class == folder_class:
                    return
                if current_class is None and legacy_class == workflow_class:
                    break
                if current_class not in {None, workflow_class}:
                    raise UpstreamError(
                        f"repository Jenkins path changed to an unexpected item type: "
                        f"{repository_path}"
                    )
                if legacy_class not in {None, workflow_class}:
                    raise UpstreamError(
                        f"legacy Jenkins path changed to an unexpected item type: {legacy_path}"
                    )
                await self._reconcile_pause(attempt)
            else:
                raise UpstreamError(
                    f"Jenkins legacy job migration did not settle for {owner}/{name}"
                )

        folder_xml = (
            "<?xml version='1.1' encoding='UTF-8'?>"
            "<com.cloudbees.hudson.plugins.folder.Folder plugin='cloudbees-folder'>"
            "<actions/><description>Managed repository branches. Do not edit.</description>"
            "<properties/><folderViews class='com.cloudbees.hudson.plugins.folder.views."
            "DefaultFolderViewHolder'><views><hudson.model.AllView><owner class='"
            "com.cloudbees.hudson.plugins.folder.Folder' reference='../../../..'/>"
            "<name>All</name><filterExecutors>false</filterExecutors>"
            "<filterQueue>false</filterQueue><properties class='hudson.model."
            "View$PropertyList'/></hudson.model.AllView></views>"
            "<tabBar class='hudson.views.DefaultViewsTabBar'/></folderViews>"
            "<healthMetrics/><icon class='com.cloudbees.hudson.plugins.folder.icons."
            "StockFolderIcon'/></com.cloudbees.hudson.plugins.folder.Folder>"
        )
        last_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            current_class = await self._item_class(repository_path)
            if current_class == folder_class:
                return
            if current_class is not None:
                raise UpstreamError(
                    f"repository Jenkins path changed to an unexpected item type: {repository_path}"
                )
            try:
                await self._create_item("repositories", repository_name, folder_xml)
            except httpx.HTTPError as exc:
                last_error = exc
            await self._reconcile_pause(attempt)

        if await self._item_class(repository_path) == folder_class:
            return
        raise UpstreamError(
            f"failed to reconcile Jenkins repository folder for {owner}/{name}"
        ) from last_error

    async def ensure_job(
        self,
        owner: str,
        name: str,
        default_branch: str,
        branch: str | None = None,
        pull_request: int | None = None,
    ) -> None:
        branch = branch or default_branch
        repository_key = f"{owner.lower()}/{name.lower()}"
        lock = self._repository_locks.setdefault(repository_key, asyncio.Lock())
        async with lock:
            await self._ensure_repository_folder(owner, name)
        repository_name = self.job_name(owner, name)
        child_name = self.branch_job_name(branch, pull_request)
        if pull_request is not None:
            display_name = f"PR-{pull_request}"
        else:
            display_name = branch
        repository_url = f"{self.github_web_url}/{owner}/{name}.git"
        escaped_repository_url = self._groovy_single_quoted(repository_url)
        escaped_default_branch = self._groovy_single_quoted(default_branch)
        escaped_repository = self._groovy_single_quoted(f"{owner}/{name}")
        script = (
            "@Library('jenkins-service-contract') _\n"
            "jenkinsServicePipeline("
            f"repositoryUrl: '{escaped_repository_url}', "
            f"trustedBranch: '{escaped_default_branch}', "
            f"repository: '{escaped_repository}'"
            ")\n"
        )
        xml = (
            "<?xml version='1.1' encoding='UTF-8'?>"
            "<flow-definition plugin='workflow-job'>"
            f"<actions/><description>Managed {escape(display_name)} pipeline. Do not edit."
            "</description>"
            f"<displayName>{escape(display_name)}</displayName>"
            "<keepDependencies>false</keepDependencies>"
            "<properties>"
            "<hudson.model.ParametersDefinitionProperty><parameterDefinitions>"
            "<hudson.model.StringParameterDefinition>"
            "<name>COMMIT_SHA</name><defaultValue></defaultValue><trim>true</trim>"
            "</hudson.model.StringParameterDefinition>"
            "<hudson.model.StringParameterDefinition>"
            "<name>PULL_REQUEST</name><defaultValue></defaultValue><trim>true</trim>"
            "</hudson.model.StringParameterDefinition>"
            "<hudson.model.StringParameterDefinition>"
            "<name>BASE_SHA</name><defaultValue></defaultValue><trim>true</trim>"
            "</hudson.model.StringParameterDefinition>"
            "<hudson.model.StringParameterDefinition>"
            "<name>BUILD_ID</name><defaultValue></defaultValue><trim>true</trim>"
            "</hudson.model.StringParameterDefinition>"
            "</parameterDefinitions></hudson.model.ParametersDefinitionProperty>"
            "</properties>"
            "<definition class='org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition' "
            "plugin='workflow-cps'>"
            f"<script>{escape(script)}</script><sandbox>true</sandbox>"
            "</definition><triggers/><disabled>false</disabled></flow-definition>"
        )
        job_path = f"repositories/{repository_name}/{child_name}"
        existing = await self._item(job_path)
        headers = {**await self._crumb(), "Content-Type": "application/xml"}
        if existing.status_code == 404:
            path = f"/job/{self._job_path(f'repositories/{repository_name}')}/createItem"
            params = {"name": child_name}
        else:
            existing.raise_for_status()
            if existing.json().get("_class") != "org.jenkinsci.plugins.workflow.job.WorkflowJob":
                raise UpstreamError(
                    f"managed Jenkins branch path has an unexpected item type: {job_path}"
                )
            path = f"/job/{self._job_path(job_path)}/config.xml"
            params = None
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                auth=self.auth,
                headers=headers,
                params=params,
                content=xml,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"failed to reconcile Jenkins job for {owner}/{name} {display_name}"
            ) from exc

    async def delete_job(self, owner: str, name: str) -> None:
        for path in (
            f"repositories/{self.job_name(owner, name)}",
            self.legacy_job_path(owner, name),
        ):
            existing = await self._item(path)
            if existing.status_code == 404:
                continue
            existing.raise_for_status()
            await self._post(f"/job/{self._job_path(path)}/doDelete")

    async def scan(self, owner: str, name: str, default_branch: str) -> None:
        await self.ensure_job(owner, name, default_branch)

    async def trigger(
        self,
        owner: str,
        name: str,
        commit_sha: str,
        pull_request: int | None,
        base_sha: str | None = None,
        build_id: str | None = None,
        branch: str | None = None,
    ) -> int:
        if branch is None:
            raise ValueError("branch is required when triggering a managed Jenkins job")
        job = self.managed_job_path(owner, name, branch, pull_request)
        response = await self._post(
            f"/job/{self._job_path(job)}/buildWithParameters",
            params={
                "COMMIT_SHA": commit_sha,
                "PULL_REQUEST": str(pull_request or ""),
                "BASE_SHA": base_sha or "",
                "BUILD_ID": build_id or "",
            },
        )
        location = response.headers.get("location", "").rstrip("/")
        try:
            return int(location.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            raise UpstreamError("Jenkins did not return a valid queue item location") from None

    async def cancel(self, job: str, build_number: int) -> None:
        encoded = self._job_path(job)
        await self._post(f"/job/{encoded}/{build_number}/stop")

    async def cancel_queue(self, queue_id: int) -> None:
        await self._post(
            "/queue/cancelItem",
            params={"id": str(queue_id)},
        )

    async def queue_status(self, queue_id: int) -> dict[str, Any] | None:
        try:
            response = await self.client.get(
                f"{self.base_url}/queue/item/{queue_id}/api/json",
                params={"tree": "cancelled,executable[number]"},
                auth=self.auth,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
        except httpx.HTTPError as exc:
            raise UpstreamError(f"failed to read Jenkins queue item {queue_id}") from exc

    async def build_status(
        self,
        job: str,
        build_number: int,
    ) -> dict[str, Any] | None:
        encoded = self._job_path(job)
        try:
            response = await self.client.get(
                f"{self.base_url}/job/{encoded}/{build_number}/api/json",
                params={"tree": "building,result,timestamp,duration"},
                auth=self.auth,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
        except httpx.HTTPError as exc:
            raise UpstreamError(f"failed to read Jenkins build {job} #{build_number}") from exc

    async def pipeline_result(
        self,
        job: str,
        build_number: int,
        max_bytes: int,
    ) -> bytes | None:
        encoded = self._job_path(job)
        path = (
            f"{self.base_url}/job/{encoded}/{build_number}/artifact/artifacts/pipeline-result.json"
        )
        try:
            async with self.client.stream("GET", path, auth=self.auth) as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > max_bytes:
                    raise UpstreamError("Jenkins pipeline result exceeds configured limit")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise UpstreamError("Jenkins pipeline result exceeds configured limit")
                return bytes(content)
        except UpstreamError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(
                f"failed to read Jenkins pipeline result for {job} #{build_number}"
            ) from exc

    async def progressive_log(
        self, job: str, build_number: int, start: int = 0
    ) -> tuple[str, int, bool]:
        encoded = self._job_path(job)
        try:
            response = await self.client.get(
                f"{self.base_url}/job/{encoded}/{build_number}/logText/progressiveText",
                params={"start": start},
                auth=self.auth,
            )
            response.raise_for_status()
            return (
                response.text,
                int(response.headers.get("x-text-size", start)),
                response.headers.get("x-more-data", "false").lower() == "true",
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"failed to read Jenkins log for {job} #{build_number}") from exc


@dataclass(frozen=True)
class BrokerReview:
    model: str
    prompt_version: str
    blocking: bool
    review: ReviewResult


class ReviewBrokerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = client or httpx.AsyncClient(timeout=90)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def review(
        self,
        *,
        repository: str,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        diff: str,
        relevant_context: str = "",
    ) -> BrokerReview:
        try:
            response = await self.client.post(
                f"{self.base_url}/internal/v1/review",
                headers=self.headers,
                json={
                    "repository": repository,
                    "pull_request": pull_request,
                    "base_sha": base_sha.lower(),
                    "head_sha": head_sha.lower(),
                    "diff": diff,
                    "relevant_context": relevant_context,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return BrokerReview(
                model=str(payload["model"]),
                prompt_version=str(payload["prompt_version"]),
                blocking=bool(payload["blocking"]),
                review=ReviewResult.model_validate(payload["review"]),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise UpstreamError("review broker returned no valid review") from exc


class GitHubClient:
    ALLOWED_ACTIONS = {"review_comment", "issue_comment", "check_run", "status"}

    def __init__(
        self,
        api_url: str,
        write_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        encoded = b64encode(f"x-access-token:{write_token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def set_status(
        self,
        repository: str,
        sha: str,
        *,
        context: str,
        state: str,
        description: str,
        target_url: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"pending", "success", "failure", "error"}:
            raise ValueError(f"unsupported GitHub status state: {state}")
        body: dict[str, Any] = {
            "state": state,
            "context": context,
            "description": description[:140],
        }
        if target_url:
            body["target_url"] = target_url
        response = await self.client.post(
            f"{self.api_url}/repos/{repository}/statuses/{sha}",
            headers=self.headers,
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"GitHub status publication failed: {context}") from exc
        return cast(dict[str, Any], response.json())

    async def create_review(
        self,
        repository: str,
        pull_number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if pull_number < 1:
            raise ValueError("pull_number must be positive")
        response = await self.client.post(
            f"{self.api_url}/repos/{repository}/pulls/{pull_number}/reviews",
            headers=self.headers,
            json={
                "commit_id": commit_sha,
                "event": "COMMENT",
                "body": body,
                "comments": comments,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError("GitHub batched review publication failed") from exc
        return cast(dict[str, Any], response.json())

    async def pull_request_diff(
        self,
        repository: str,
        pull_number: int,
        *,
        base_sha: str,
        head_sha: str,
        max_bytes: int,
    ) -> str:
        if pull_number < 1:
            raise ValueError("pull_number must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        path = f"{self.api_url}/repos/{repository}/pulls/{pull_number}"

        async def verify_identity() -> None:
            response = await self.client.get(path, headers=self.headers)
            try:
                response.raise_for_status()
                payload = response.json()
                actual_base = payload["base"]["sha"]
                actual_head = payload["head"]["sha"]
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise UpstreamError("GitHub pull request identity is unavailable") from exc
            if (
                not isinstance(actual_base, str)
                or not isinstance(actual_head, str)
                or actual_base.lower() != base_sha.lower()
                or actual_head.lower() != head_sha.lower()
            ):
                raise UpstreamError("GitHub pull request changed while preparing its review")

        await verify_identity()
        diff_headers = {
            **self.headers,
            "Accept": "application/vnd.github.diff",
        }
        content = bytearray()
        try:
            async with self.client.stream("GET", path, headers=diff_headers) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > max_bytes:
                    raise UpstreamError("GitHub pull request diff exceeds configured byte limit")
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise UpstreamError(
                            "GitHub pull request diff exceeds configured byte limit"
                        )
        except UpstreamError:
            raise
        except (httpx.HTTPError, UnicodeDecodeError, ValueError) as exc:
            raise UpstreamError("GitHub pull request diff is unavailable") from exc
        await verify_identity()
        try:
            return bytes(content).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpstreamError("GitHub pull request diff is not UTF-8") from exc

    async def execute_action(
        self,
        repository: str,
        action: str,
        payload: dict[str, Any],
        allowed: set[str],
    ) -> dict[str, Any]:
        if action not in self.ALLOWED_ACTIONS or action not in allowed:
            raise ValueError(f"GitHub action is not allowed: {action}")
        if action == "review_comment":
            target = payload.get("pull_number")
            if not isinstance(target, int) or target < 1:
                raise ValueError("review_comment requires a positive pull_number")
            path = f"/repos/{repository}/pulls/{target}/comments"
        elif action == "issue_comment":
            target = payload.get("issue_number")
            if not isinstance(target, int) or target < 1:
                raise ValueError("issue_comment requires a positive issue_number")
            path = f"/repos/{repository}/issues/{target}/comments"
        elif action == "check_run":
            path = f"/repos/{repository}/check-runs"
        else:
            target = payload.get("sha")
            if not isinstance(target, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", target):
                raise ValueError("status requires a 40-character commit SHA")
            path = f"/repos/{repository}/statuses/{target}"
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ValueError(f"{action} requires an object body")
        response = await self.client.post(
            f"{self.api_url}{path}",
            headers=self.headers,
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"GitHub action failed: {action}") from exc
        return cast(dict[str, Any], response.json())

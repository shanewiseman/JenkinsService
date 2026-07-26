from __future__ import annotations

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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, token)
        self.client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self.github_web_url = github_web_url.rstrip("/")

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
    def _job_path(job: str) -> str:
        return "/job/".join(quote(part, safe="") for part in job.split("/"))

    @staticmethod
    def _groovy_single_quoted(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    async def ensure_job(self, owner: str, name: str, default_branch: str) -> None:
        job_name = self.job_name(owner, name)
        repository_url = f"{self.github_web_url}/{owner}/{name}.git"
        escaped_repository_url = self._groovy_single_quoted(repository_url)
        escaped_default_branch = self._groovy_single_quoted(default_branch)
        escaped_repository = self._groovy_single_quoted(f"{owner}/{name}")
        script = (
            "@Library('jenkins-service-contract@v1') _\n"
            "jenkinsServicePipeline("
            f"repositoryUrl: '{escaped_repository_url}', "
            f"trustedBranch: '{escaped_default_branch}', "
            f"repository: '{escaped_repository}'"
            ")\n"
        )
        xml = (
            "<?xml version='1.1' encoding='UTF-8'?>"
            "<flow-definition plugin='workflow-job'>"
            "<actions/><description>Managed by JenkinsService. Do not edit.</description>"
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
        encoded = quote(job_name, safe="")
        existing = await self.client.get(
            f"{self.base_url}/job/repositories/job/{encoded}/api/json",
            auth=self.auth,
        )
        headers = {**await self._crumb(), "Content-Type": "application/xml"}
        if existing.status_code == 404:
            path = "/job/repositories/createItem"
            params = {"name": job_name}
        else:
            existing.raise_for_status()
            path = f"/job/repositories/job/{encoded}/config.xml"
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
            raise UpstreamError(f"failed to reconcile Jenkins job for {owner}/{name}") from exc

    async def delete_job(self, owner: str, name: str) -> None:
        encoded = quote(self.job_name(owner, name), safe="")
        await self._post(f"/job/repositories/job/{encoded}/doDelete")

    async def scan(self, owner: str, name: str) -> None:
        encoded = quote(self.job_name(owner, name), safe="")
        await self._post(f"/job/repositories/job/{encoded}/build")

    async def trigger(
        self,
        owner: str,
        name: str,
        commit_sha: str,
        pull_request: int | None,
        base_sha: str | None = None,
        build_id: str | None = None,
    ) -> int:
        encoded = quote(self.job_name(owner, name), safe="")
        response = await self._post(
            f"/job/repositories/job/{encoded}/buildWithParameters",
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

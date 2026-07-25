from __future__ import annotations

import httpx
import pytest

from jenkins_service.clients import GitHubClient, JenkinsClient, UpstreamError


async def test_jenkins_trigger_uses_crumb_and_returns_queue_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(
                200,
                json={"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb"},
            )
        return httpx.Response(201, headers={"location": "http://jenkins/queue/item/123/"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = JenkinsClient("http://jenkins", "user", "token", http)
        queue_id = await client.trigger("owner", "repo", "a" * 40, 7)
    assert queue_id == 123
    assert requests[-1].headers["jenkins-crumb"] == "crumb"
    assert requests[-1].url.path.endswith("/buildWithParameters")


async def test_jenkins_trigger_rejects_missing_queue_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(404)
        return httpx.Response(201)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = JenkinsClient("http://jenkins", "user", "token", http)
        with pytest.raises(UpstreamError, match="queue item location"):
            await client.trigger("owner", "repo", "a" * 40, None)


async def test_jenkins_job_uses_configured_enterprise_checkout_url() -> None:
    posted = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.url.path == "/job/repositories/job/owner--repo/api/json":
            return httpx.Response(404)
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(404)
        posted = request.content.decode()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JenkinsClient(
            "http://jenkins",
            "user",
            "token",
            http,
            github_web_url="https://github.enterprise.test",
        )
        await client.ensure_job("owner", "repo", "main")
    assert "https://github.enterprise.test/owner/repo.git" in posted


async def test_jenkins_queue_build_and_result_reads_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/queue/item/123/api/json":
            return httpx.Response(200, json={"executable": {"number": 7}})
        if request.url.path.endswith("/7/api/json"):
            return httpx.Response(
                200,
                json={"building": False, "result": "SUCCESS", "timestamp": 1},
            )
        if request.url.path.endswith("/artifact/artifacts/pipeline-result.json"):
            return httpx.Response(200, content=b"{}")
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JenkinsClient("http://jenkins", "user", "token", http)
        assert await client.queue_status(123) == {"executable": {"number": 7}}
        state = await client.build_status("repositories/owner--repo", 7)
        assert state is not None and state["result"] == "SUCCESS"
        assert await client.pipeline_result("repositories/owner--repo", 7, 10) == b"{}"


async def test_github_client_enforces_declared_actions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 7})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GitHubClient("https://api.github.test", "token", http)
        response = await client.execute_action(
            "allowed/repo",
            "issue_comment",
            {"issue_number": 1, "body": {"body": "review"}},
            {"issue_comment"},
        )
        assert response["id"] == 7

        with pytest.raises(ValueError, match="not allowed"):
            await client.execute_action(
                "allowed/repo",
                "status",
                {"sha": "a" * 40, "body": {}},
                {"issue_comment"},
            )

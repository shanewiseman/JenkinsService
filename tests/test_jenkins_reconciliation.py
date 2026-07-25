from __future__ import annotations

from html import unescape

import httpx

from jenkins_service.clients import JenkinsClient


async def test_ensure_job_creates_only_curated_pipeline_xml() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(
                200,
                json={
                    "crumbRequestField": "Jenkins-Crumb",
                    "crumb": "crumb",
                },
            )
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JenkinsClient(
            "http://jenkins",
            "user",
            "token",
            http,
        )
        await client.ensure_job(
            "allowed",
            "project",
            "main",
        )

    create = requests[-1]
    assert create.url.path == "/job/repositories/createItem"
    assert create.url.params["name"] == "allowed--project"
    assert create.headers["jenkins-crumb"] == "crumb"
    xml = unescape(create.content.decode())
    assert "jenkinsServicePipeline" in xml
    assert "trustedBranch: 'main'" in xml
    assert "script console" not in xml.lower()

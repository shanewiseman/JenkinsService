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
        if request.method == "GET" and request.url.path == (
            "/job/repositories/job/allowed--project/api/json"
        ):
            return httpx.Response(
                200,
                json={"_class": "com.cloudbees.hudson.plugins.folder.Folder"},
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
    assert create.url.path == "/job/repositories/job/allowed--project/createItem"
    assert create.url.params["name"] == JenkinsClient.branch_job_name("main")
    assert create.headers["jenkins-crumb"] == "crumb"
    xml = unescape(create.content.decode())
    assert "jenkinsServicePipeline" in xml
    assert "trustedBranch: 'main'" in xml
    assert "<displayName>main</displayName>" in xml
    assert "script console" not in xml.lower()


async def test_ensure_job_preserves_legacy_history_before_creating_hierarchy() -> None:
    requests: list[httpx.Request] = []
    legacy_renamed = False
    rename_visibility_checks = 0
    folder_created = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal folder_created, legacy_renamed, rename_visibility_checks
        requests.append(request)
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(404)
        if request.method == "GET":
            if request.url.path.endswith("/allowed--project--legacy/api/json"):
                if legacy_renamed and rename_visibility_checks >= 3:
                    return httpx.Response(
                        200,
                        json={"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob"},
                    )
                return httpx.Response(404)
            if request.url.path.endswith("/allowed--project/api/json") and folder_created:
                return httpx.Response(
                    200,
                    json={"_class": "com.cloudbees.hudson.plugins.folder.Folder"},
                )
            if request.url.path.endswith("/allowed--project/api/json") and legacy_renamed:
                rename_visibility_checks += 1
                if rename_visibility_checks >= 3:
                    return httpx.Response(404)
            if request.url.path.endswith("/allowed--project/api/json"):
                return httpx.Response(
                    200,
                    json={"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob"},
                )
            return httpx.Response(404)
        if request.url.path.endswith("/allowed--project/doRename"):
            legacy_renamed = True
        if request.url.path == "/job/repositories/createItem":
            folder_created = True
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JenkinsClient(
            "http://jenkins",
            "user",
            "token",
            http,
            reconcile_delay_seconds=0,
        )
        await client.ensure_job("allowed", "project", "master")

    assert legacy_renamed
    assert folder_created
    rename = next(request for request in requests if request.url.path.endswith("/doRename"))
    assert rename.url.params["newName"] == "allowed--project--legacy"
    creates = [request for request in requests if request.url.path.endswith("/createItem")]
    assert creates[0].url.params["name"] == "allowed--project"
    assert creates[1].url.params["name"] == JenkinsClient.branch_job_name("master")


async def test_ensure_job_recovers_when_concurrent_folder_creation_wins() -> None:
    folder_created = False
    folder_create_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal folder_create_attempts, folder_created
        if request.url.path == "/crumbIssuer/api/json":
            return httpx.Response(404)
        if request.method == "GET":
            if request.url.path.endswith("/allowed--project/api/json"):
                if folder_created:
                    return httpx.Response(
                        200,
                        json={"_class": "com.cloudbees.hudson.plugins.folder.Folder"},
                    )
                return httpx.Response(404)
            return httpx.Response(404)
        if request.url.path == "/job/repositories/createItem":
            folder_create_attempts += 1
            folder_created = True
            return httpx.Response(400)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JenkinsClient(
            "http://jenkins",
            "user",
            "token",
            http,
            reconcile_delay_seconds=0,
        )
        await client.ensure_job("allowed", "project", "master")

    assert folder_create_attempts == 1

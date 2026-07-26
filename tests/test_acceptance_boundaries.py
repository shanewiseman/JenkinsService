from __future__ import annotations

from pathlib import Path

import yaml

from jenkins_service.app import create_app
from jenkins_service.config import Settings
from jenkins_service.models import (
    Build,
    PipelineResult,
    Principal,
    RepositoryCreate,
    Scope,
)
from jenkins_service.security import Redactor

ROOT = Path(__file__).parents[1]


def test_openapi_contains_operation_input_schemas(
    settings,
    store,
    service,
    authenticator,
) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    operation = app.openapi()["paths"]["/api/v1/pipelines/trigger"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert "repository_id" in request_schema["properties"]
    assert request_schema["properties"]["commit_sha"]["pattern"]


def test_database_password_is_url_encoded(tmp_path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text(
        "a+b&c?d",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=("postgresql://service@postgres:5432/service"),
        database_password_file=password_file,
    )
    assert settings.postgres_dsn().endswith("password=a%2Bb%26c%3Fd")


def test_compose_mounts_credentials_only_into_their_trusted_consumers() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    secret_sets = {name: set(service.get("secrets", [])) for name, service in services.items()}

    assert "github_read_pat" in secret_sets["jenkins"]
    assert "github_webhook_secret" not in secret_sets["jenkins"]
    assert "github_read_pat" not in secret_sets["gateway"]
    assert {"github_write_pat", "github_webhook_secret"} <= secret_sets["gateway"]
    assert secret_sets["review-broker"] == {
        "openai_api_key",
        "review_broker_token",
    }
    assert "openai_api_key" not in secret_sets["gateway"]
    assert secret_sets["extension-runner"] == set()
    assert secret_sets["orchestrator"] == {
        "jenkins_api_token",
        "build_callback_secret",
    }

    pipeline = (ROOT / "shared-library/vars/jenkinsServicePipeline.groovy").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "openai_api_key",
        "github_read_pat",
        "github_write_pat",
        "jenkins_api_token",
        "github_webhook_secret",
        "build_callback_secret:/workspace",
        "/certs/client:/workspace",
    ):
        assert forbidden not in pipeline
    assert '"git", "-C", "source", "diff", "--no-ext-diff", "--unified=3"' in pipeline
    assert "resource.RLIMIT_FSIZE" in pipeline
    assert "REVIEW_BASE_SHA" in pipeline
    assert "diff: reviewDiff" in pipeline


def test_compose_routes_only_public_services_through_traefik() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    def labels(service_name: str) -> dict[str, str]:
        return dict(label.split("=", 1) for label in services[service_name].get("labels", []))

    routed_services = {
        name for name, service in services.items() if labels(name).get("traefik.enable") == "true"
    }
    assert routed_services == {"jenkins", "gateway"}
    assert {name for name in services if labels(name).get("traefik.enable") == "false"} == set(
        services
    ) - routed_services
    assert {
        name for name, service in services.items() if "dmz" in service.get("networks", {})
    } == routed_services
    assert compose["networks"]["dmz"] == {
        "external": True,
        "name": "${DMZ_NETWORK:-dmz_internal}",
    }

    jenkins_labels = labels("jenkins")
    gateway_labels = labels("gateway")
    assert jenkins_labels["traefik.docker.network"] == ("${DMZ_NETWORK:-dmz_internal}")
    assert gateway_labels["traefik.docker.network"] == ("${DMZ_NETWORK:-dmz_internal}")
    assert jenkins_labels["traefik.http.routers.jenkinsservice-jenkins.tls"] == "true"
    assert gateway_labels["traefik.http.routers.jenkinsservice-gateway.tls"] == "true"
    assert jenkins_labels["traefik.http.routers.jenkinsservice-jenkins.rule"] == (
        "Host(`${JENKINS_HOST:?Set JENKINS_HOST to the public hostname}`) "
        "&& (Path(`/jenkins`) || PathPrefix(`/jenkins/`))"
    )
    assert (
        gateway_labels["traefik.http.routers.jenkinsservice-gateway.rule"]
        == "Host(`${JENKINS_HOST:?Set JENKINS_HOST to the public hostname}`)"
    )
    assert jenkins_labels["traefik.http.routers.jenkinsservice-jenkins.priority"] == "200"
    assert gateway_labels["traefik.http.routers.jenkinsservice-gateway.priority"] == "1"
    assert (
        jenkins_labels["traefik.http.services.jenkinsservice-jenkins.loadbalancer.server.port"]
        == "8080"
    )
    assert (
        gateway_labels["traefik.http.services.jenkinsservice-gateway.loadbalancer.server.port"]
        == "8000"
    )
    assert (
        jenkins_labels[
            "traefik.http.middlewares.jenkinsservice-jenkins-slash.redirectregex.replacement"
        ]
        == "https://$${1}/jenkins/"
    )
    assert services["jenkins"]["ports"] == ["127.0.0.1:${JENKINS_PORT:-18080}:8080"]
    assert "expose" not in services["jenkins"]
    assert services["gateway"]["ports"] == ["127.0.0.1:${API_PORT:-18000}:8000"]

    casc = (ROOT / "docker/jenkins/casc.yaml").read_text(encoding="utf-8")
    java_opts = services["jenkins"]["environment"]["JAVA_OPTS"]
    agent_start = (ROOT / "docker/agent/start-agent.sh").read_text(encoding="utf-8")
    assert "-Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true" in java_opts
    assert 'remote: "file:///usr/share/jenkins/ref/shared-library"' in casc
    assert "allowVersionOverride: false" in casc
    assert "slaveAgentPort: -1" in casc
    assert services["orchestrator"]["environment"]["JENKINS_WEB_SOCKET"] == "true"
    assert "-webSocket" not in agent_start
    assert '-url "$JENKINS_URL"' not in agent_start
    assert '-name "$JENKINS_AGENT_NAME"' not in agent_start


def test_pipeline_cleanup_quotes_the_complete_label_filter() -> None:
    pipeline = (ROOT / "shared-library/vars/jenkinsServicePipeline.groovy").read_text(
        encoding="utf-8",
    )

    assert 'String cleanupFilter = shellQuote("label=${buildLabel}")' in pipeline
    assert "docker ps -aq --filter ${cleanupFilter}" in pipeline
    assert "--filter 'label=${shellQuote(buildLabel)}'" not in pipeline


def test_pipeline_json_parsing_does_not_retain_a_parser_across_cps_steps() -> None:
    pipeline = (ROOT / "shared-library/vars/jenkinsServicePipeline.groovy").read_text(
        encoding="utf-8",
    )

    assert "@NonCPS\nprivate Object parseJson(String text)" in pipeline
    assert "new JsonSlurperClassic().parseText(readFile" not in pipeline
    assert "contract = parseJson(contractJson) as Map" in pipeline
    assert "artifacts: parseJson(artifactsJson)" in pipeline
    assert "result = parseJson(resultJson) as Map" in pipeline


def test_backup_and_restore_helper_images_are_digest_pinned() -> None:
    alpine_image = (
        "alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1"
    )

    for script_name in ("backup.sh", "restore.sh"):
        script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert alpine_image in script


def test_contract_validator_invokes_python3_explicitly() -> None:
    script = (ROOT / "scripts/validate-contract.sh").read_text(encoding="utf-8")
    assert 'exec python3 -m jenkins_service.contract "${1:-.jenkins/pipeline.yaml}"' in script


def test_extension_runner_has_bounded_hardened_tmpfs() -> None:
    runner = (ROOT / "src/jenkins_service/extension_runner.py").read_text(
        encoding="utf-8",
    )
    assert '"--read-only"' in runner
    assert '"--tmpfs"' in runner
    assert '"/tmp:rw,noexec,nosuid,size=64m"' in runner


async def test_progressive_logs_are_redacted(
    service,
    store,
) -> None:
    service.redactor = Redactor([r"(?i)(token=)\S+"])
    repository = await store.create_repository(RepositoryCreate(owner="allowed", name="project"))
    build = Build(
        repository_id=repository.id,
        jenkins_job="repositories/allowed--project",
        jenkins_build_number=7,
        result=PipelineResult(
            repository=repository.full_name,
            commit_sha="a" * 40,
            status="running",
        ),
    )
    await store.save_build(build)
    service.jenkins.progressive_log = _secret_log

    result = await service.build_log(
        Principal(
            token_id="reader",
            scopes={Scope.READ},
        ),
        str(build.id),
    )
    assert result["text"] == "token=[REDACTED]"


async def test_terminal_queued_build_has_no_more_logs(
    service,
    store,
) -> None:
    repository = await store.create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    build = Build(
        repository_id=repository.id,
        jenkins_job="repositories/allowed--project",
        jenkins_queue_id=42,
        result=PipelineResult(
            repository=repository.full_name,
            commit_sha="a" * 40,
            status="queued",
        ),
    )
    await store.save_build(build)
    service.jenkins.queue_state = {"cancelled": True}

    result = await service.build_log(
        Principal(
            token_id="reader",
            scopes={Scope.READ},
        ),
        str(build.id),
    )

    assert result == {"text": "", "next": 0, "more": False}


async def _secret_log(
    job: str,
    build_number: int,
    start: int,
) -> tuple[str, int, bool]:
    return ("token=secret-value", 18, False)

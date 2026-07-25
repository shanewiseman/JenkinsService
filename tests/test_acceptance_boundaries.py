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


def test_compose_mounts_github_secrets_only_where_used() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    jenkins_secrets = set(compose["services"]["jenkins"]["secrets"])
    gateway_secrets = set(compose["services"]["gateway"]["secrets"])

    assert "github_read_pat" in jenkins_secrets
    assert "github_webhook_secret" not in jenkins_secrets
    assert "github_read_pat" not in gateway_secrets
    assert {"github_write_pat", "github_webhook_secret"} <= gateway_secrets


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


async def _secret_log(
    job: str,
    build_number: int,
    start: int,
) -> tuple[str, int, bool]:
    return ("token=secret-value", 18, False)

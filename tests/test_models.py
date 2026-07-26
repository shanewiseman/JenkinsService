from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from jenkins_service.models import (
    Artifact,
    CheckResult,
    ExtensionManifest,
    PipelineResult,
    RepositoryCreate,
    TriggerRequest,
)


def test_pipeline_result_requires_exact_commit() -> None:
    result = PipelineResult(
        repository="allowed/project",
        commit_sha="a" * 40,
        trusted_sha="b" * 40,
        status="passed",
    )
    assert result.schema_version == "ci.jenkinsservice.dev/result/v1"
    with pytest.raises(ValidationError):
        PipelineResult(repository="allowed/project", commit_sha="main", status="passed")
    with pytest.raises(ValidationError):
        PipelineResult(
            repository="allowed/project",
            commit_sha="a" * 40,
            trusted_sha="main",
            status="passed",
        )


def test_extension_manifest_requires_digest() -> None:
    values = {
        "schema_version": "ci.jenkinsservice.dev/extension/v1",
        "id": "reviewer",
        "image": "example/reviewer:latest",
        "compatible_contracts": ["ci.jenkinsservice.dev/v1"],
        "actions": ["review"],
        "input_schema": "input.schema.json",
        "output_schema": "output.schema.json",
    }
    with pytest.raises(ValidationError, match="sha256"):
        ExtensionManifest.model_validate(values)
    values["image"] = "example/reviewer@sha256:" + "a" * 64
    assert ExtensionManifest.model_validate(values).id == "reviewer"


@pytest.mark.parametrize(
    "branch",
    ["feature/../../main", "main'); error('injected", ".hidden", "HEAD"],
)
def test_repository_rejects_unsafe_branch_names(branch: str) -> None:
    with pytest.raises(ValidationError, match="default_branch"):
        RepositoryCreate(owner="allowed", name="repo", default_branch=branch)
    with pytest.raises(ValidationError, match="branch"):
        TriggerRequest(
            repository_id=uuid4(),
            commit_sha="a" * 40,
            branch=branch,
        )


def test_pipeline_result_rejects_unknown_fields_and_unsafe_artifact_paths() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PipelineResult.model_validate(
            {
                "repository": "allowed/project",
                "commit_sha": "a" * 40,
                "status": "passed",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError, match="traverse"):
        Artifact(
            name="bad",
            path="../secret",
            sha256="a" * 64,
            size=1,
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        Artifact(
            name="bad",
            path="",
            sha256="a" * 64,
            size=1,
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        CheckResult(
            id="test",
            name="test",
            status="passed",
            required=True,
            duration_seconds=0,
            reports=[""],
        )

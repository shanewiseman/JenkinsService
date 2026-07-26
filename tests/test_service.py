from __future__ import annotations

import pytest

from jenkins_service.models import (
    Build,
    CancelRequest,
    ExtensionActionRequest,
    ExtensionOutput,
    PipelineResult,
    Principal,
    RepositoryCreate,
    ScanRequest,
    Scope,
    TriggerRequest,
)


async def test_registration_reconciles_job_and_trigger_uses_managed_path(
    service,
    store,
) -> None:
    principal = Principal(
        token_id="operator",
        scopes={Scope.OPERATE},
    )
    repository = await service.register_repository(
        principal,
        RepositoryCreate(
            owner="allowed",
            name="project",
            default_branch="stable",
        ),
    )
    assert service.jenkins.jobs == [
        ("allowed", "project", "stable", "stable", None),
    ]

    queue = await service.trigger_pipeline(
        principal,
        TriggerRequest(
            repository_id=repository.id,
            commit_sha="a" * 40,
            pull_request=3,
        ),
    )
    assert queue.jenkins_queue_id == 42
    build = (await store.list_builds())[0]
    assert service.jenkins.jobs[-1] == (
        "allowed",
        "project",
        "stable",
        "stable",
        3,
    )
    assert build.jenkins_job == "repositories/allowed--project/PR-3"
    assert build.branch == "stable"
    assert build.jenkins_queue_id == 42
    assert build.queue_item_id == queue.id


async def test_non_pr_branches_receive_separate_managed_jobs(
    service,
    store,
) -> None:
    principal = Principal(token_id="operator", scopes={Scope.OPERATE})
    repository = await service.register_repository(
        principal,
        RepositoryCreate(owner="allowed", name="project", default_branch="master"),
    )

    queue = await service.trigger_pipeline(
        principal,
        TriggerRequest(
            repository_id=repository.id,
            commit_sha="c" * 40,
            branch="feature/display-refresh",
        ),
    )

    build = (await store.list_builds())[0]
    assert queue.branch == "feature/display-refresh"
    assert build.branch == "feature/display-refresh"
    assert build.jenkins_job == (
        "repositories/allowed--project/"
        + service.jenkins.branch_job_name("feature/display-refresh")
    )


async def test_hierarchy_reconciliation_preserves_legacy_build_lookup(
    service,
    store,
) -> None:
    principal = Principal(token_id="operator", scopes={Scope.OPERATE})
    repository = await service.register_repository(
        principal,
        RepositoryCreate(owner="allowed", name="project"),
    )
    legacy_build = Build(
        repository_id=repository.id,
        jenkins_job="repositories/allowed--project",
        result=PipelineResult(
            repository=repository.full_name,
            commit_sha="d" * 40,
            status="passed",
        ),
    )
    await store.save_build(legacy_build)

    result = await service.scan_repository(
        principal,
        ScanRequest(repository_id=repository.id),
    )

    assert result.detail == "repository hierarchy reconciled"
    assert (await store.get_build(legacy_build.id)).jenkins_job == (
        "repositories/allowed--project--legacy"
    )


async def test_build_read_reconciles_queue_and_canonical_result(
    service,
    store,
) -> None:
    principal = Principal(token_id="operator", scopes={Scope.OPERATE})
    repository = await service.register_repository(
        principal,
        RepositoryCreate(owner="allowed", name="project"),
    )
    queue = await service.trigger_pipeline(
        principal,
        TriggerRequest(repository_id=repository.id, commit_sha="a" * 40),
    )
    build = (await store.list_builds())[0]
    service.jenkins.queue_state = {"executable": {"number": 7}}
    service.jenkins.build_state = {
        "building": False,
        "result": "SUCCESS",
        "timestamp": 1_700_000_000_000,
        "duration": 1000,
    }
    service.jenkins.result_bytes = (
        b'{"schema_version":"ci.jenkinsservice.dev/result/v1",'
        b'"repository":"allowed/project","commit_sha":"'
        + b"a"
        * 40
        + b'","pull_request":null,"status":"passed","checks":[],'
        b'"artifacts":[],"extension_runs":[]}'
    )

    refreshed = await service.build(principal, str(build.id))
    assert refreshed.jenkins_build_number == 7
    assert refreshed.result.status == "passed"
    assert (await store.get_queue_item(queue.id)).state == "started"


async def test_queued_build_can_be_cancelled(
    service,
    store,
) -> None:
    principal = Principal(token_id="operator", scopes={Scope.OPERATE})
    repository = await service.register_repository(
        principal,
        RepositoryCreate(owner="allowed", name="project"),
    )
    queue = await service.trigger_pipeline(
        principal,
        TriggerRequest(repository_id=repository.id, commit_sha="b" * 40),
    )
    build = (await store.list_builds())[0]

    await service.cancel_pipeline(principal, CancelRequest(build_id=build.id))

    assert service.jenkins.queue_cancellations == [42]
    assert (await store.get_queue_item(queue.id)).state == "cancelled"


async def test_extension_action_is_validated_audited_and_idempotent(
    service,
    store,
) -> None:
    principal = Principal(
        token_id="extension-operator",
        scopes={Scope.EXTEND},
    )
    repository = await store.create_repository(RepositoryCreate(owner="allowed", name="project"))
    service.extension_runner.output = ExtensionOutput(
        findings=[
            {
                "severity": "warning",
                "message": "example",
            }
        ],
        requested_github_actions=[
            {
                "type": "issue_comment",
                "issue_number": 12,
                "body": {"body": "review"},
            }
        ],
    )
    request = ExtensionActionRequest(
        extension_id="reference-review",
        action="review",
        repository_id=repository.id,
        idempotency_key="request-12345",
    )
    first = await service.run_extension_action(
        principal,
        request,
    )
    second = await service.run_extension_action(
        principal,
        request,
    )

    assert first.status == "succeeded"
    assert second.id == first.id
    assert service.extension_runner.calls == [("reference-review", "review")]
    assert service.github.actions[0][1] == "issue_comment"
    actions = [record.action for record in await store.list_audit()]
    assert "github.issue_comment" in actions
    assert "run_extension_action" in actions


async def test_extension_output_schema_is_enforced_before_github_actions(
    service,
    store,
    monkeypatch,
) -> None:
    principal = Principal(
        token_id="extension-operator",
        scopes={Scope.EXTEND},
    )
    repository = await store.create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    service.extension_runner.output = ExtensionOutput(
        requested_github_actions=[
            {
                "type": "issue_comment",
                "issue_number": 12,
                "body": {"body": "review"},
            }
        ],
    )
    validated_outputs = []

    def reject_output(extension_id, output) -> None:
        validated_outputs.append((extension_id, output))
        raise ValueError("extension output does not match schema")

    monkeypatch.setattr(service.extension_catalog, "validate_output", reject_output)
    request = ExtensionActionRequest(
        extension_id="reference-review",
        action="review",
        repository_id=repository.id,
        idempotency_key="schema-rejection",
    )

    with pytest.raises(ValueError, match="does not match schema"):
        await service.run_extension_action(principal, request)

    assert validated_outputs[0][0] == "reference-review"
    assert validated_outputs[0][1]["requested_github_actions"]
    assert service.github.actions == []
    stored = await store.get_extension_run_by_key("schema-rejection")
    assert stored is not None
    assert stored.status == "failed"

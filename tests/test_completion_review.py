from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jenkins_service.app import create_app
from jenkins_service.clients import BrokerReview
from jenkins_service.models import (
    BuildCompletion,
    PipelineResult,
    Principal,
    RepositoryCreate,
    ReviewPolicy,
    Scope,
    TriggerRequest,
)
from jenkins_service.review import ReviewFinding, ReviewResult, Severity


class FakeReviewBroker:
    def __init__(self, severity: Severity) -> None:
        self.severity = severity
        self.calls: list[dict[str, Any]] = []

    async def review(self, **payload: Any) -> BrokerReview:
        self.calls.append(payload)
        review = ReviewResult(
            summary="One bounded inline finding.",
            findings=[
                ReviewFinding(
                    severity=self.severity,
                    title="Unsafe change",
                    body="Handle this added line safely.",
                    path="src/example.py",
                    line=1,
                )
            ],
        )
        return BrokerReview(
            model="gpt-5.6-terra",
            prompt_version="v1",
            blocking=review.has_critical_findings,
            review=review,
        )


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_build_completion_authentication_replay_and_idempotency(
    settings,
    store,
    service,
    authenticator,
    tmp_path,
) -> None:
    secret = "completion-secret"
    secret_file = tmp_path / "build-callback"
    secret_file.write_text(secret, encoding="utf-8")
    settings.build_callback_secret_file = secret_file
    principal = Principal(token_id="operator", scopes={Scope.OPERATE})
    repository = asyncio.run(
        service.register_repository(
            principal,
            RepositoryCreate(owner="allowed", name="project"),
        )
    )
    queue = asyncio.run(
        service.trigger_pipeline(
            principal,
            TriggerRequest(repository_id=repository.id, commit_sha="a" * 40),
        )
    )
    assert queue.build_id is not None
    result = PipelineResult(
        repository="allowed/project",
        commit_sha="a" * 40,
        build_id=queue.build_id,
        status="passed",
    )
    completion = BuildCompletion(
        callback_id="callback-123",
        timestamp=int(time.time()),
        build_id=queue.build_id,
        result=result,
    )
    body = completion.model_dump_json().encode()
    headers = {"X-JenkinsService-Signature": _signature(secret, body)}
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )

    with TestClient(app) as client:
        bad_signature = client.post(
            "/internal/build-completions",
            content=body,
            headers={"X-JenkinsService-Signature": "sha256=bad"},
        )
        assert bad_signature.status_code == 401

        wrong = completion.model_copy(
            update={"result": result.model_copy(update={"repository": "allowed/other"})}
        )
        wrong_body = wrong.model_dump_json().encode()
        wrong_identity = client.post(
            "/internal/build-completions",
            content=wrong_body,
            headers={"X-JenkinsService-Signature": _signature(secret, wrong_body)},
        )
        assert wrong_identity.status_code == 400

        accepted = client.post(
            "/internal/build-completions",
            content=body,
            headers=headers,
        )
        replay = client.post(
            "/internal/build-completions",
            content=body,
            headers=headers,
        )
        assert accepted.status_code == 202
        assert accepted.json()["duplicate"] is False
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True

        expired = completion.model_copy(
            update={
                "callback_id": "callback-expired",
                "timestamp": int(time.time()) - settings.build_callback_max_age_seconds - 1,
            }
        )
        expired_body = expired.model_dump_json().encode()
        expired_response = client.post(
            "/internal/build-completions",
            content=expired_body,
            headers={"X-JenkinsService-Signature": _signature(secret, expired_body)},
        )
        assert expired_response.status_code == 401

    native = [
        item
        for item in service.github.statuses
        if item["context"] == "jenkinsservice/native-ci" and item["state"] == "success"
    ]
    ai = [
        item
        for item in service.github.statuses
        if item["context"] == "jenkinsservice/ai-review" and item["state"] == "success"
    ]
    assert len(native) == 1
    assert len(ai) == 1


@pytest.mark.parametrize(
    ("severity", "expected_state"),
    [(Severity.HIGH, "success"), (Severity.CRITICAL, "failure")],
)
async def test_ai_review_publishes_one_validated_batch_and_deduplicates(
    service,
    store,
    severity: Severity,
    expected_state: str,
) -> None:
    principal = Principal(token_id="jenkins-callback", scopes={Scope.OPERATE})
    repository = await service.register_repository(
        principal,
        RepositoryCreate(owner="allowed", name="project"),
    )
    queue = await service.trigger_pipeline(
        principal,
        TriggerRequest(
            repository_id=repository.id,
            commit_sha="b" * 40,
            base_sha="a" * 40,
            pull_request=17,
        ),
    )
    assert queue.build_id is not None
    result = PipelineResult(
        repository="allowed/project",
        commit_sha="b" * 40,
        base_sha="a" * 40,
        build_id=queue.build_id,
        pull_request=17,
        status="passed",
    )
    completion = BuildCompletion(
        callback_id="review-callback",
        timestamp=int(time.time()),
        build_id=queue.build_id,
        result=result,
        review=ReviewPolicy(critical_severities={"critical"}),
        diff=(
            "diff --git a/src/example.py b/src/example.py\n"
            "--- a/src/example.py\n"
            "+++ b/src/example.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    )
    broker = FakeReviewBroker(severity)
    service.review_broker = broker  # type: ignore[assignment]

    await service.process_ai_review(principal, completion)
    await service.process_ai_review(principal, completion)

    assert len(broker.calls) == 1
    assert broker.calls[0]["base_sha"] == "a" * 40
    assert broker.calls[0]["head_sha"] == "b" * 40
    assert len(service.github.reviews) == 1
    review = service.github.reviews[0]
    assert review["pull_number"] == 17
    assert review["commit_sha"] == "b" * 40
    assert review["body"] == "One bounded inline finding."
    assert review["comments"] == [
        {
            "path": "src/example.py",
            "line": 1,
            "side": "RIGHT",
            "body": (
                f"**{severity.value.upper()}: Unsafe change**\n\nHandle this added line safely."
            ),
        }
    ]
    final_statuses = [
        item
        for item in service.github.statuses
        if item["context"] == "jenkinsservice/ai-review" and item["state"] in {"success", "failure"}
    ]
    assert final_statuses[0]["state"] == expected_state
    assert final_statuses[1]["state"] == expected_state

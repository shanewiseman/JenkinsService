from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from jenkins_service.app import create_app
from jenkins_service.clients import UpstreamError
from jenkins_service.models import RepositoryCreate, Scope
from jenkins_service.webhook import dispatch_github_webhook, process_github_webhook


def test_health_auth_scope_and_repository_flow(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 401
        forbidden = client.post(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer read-token"},
            json={"owner": "allowed", "name": "project"},
        )
        assert forbidden.status_code == 403
        created = client.post(
            "/api/v1/repositories",
            headers={
                "Authorization": "Bearer operate-token",
                "X-Request-ID": "request-123",
            },
            json={"owner": "allowed", "name": "project"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["full_name"] == "allowed/project"
        assert created.headers["X-Request-ID"] == "request-123"
        assert store.audit[-1].request_id == "request-123"
        listed = client.get(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer read-token"},
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1


def test_origin_and_repository_allowlist(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    with TestClient(app) as client:
        origin = client.get(
            "/api/v1/capabilities",
            headers={
                "Authorization": "Bearer read-token",
                "Origin": "https://evil.example",
            },
        )
        assert origin.status_code == 403
        assert origin.headers["X-Request-ID"]
        assert origin.headers["X-Content-Type-Options"] == "nosniff"
        assert origin.headers["Referrer-Policy"] == "no-referrer"
        denied = client.post(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer operate-token"},
            json={"owner": "other", "name": "project"},
        )
        assert denied.status_code == 400


def test_operation_requests_reject_unknown_fields(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer operate-token"},
            json={"owner": "allowed", "name": "project", "unexpected": True},
        )
    assert response.status_code == 422


def test_operation_requests_return_stable_error_for_invalid_json(
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
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/repositories",
            headers={
                "Authorization": "Bearer operate-token",
                "Content-Type": "application/json",
            },
            content="{",
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid JSON payload"}


async def test_webhook_dispatch_uses_operate_scope(
    store,
    service,
    monkeypatch,
) -> None:
    await store.create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    triggers = []

    async def capture_trigger(principal, payload) -> None:
        triggers.append((principal, payload))

    monkeypatch.setattr(service, "trigger_pipeline", capture_trigger)
    await dispatch_github_webhook(
        service,
        "push",
        {
            "after": "a" * 40,
            "ref": "refs/heads/feature/standalone",
            "repository": {"full_name": "allowed/project"},
        },
    )

    assert len(triggers) == 1
    assert triggers[0][0].scopes == {Scope.OPERATE}
    assert triggers[0][1].branch == "feature/standalone"


async def test_webhook_dispatch_ignores_malformed_payload_shapes(
    store,
    service,
    monkeypatch,
) -> None:
    await store.create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    triggered = []

    async def capture_trigger(principal, payload) -> None:
        triggered.append((principal, payload))

    monkeypatch.setattr(service, "trigger_pipeline", capture_trigger)
    malformed = [
        ("push", {}),
        ("push", {"repository": [], "after": "a" * 40}),
        (
            "push",
            {
                "repository": {"full_name": "allowed/project"},
                "after": 123,
                "ref": "refs/heads/main",
            },
        ),
        (
            "push",
            {
                "repository": {"full_name": "allowed/project"},
                "after": "a" * 40,
                "ref": "refs/tags/v1",
            },
        ),
        (
            "pull_request",
            {
                "repository": {"full_name": "allowed/project"},
                "action": "opened",
                "pull_request": {},
            },
        ),
        (
            "pull_request",
            {
                "repository": {"full_name": "allowed/project"},
                "action": "opened",
                "pull_request": {
                    "head": {"sha": "a" * 40},
                    "number": "1",
                },
            },
        ),
    ]

    for event, payload in malformed:
        await dispatch_github_webhook(service, event, payload)

    assert triggered == []


async def test_webhook_dispatch_routes_pull_request_to_stable_pr_job(
    store,
    service,
    monkeypatch,
) -> None:
    await store.create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    triggered = []

    async def capture_trigger(principal, payload) -> None:
        triggered.append(payload)

    monkeypatch.setattr(service, "trigger_pipeline", capture_trigger)
    await dispatch_github_webhook(
        service,
        "pull_request",
        {
            "repository": {"full_name": "allowed/project"},
            "action": "synchronize",
            "pull_request": {
                "number": 17,
                "head": {
                    "sha": "a" * 40,
                    "ref": "feature/display-refresh",
                },
                "base": {"sha": "b" * 40},
            },
        },
    )

    assert len(triggered) == 1
    assert triggered[0].branch == "feature/display-refresh"
    assert triggered[0].pull_request == 17


async def test_webhook_processing_rejects_malformed_repository_shape(
    store,
    service,
) -> None:
    delivery, inserted = await process_github_webhook(
        service,
        "malformed-delivery",
        "push",
        {"repository": []},
    )

    assert inserted
    assert not delivery.accepted
    assert delivery.repository == "unknown"
    assert delivery.reason == "malformed repository metadata"

    outside, outside_inserted = await process_github_webhook(
        service,
        "outside-allowlist-delivery",
        "push",
        {"repository": {"full_name": "outside/project"}},
    )
    assert outside_inserted
    assert not outside.accepted
    assert outside.reason == "repository outside configured allowlist"


def test_webhook_signature_dedup_and_dispatch(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    payload = {
        "after": "a" * 40,
        "ref": "refs/heads/main",
        "repository": {"full_name": "allowed/project"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        # An allowlisted but unregistered repo is accepted without a trigger.
        first = client.post("/webhooks/github", content=body, headers=headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False
        second = client.post("/webhooks/github", content=body, headers=headers)
        assert second.status_code == 202
        assert second.json()["duplicate"] is True
        bad = client.post(
            "/webhooks/github",
            content=body,
            headers={**headers, "X-Hub-Signature-256": "sha256=bad"},
        )
        assert bad.status_code == 401


def test_webhook_background_audit_retains_request_id(
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
    payload = {
        "after": "a" * 40,
        "ref": "refs/heads/main",
        "repository": {"full_name": "allowed/project"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer operate-token"},
            json={"owner": "allowed", "name": "project"},
        )
        assert created.status_code == 200

        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "request-id-delivery",
                "X-GitHub-Event": "push",
                "X-Request-ID": "webhook-request-123",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    assert store.audit[-1].action == "trigger_pipeline"
    assert store.audit[-1].request_id == "webhook-request-123"


def test_webhook_background_failure_is_durably_audited(
    settings,
    store,
    service,
    authenticator,
    monkeypatch,
) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    payload = {
        "after": "a" * 40,
        "ref": "refs/heads/feature/first-push",
        "repository": {"full_name": "allowed/project"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()

    async def fail_trigger(*args, **kwargs) -> None:
        raise UpstreamError("transient Jenkins reconciliation failure")

    monkeypatch.setattr(service, "trigger_pipeline", fail_trigger)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/repositories",
            headers={"Authorization": "Bearer operate-token"},
            json={"owner": "allowed", "name": "project"},
        )
        assert created.status_code == 200
        response = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "failed-dispatch-delivery",
                "X-GitHub-Event": "push",
                "X-Request-ID": "failed-webhook-request",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    failure = store.audit[-1]
    assert failure.action == "github_webhook_dispatch"
    assert failure.target == "failed-dispatch-delivery"
    assert failure.outcome == "failed"
    assert failure.request_id == "failed-webhook-request"
    assert failure.detail == {
        "event": "push",
        "repository": "allowed/project",
        "error_type": "UpstreamError",
        "reason": "transient Jenkins reconciliation failure",
    }


def test_webhook_body_limit_is_enforced_while_streaming(
    settings,
    store,
    service,
    authenticator,
) -> None:
    settings.max_webhook_bytes = 8
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/github",
            content=b'{"larger":true}',
        )
    assert response.status_code == 413


def test_openapi_contains_registry_not_raw_jenkins(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/pipelines/trigger" in paths
    assert "/api/v1/extensions/run" in paths
    assert "/api/v1/builds/{build_id}/artifacts/ai-review.json" in paths
    assert all("script" not in path and "credential" not in path for path in paths)


def test_openapi_uses_concrete_audit_response_models(
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
    paths = app.openapi()["paths"]
    deliveries = paths["/api/v1/webhooks/deliveries"]["get"]["responses"]["200"]
    audits = paths["/api/v1/audit"]["get"]["responses"]["200"]

    delivery_items = deliveries["content"]["application/json"]["schema"]["items"]
    audit_items = audits["content"]["application/json"]["schema"]["items"]
    assert delivery_items["$ref"].endswith("/WebhookDelivery")
    assert audit_items["$ref"].endswith("/AuditRecord")

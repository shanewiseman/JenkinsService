from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from jenkins_service.app import create_app


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
            headers={"Authorization": "Bearer operate-token"},
            json={"owner": "allowed", "name": "project"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["full_name"] == "allowed/project"
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


def test_webhook_signature_dedup_and_dispatch(settings, store, service, authenticator) -> None:
    app = create_app(
        settings=settings,
        store=store,
        service=service,
        authenticator=authenticator,
    )
    payload = {
        "after": "a" * 40,
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
    assert all("script" not in path and "credential" not in path for path in paths)

from __future__ import annotations

from fastapi.testclient import TestClient

from jenkins_service.app import create_app


def test_streamable_http_requires_auth_and_initializes(
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
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "test-client",
                "version": "1.0",
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        unauthorized = client.post(
            "/mcp",
            headers=headers,
            json=request,
        )
        assert unauthorized.status_code == 401

        initialized = client.post(
            "/mcp",
            headers={
                **headers,
                "Authorization": "Bearer read-token",
            },
            json=request,
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"] == ("JenkinsService")

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
REPOSITORY_ID = "12345678-1234-1234-1234-123456789abc"


@contextmanager
def activation_server(
    repositories: list[dict[str, Any]],
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def request_body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length)) if length else None

        def respond(self, payload: Any) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def record(self, body: dict[str, Any] | None = None) -> None:
            calls.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "origin": self.headers.get("Origin"),
                    "body": body,
                }
            )

        def do_GET(self) -> None:
            self.record()
            assert self.path == "/api/v1/repositories"
            self.respond(repositories)

        def do_POST(self) -> None:
            body = self.request_body()
            self.record(body)
            if self.path == "/api/v1/repositories":
                repository = {
                    **(body or {}),
                    "id": REPOSITORY_ID,
                    "full_name": "/".join((str(body["owner"]), str(body["name"]))),
                }
                repositories.append(repository)
                self.respond(repository)
                return
            assert self.path == "/api/v1/repositories/scan"
            self.respond({"id": REPOSITORY_ID, "detail": "scan requested"})

        def do_PATCH(self) -> None:
            body = self.request_body()
            self.record(body)
            assert self.path == f"/api/v1/repositories/{REPOSITORY_ID}"
            repositories[0].update(body or {})
            self.respond(repositories[0])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield (f"http://{host}:{port}", calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_activation(url: str, branch: str = "master") -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "JENKINSSERVICE_URL": url,
        "JENKINSSERVICE_ORIGIN": "https://ci.example.test",
        "JENKINSSERVICE_ADMIN_TOKEN": "admin-secret",
    }
    return subprocess.run(  # noqa: S603 - fixed repository-owned script
        [
            str(ROOT / "scripts/activate-repository.sh"),
            "allowed/project",
            branch,
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_activation_script_registers_and_scans_repository() -> None:
    with activation_server([]) as (url, calls):
        result = run_activation(url)

    assert result.stdout == (f"Activated allowed/project (id={REPOSITORY_ID}, branch=master)\n")
    assert [(call["method"], call["path"]) for call in calls] == [
        ("GET", "/api/v1/repositories"),
        ("POST", "/api/v1/repositories"),
        ("POST", "/api/v1/repositories/scan"),
    ]
    assert calls[1]["body"] == {
        "owner": "allowed",
        "name": "project",
        "default_branch": "master",
        "enabled": True,
    }
    assert calls[2]["body"] == {"repository_id": REPOSITORY_ID}
    assert all(call["authorization"] == "Bearer admin-secret" for call in calls)
    assert all(call["origin"] == "https://ci.example.test" for call in calls)


def test_activation_script_reconciles_existing_repository_before_scan() -> None:
    existing = {
        "owner": "allowed",
        "name": "project",
        "default_branch": "main",
        "enabled": False,
        "id": REPOSITORY_ID,
        "full_name": "allowed/project",
    }
    with activation_server([existing]) as (url, calls):
        result = run_activation(url, branch="release")

    assert result.stdout.endswith("branch=release)\n")
    assert [(call["method"], call["path"]) for call in calls] == [
        ("GET", "/api/v1/repositories"),
        ("PATCH", f"/api/v1/repositories/{REPOSITORY_ID}"),
        ("POST", "/api/v1/repositories/scan"),
    ]
    assert calls[1]["body"] == {"default_branch": "release", "enabled": True}

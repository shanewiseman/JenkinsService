from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from jenkins_service.config import Settings
from jenkins_service.extensions import ExtensionCatalog
from jenkins_service.models import ExtensionOutput, Scope
from jenkins_service.security import TokenAuthenticator, hash_token
from jenkins_service.service import JenkinsService
from jenkins_service.store import MemoryStore


class FakeJenkins:
    def __init__(self) -> None:
        self.scans: list[tuple[str, str]] = []
        self.triggers: list[tuple[str, str, str, int | None]] = []
        self.cancellations: list[tuple[str, int]] = []
        self.queue_cancellations: list[int] = []
        self.jobs: list[tuple[str, str, str, str, int | None]] = []
        self.queue_state: dict[str, Any] | None = None
        self.build_state: dict[str, Any] | None = None
        self.result_bytes: bytes | None = None

    @staticmethod
    def job_name(owner: str, name: str) -> str:
        return f"{owner}--{name}"

    @staticmethod
    def legacy_job_path(owner: str, name: str) -> str:
        return f"repositories/{owner}--{name}--legacy"

    @staticmethod
    def branch_job_name(branch: str, pull_request: int | None = None) -> str:
        if pull_request is not None:
            return f"PR-{pull_request}"
        slug = branch.replace("/", "-")[:120]
        digest = hashlib.sha256(branch.encode()).hexdigest()[:16]
        return f"branch-{slug}-{digest}"

    @classmethod
    def managed_job_path(
        cls,
        owner: str,
        name: str,
        branch: str,
        pull_request: int | None = None,
    ) -> str:
        return (
            f"repositories/{cls.job_name(owner, name)}/{cls.branch_job_name(branch, pull_request)}"
        )

    async def close(self) -> None:
        return None

    async def ensure_job(
        self,
        owner: str,
        name: str,
        default_branch: str,
        branch: str | None = None,
        pull_request: int | None = None,
    ) -> None:
        self.jobs.append(
            (
                owner,
                name,
                default_branch,
                branch or default_branch,
                pull_request,
            )
        )

    async def delete_job(self, owner: str, name: str) -> None:
        return None

    async def scan(self, owner: str, name: str, default_branch: str) -> None:
        self.scans.append((owner, name))

    async def trigger(
        self,
        owner: str,
        name: str,
        sha: str,
        pull_request: int | None,
        base_sha: str | None = None,
        build_id: str | None = None,
        branch: str | None = None,
    ) -> int:
        self.triggers.append((owner, name, sha, pull_request))
        return 42

    async def cancel(self, job: str, build_number: int) -> None:
        self.cancellations.append((job, build_number))

    async def cancel_queue(self, queue_id: int) -> None:
        self.queue_cancellations.append(queue_id)

    async def queue_status(self, queue_id: int) -> dict[str, Any] | None:
        return self.queue_state

    async def build_status(
        self,
        job: str,
        build_number: int,
    ) -> dict[str, Any] | None:
        return self.build_state

    async def pipeline_result(
        self,
        job: str,
        build_number: int,
        max_bytes: int,
    ) -> bytes | None:
        return self.result_bytes

    async def progressive_log(
        self,
        job: str,
        build_number: int,
        start: int,
    ) -> tuple[str, int, bool]:
        return ("build log", start + 9, False)


class FakeGitHub:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str, dict[str, Any], set[str]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.pull_request_diffs: list[dict[str, Any]] = []
        self.diff = (
            "diff --git a/src/example.py b/src/example.py\n"
            "--- a/src/example.py\n"
            "+++ b/src/example.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

    async def close(self) -> None:
        return None

    async def set_status(
        self,
        repository: str,
        sha: str,
        *,
        context: str,
        state: str,
        description: str,
        target_url: str | None = None,
    ) -> dict[str, Any]:
        value = {
            "repository": repository,
            "sha": sha,
            "context": context,
            "state": state,
            "description": description,
            "target_url": target_url,
        }
        self.statuses.append(value)
        return {"id": len(self.statuses), **value}

    async def create_review(
        self,
        repository: str,
        pull_number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        value = {
            "repository": repository,
            "pull_number": pull_number,
            "commit_sha": commit_sha,
            "body": body,
            "comments": comments,
        }
        self.reviews.append(value)
        return {"id": len(self.reviews), **value}

    async def pull_request_diff(
        self,
        repository: str,
        pull_number: int,
        *,
        base_sha: str,
        head_sha: str,
        max_bytes: int,
    ) -> str:
        value = {
            "repository": repository,
            "pull_number": pull_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "max_bytes": max_bytes,
        }
        self.pull_request_diffs.append(value)
        if len(self.diff.encode("utf-8")) > max_bytes:
            raise ValueError("diff exceeds configured limit")
        return self.diff

    async def execute_action(
        self,
        repository: str,
        action: str,
        payload: dict[str, Any],
        allowed: set[str],
    ) -> dict[str, Any]:
        self.actions.append((repository, action, payload, allowed))
        return {"id": 99}


class FakeRunner:
    def __init__(self, output: ExtensionOutput | None = None) -> None:
        self.output = output or ExtensionOutput()
        self.calls: list[tuple[str, str]] = []

    async def close(self) -> None:
        return None

    async def run(
        self,
        manifest: Any,
        action: str,
        payload: Any,
    ) -> ExtensionOutput:
        self.calls.append((manifest.id, action))
        return self.output


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    webhook = tmp_path / "webhook"
    webhook.write_text("webhook-secret", encoding="utf-8")
    return Settings(
        public_base_url="https://ci.example.test",
        require_https=False,
        github_allowlist=["allowed/*"],
        extension_allowlist=["reference-review"],
        allowed_origins=["https://client.example.test"],
        github_webhook_secret_file=webhook,
        api_tokens_file=tmp_path / "missing-api-tokens",
        jenkins_token_file=tmp_path / "missing-jenkins-token",
        github_write_token_file=tmp_path / "missing-github-token",
        database_password_file=None,
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def authenticator() -> TokenAuthenticator:
    return TokenAuthenticator(
        [
            (
                "reader",
                {Scope.READ},
                hash_token("read-token"),
            ),
            (
                "operator",
                {Scope.OPERATE},
                hash_token("operate-token"),
            ),
            (
                "admin",
                {Scope.ADMIN},
                hash_token("admin-token"),
            ),
        ]
    )


@pytest.fixture
def service(store: MemoryStore) -> JenkinsService:
    root = Path(__file__).parents[1]
    return JenkinsService(
        store=store,
        jenkins=FakeJenkins(),  # type: ignore[arg-type]
        github=FakeGitHub(),  # type: ignore[arg-type]
        extension_runner=FakeRunner(),  # type: ignore[arg-type]
        extension_catalog=ExtensionCatalog.from_directory(
            root / "extensions",
            {"reference-review"},
        ),
        github_allowlist=["allowed/*"],
        service_version="test",
    )

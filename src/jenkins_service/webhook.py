from __future__ import annotations

from typing import Any

from .models import Principal, Scope, TriggerRequest, WebhookDelivery
from .service import JenkinsService


async def process_github_webhook(
    service: JenkinsService,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
) -> tuple[WebhookDelivery, bool]:
    repository_name = payload.get("repository", {}).get("full_name", "")
    allowed = bool(repository_name) and service.repository_allowed(repository_name)
    delivery = WebhookDelivery(
        delivery_id=delivery_id,
        event=event,
        repository=repository_name or "unknown",
        accepted=allowed,
        reason=None if allowed else "repository outside configured allowlist",
    )
    inserted = await service.store.record_webhook_once(delivery)
    return delivery, inserted


async def dispatch_github_webhook(
    service: JenkinsService,
    event: str,
    payload: dict[str, Any],
) -> None:
    full_name = payload["repository"]["full_name"]
    repository = next(
        (
            item
            for item in await service.store.list_repositories()
            if item.full_name.lower() == full_name.lower() and item.enabled
        ),
        None,
    )
    if repository is None:
        return
    principal = Principal(token_id="github-webhook", scopes={Scope.ADMIN})
    if event == "push":
        commit_sha = payload.get("after")
        if commit_sha and commit_sha != "0" * 40:
            await service.trigger_pipeline(
                principal,
                TriggerRequest(repository_id=repository.id, commit_sha=commit_sha),
            )
    elif event == "pull_request" and payload.get("action") in {
        "opened",
        "reopened",
        "synchronize",
        "ready_for_review",
    }:
        pull_request = payload["pull_request"]
        # The shared library checks out this source SHA but loads its Jenkinsfile
        # and pipeline contract from the trusted target branch for fork builds.
        await service.trigger_pipeline(
            principal,
            TriggerRequest(
                repository_id=repository.id,
                commit_sha=pull_request["head"]["sha"],
                pull_request=pull_request["number"],
            ),
        )

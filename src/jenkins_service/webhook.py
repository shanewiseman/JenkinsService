from __future__ import annotations

import re
from typing import Any

from .models import Principal, Scope, TriggerRequest, WebhookDelivery, validate_branch_name
from .service import JenkinsService


def _repository_full_name(payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    full_name = repository.get("full_name")
    return full_name if isinstance(full_name, str) and full_name else None


def _commit_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{40}", value) else None


def _branch_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return validate_branch_name(value)
    except ValueError:
        return None


def _push_branch(value: Any) -> str | None:
    prefix = "refs/heads/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    return _branch_name(value.removeprefix(prefix))


async def process_github_webhook(
    service: JenkinsService,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
) -> tuple[WebhookDelivery, bool]:
    repository_name = _repository_full_name(payload)
    allowed = repository_name is not None and service.repository_allowed(repository_name)
    reason = (
        None
        if allowed
        else "malformed repository metadata"
        if repository_name is None
        else "repository outside configured allowlist"
    )
    delivery = WebhookDelivery(
        delivery_id=delivery_id,
        event=event,
        repository=repository_name or "unknown",
        accepted=allowed,
        reason=reason,
    )
    inserted = await service.store.record_webhook_once(delivery)
    return delivery, inserted


async def dispatch_github_webhook(
    service: JenkinsService,
    event: str,
    payload: dict[str, Any],
) -> None:
    full_name = _repository_full_name(payload)
    if full_name is None:
        return
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
    if event == "push":
        commit_sha = _commit_sha(payload.get("after"))
        branch = _push_branch(payload.get("ref"))
        if commit_sha is None or commit_sha == "0" * 40 or branch is None:
            return
        await service.trigger_pipeline(
            Principal(token_id="github-webhook", scopes={Scope.OPERATE}),
            TriggerRequest(
                repository_id=repository.id,
                commit_sha=commit_sha,
                branch=branch,
            ),
        )
    elif event == "pull_request" and payload.get("action") in {
        "opened",
        "reopened",
        "synchronize",
        "ready_for_review",
    }:
        pull_request = payload.get("pull_request")
        if not isinstance(pull_request, dict):
            return
        head = pull_request.get("head")
        base = pull_request.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            return
        commit_sha = _commit_sha(head.get("sha"))
        base_sha = _commit_sha(base.get("sha"))
        branch = _branch_name(head.get("ref"))
        pull_request_number = pull_request.get("number")
        if (
            commit_sha is None
            or base_sha is None
            or branch is None
            or not isinstance(pull_request_number, int)
            or isinstance(pull_request_number, bool)
            or pull_request_number < 1
        ):
            return
        # The shared library checks out this source SHA but loads its Jenkinsfile
        # and pipeline contract from the trusted target branch for fork builds.
        await service.trigger_pipeline(
            Principal(token_id="github-webhook", scopes={Scope.OPERATE}),
            TriggerRequest(
                repository_id=repository.id,
                commit_sha=commit_sha,
                base_sha=base_sha,
                branch=branch,
                pull_request=pull_request_number,
            ),
        )

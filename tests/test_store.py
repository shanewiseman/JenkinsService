from __future__ import annotations

import pytest

from jenkins_service.models import (
    ExtensionRun,
    RepositoryCreate,
    RepositoryUpdate,
    WebhookDelivery,
)
from jenkins_service.store import ConflictError, MemoryStore, NotFoundError


async def test_repository_lifecycle() -> None:
    store = MemoryStore()
    repository = await store.create_repository(RepositoryCreate(owner="allowed", name="project"))
    assert (await store.get_repository(repository.id)).full_name == "allowed/project"
    updated = await store.update_repository(
        repository.id, RepositoryUpdate(default_branch="stable")
    )
    assert updated.default_branch == "stable"
    with pytest.raises(ConflictError):
        await store.create_repository(RepositoryCreate(owner="Allowed", name="Project"))
    await store.delete_repository(repository.id)
    with pytest.raises(NotFoundError):
        await store.get_repository(repository.id)


async def test_webhook_delivery_is_atomic_and_idempotent() -> None:
    store = MemoryStore()
    delivery = WebhookDelivery(
        delivery_id="delivery-1",
        event="push",
        repository="allowed/project",
        accepted=True,
    )
    assert await store.record_webhook_once(delivery)
    assert not await store.record_webhook_once(delivery)
    assert len(await store.list_webhooks()) == 1


async def test_extension_idempotency_key_returns_original() -> None:
    store = MemoryStore()
    first = ExtensionRun(
        extension_id="reference-review",
        image_digest="sha256:" + "1" * 64,
        action="review",
        target="allowed/project",
        idempotency_key="key-12345",
    )
    second = first.model_copy(update={"id": __import__("uuid").uuid4()})
    assert (await store.save_extension_run(first)).id == first.id
    assert (await store.save_extension_run(second)).id == first.id

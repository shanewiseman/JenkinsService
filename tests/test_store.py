from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from jenkins_service.models import (
    AuditRecord,
    Build,
    ExtensionRun,
    PipelineResult,
    QueueItem,
    RepositoryCreate,
    RepositoryUpdate,
    WebhookDelivery,
)
from jenkins_service.store import (
    ConflictError,
    MemoryStore,
    NotFoundError,
    PostgresStore,
)


class RecordingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.calls.append((query, args))
        if "extension_runs" in query:
            return {"data": json.loads(args[2])}
        return {"delivery_id": args[0]}


class RepositoryReadPool:
    def __init__(self, data: str) -> None:
        self.data = data

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM repositories" in query
        return [{"data": self.data}]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "FROM repositories" in query
        return {"data": self.data}


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


async def test_postgres_repository_reads_decode_jsonb_text() -> None:
    repository = await MemoryStore().create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    pool = RepositoryReadPool(
        repository.model_dump_json(exclude={"full_name"}),
    )
    store = PostgresStore("postgresql://unused")
    store.pool = pool  # type: ignore[assignment]

    assert await store.list_repositories() == [repository]
    assert await store.get_repository(repository.id) == repository


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


async def test_postgres_queue_and_build_columns_use_model_timestamps() -> None:
    pool = RecordingPool()
    store = PostgresStore("postgresql://unused")
    store.pool = pool  # type: ignore[assignment]
    created_at = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    updated_at = datetime(2024, 1, 2, 3, 5, tzinfo=UTC)
    repository = await MemoryStore().create_repository(
        RepositoryCreate(owner="allowed", name="project"),
    )
    queue = QueueItem(
        repository_id=repository.id,
        jenkins_queue_id=42,
        commit_sha="a" * 40,
        created_at=created_at,
    )
    build = Build(
        repository_id=repository.id,
        jenkins_job="repositories/allowed--project",
        result=PipelineResult(
            repository=repository.full_name,
            commit_sha="a" * 40,
            status="queued",
        ),
        created_at=created_at,
        updated_at=updated_at,
    )

    await store.save_queue_item(queue)
    await store.save_build(build)

    queue_sql, queue_args = pool.calls[0]
    assert "(id, repository_id, data, created_at)" in queue_sql
    assert "created_at = EXCLUDED.created_at" in queue_sql
    assert queue_args[3] == created_at

    build_sql, build_args = pool.calls[1]
    assert "(id, repository_id, data, created_at, updated_at)" in build_sql
    assert "created_at = EXCLUDED.created_at" in build_sql
    assert "updated_at = EXCLUDED.updated_at" in build_sql
    assert build_args[3:] == (created_at, updated_at)


async def test_postgres_ordered_audit_columns_use_model_timestamps() -> None:
    pool = RecordingPool()
    store = PostgresStore("postgresql://unused")
    store.pool = pool  # type: ignore[assignment]
    created_at = datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
    webhook = WebhookDelivery(
        delivery_id="delivery-1",
        event="push",
        repository="allowed/project",
        accepted=True,
        received_at=created_at,
    )
    extension = ExtensionRun(
        extension_id="reference-review",
        image_digest="sha256:" + "1" * 64,
        action="review",
        target="allowed/project",
        idempotency_key="key-12345",
        created_at=created_at,
    )
    audit = AuditRecord(
        actor="operator",
        action="test",
        target="allowed/project",
        request_id="request-1",
        outcome="succeeded",
        created_at=created_at,
    )

    assert await store.record_webhook_once(webhook)
    await store.save_extension_run(extension)
    await store.record_audit(audit)

    webhook_sql, webhook_args = pool.calls[0]
    assert "(delivery_id, data, received_at)" in webhook_sql
    assert webhook_args[2] == created_at

    extension_sql, extension_args = pool.calls[1]
    assert "(id, idempotency_key, data, created_at)" in extension_sql
    assert "created_at = CASE" in extension_sql
    assert extension_args[3] == created_at

    audit_sql, audit_args = pool.calls[2]
    assert "(id, actor, action, target, data, created_at)" in audit_sql
    assert audit_args[5] == created_at

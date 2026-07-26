from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from .models import (
    AuditRecord,
    Build,
    ExtensionRun,
    QueueItem,
    Repository,
    RepositoryCreate,
    RepositoryUpdate,
    WebhookDelivery,
    now_utc,
)


class ConflictError(Exception):
    pass


class NotFoundError(Exception):
    pass


def _model_from_json[ModelT: BaseModel](model: type[ModelT], value: object) -> ModelT:
    if isinstance(value, str | bytes | bytearray):
        return model.model_validate_json(value)
    return model.model_validate(value)


class Store(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> bool: ...

    async def create_repository(
        self,
        value: RepositoryCreate,
    ) -> Repository: ...

    async def list_repositories(self) -> list[Repository]: ...

    async def get_repository(
        self,
        repository_id: UUID,
    ) -> Repository: ...

    async def update_repository(
        self,
        repository_id: UUID,
        value: RepositoryUpdate,
    ) -> Repository: ...

    async def delete_repository(
        self,
        repository_id: UUID,
    ) -> None: ...

    async def save_queue_item(
        self,
        value: QueueItem,
    ) -> QueueItem: ...

    async def list_queue_items(self) -> list[QueueItem]: ...

    async def get_queue_item(
        self,
        queue_item_id: UUID,
    ) -> QueueItem: ...

    async def save_build(self, value: Build) -> Build: ...

    async def get_build(self, build_id: UUID) -> Build: ...

    async def list_builds(self) -> list[Build]: ...

    async def rebind_jenkins_job(
        self,
        repository_id: UUID,
        old_job: str,
        new_job: str,
    ) -> None: ...

    async def record_webhook_once(
        self,
        value: WebhookDelivery,
    ) -> bool: ...

    async def list_webhooks(self) -> list[WebhookDelivery]: ...

    async def save_extension_run(
        self,
        value: ExtensionRun,
    ) -> ExtensionRun: ...

    async def get_extension_run(
        self,
        run_id: UUID,
    ) -> ExtensionRun: ...

    async def get_extension_run_by_key(
        self,
        key: str,
    ) -> ExtensionRun | None: ...

    async def list_extension_runs(
        self,
    ) -> list[ExtensionRun]: ...

    async def record_audit(
        self,
        value: AuditRecord,
    ) -> None: ...

    async def list_audit(self) -> list[AuditRecord]: ...


class MemoryStore:
    """Deterministic store for tests and local contract work."""

    def __init__(self) -> None:
        self.repositories: dict[UUID, Repository] = {}
        self.queue_items: dict[UUID, QueueItem] = {}
        self.builds: dict[UUID, Build] = {}
        self.webhooks: dict[str, WebhookDelivery] = {}
        self.extension_runs: dict[UUID, ExtensionRun] = {}
        self.extension_keys: dict[str, UUID] = {}
        self.audit: list[AuditRecord] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def create_repository(
        self,
        value: RepositoryCreate,
    ) -> Repository:
        duplicate = any(
            item.full_name.lower() == value.full_name.lower() for item in self.repositories.values()
        )
        if duplicate:
            raise ConflictError(f"repository already registered: {value.full_name}")
        result = Repository(**value.model_dump())
        self.repositories[result.id] = result
        return result

    async def list_repositories(self) -> list[Repository]:
        return sorted(
            self.repositories.values(),
            key=lambda item: item.full_name.lower(),
        )

    async def get_repository(
        self,
        repository_id: UUID,
    ) -> Repository:
        try:
            return self.repositories[repository_id]
        except KeyError as exc:
            raise NotFoundError(f"repository not found: {repository_id}") from exc

    async def update_repository(
        self,
        repository_id: UUID,
        value: RepositoryUpdate,
    ) -> Repository:
        current = await self.get_repository(repository_id)
        updated = current.model_copy(
            update={
                **value.model_dump(exclude_none=True),
                "updated_at": now_utc(),
            }
        )
        self.repositories[repository_id] = updated
        return updated

    async def delete_repository(
        self,
        repository_id: UUID,
    ) -> None:
        if self.repositories.pop(repository_id, None) is None:
            raise NotFoundError(f"repository not found: {repository_id}")

    async def save_queue_item(
        self,
        value: QueueItem,
    ) -> QueueItem:
        self.queue_items[value.id] = value
        return value

    async def list_queue_items(self) -> list[QueueItem]:
        return sorted(
            self.queue_items.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def get_queue_item(
        self,
        queue_item_id: UUID,
    ) -> QueueItem:
        try:
            return self.queue_items[queue_item_id]
        except KeyError as exc:
            raise NotFoundError(f"queue item not found: {queue_item_id}") from exc

    async def save_build(self, value: Build) -> Build:
        self.builds[value.id] = value
        return value

    async def get_build(self, build_id: UUID) -> Build:
        try:
            return self.builds[build_id]
        except KeyError as exc:
            raise NotFoundError(f"build not found: {build_id}") from exc

    async def list_builds(self) -> list[Build]:
        return sorted(
            self.builds.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def rebind_jenkins_job(
        self,
        repository_id: UUID,
        old_job: str,
        new_job: str,
    ) -> None:
        for build_id, build in self.builds.items():
            if build.repository_id == repository_id and build.jenkins_job == old_job:
                self.builds[build_id] = build.model_copy(update={"jenkins_job": new_job})

    async def record_webhook_once(
        self,
        value: WebhookDelivery,
    ) -> bool:
        if value.delivery_id in self.webhooks:
            return False
        self.webhooks[value.delivery_id] = value
        return True

    async def list_webhooks(self) -> list[WebhookDelivery]:
        return sorted(
            self.webhooks.values(),
            key=lambda item: item.received_at,
            reverse=True,
        )

    async def save_extension_run(
        self,
        value: ExtensionRun,
    ) -> ExtensionRun:
        existing_id = self.extension_keys.get(value.idempotency_key)
        if existing_id is not None and existing_id != value.id:
            return self.extension_runs[existing_id]
        self.extension_runs[value.id] = value
        self.extension_keys[value.idempotency_key] = value.id
        return value

    async def get_extension_run(
        self,
        run_id: UUID,
    ) -> ExtensionRun:
        try:
            return self.extension_runs[run_id]
        except KeyError as exc:
            raise NotFoundError(f"extension run not found: {run_id}") from exc

    async def get_extension_run_by_key(
        self,
        key: str,
    ) -> ExtensionRun | None:
        run_id = self.extension_keys.get(key)
        return self.extension_runs.get(run_id) if run_id else None

    async def list_extension_runs(
        self,
    ) -> list[ExtensionRun]:
        return sorted(
            self.extension_runs.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def record_audit(
        self,
        value: AuditRecord,
    ) -> None:
        self.audit.append(value)

    async def list_audit(self) -> list[AuditRecord]:
        return sorted(
            self.audit,
            key=lambda item: item.created_at,
            reverse=True,
        )


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        excluded = {"full_name"} if isinstance(value, Repository) else None
        return value.model_dump_json(exclude=excluded)
    return json.dumps(value)


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("PostgreSQL store is not connected")
        return self.pool

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
            server_settings={"application_name": "jenkins-service"},
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def ping(self) -> bool:
        try:
            return bool(await self._pool().fetchval("SELECT TRUE"))
        except (asyncpg.PostgresError, OSError):
            return False

    async def create_repository(
        self,
        value: RepositoryCreate,
    ) -> Repository:
        result = Repository(**value.model_dump())
        try:
            await self._pool().execute(
                "INSERT INTO repositories (id, full_name, data) VALUES ($1, $2, $3::jsonb)",
                result.id,
                result.full_name.lower(),
                _json(result),
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError(f"repository already registered: {value.full_name}") from exc
        return result

    async def list_repositories(self) -> list[Repository]:
        rows = await self._pool().fetch("SELECT data FROM repositories ORDER BY full_name")
        return [_model_from_json(Repository, row["data"]) for row in rows]

    async def get_repository(
        self,
        repository_id: UUID,
    ) -> Repository:
        row = await self._pool().fetchrow(
            "SELECT data FROM repositories WHERE id = $1",
            repository_id,
        )
        if row is None:
            raise NotFoundError(f"repository not found: {repository_id}")
        return _model_from_json(Repository, row["data"])

    async def update_repository(
        self,
        repository_id: UUID,
        value: RepositoryUpdate,
    ) -> Repository:
        current = await self.get_repository(repository_id)
        result = current.model_copy(
            update={
                **value.model_dump(exclude_none=True),
                "updated_at": now_utc(),
            }
        )
        status = await self._pool().execute(
            "UPDATE repositories SET data = $2::jsonb WHERE id = $1",
            repository_id,
            _json(result),
        )
        if status != "UPDATE 1":
            raise NotFoundError(f"repository not found: {repository_id}")
        return result

    async def delete_repository(
        self,
        repository_id: UUID,
    ) -> None:
        status = await self._pool().execute(
            "DELETE FROM repositories WHERE id = $1",
            repository_id,
        )
        if status != "DELETE 1":
            raise NotFoundError(f"repository not found: {repository_id}")

    async def save_queue_item(
        self,
        value: QueueItem,
    ) -> QueueItem:
        await self._pool().execute(
            """
            INSERT INTO queue_items
                (id, repository_id, data, created_at)
            VALUES ($1, $2, $3::jsonb, $4)
            ON CONFLICT (id)
            DO UPDATE SET
                data = EXCLUDED.data,
                created_at = EXCLUDED.created_at
            """,
            value.id,
            value.repository_id,
            _json(value),
            value.created_at,
        )
        return value

    async def list_queue_items(self) -> list[QueueItem]:
        rows = await self._pool().fetch(
            "SELECT data FROM queue_items ORDER BY created_at DESC LIMIT 500"
        )
        return [_model_from_json(QueueItem, row["data"]) for row in rows]

    async def get_queue_item(
        self,
        queue_item_id: UUID,
    ) -> QueueItem:
        row = await self._pool().fetchrow(
            "SELECT data FROM queue_items WHERE id = $1",
            queue_item_id,
        )
        if row is None:
            raise NotFoundError(f"queue item not found: {queue_item_id}")
        return _model_from_json(QueueItem, row["data"])

    async def save_build(self, value: Build) -> Build:
        await self._pool().execute(
            """
            INSERT INTO builds
                (id, repository_id, data, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, $5)
            ON CONFLICT (id)
            DO UPDATE SET
                data = EXCLUDED.data,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at
            """,
            value.id,
            value.repository_id,
            _json(value),
            value.created_at,
            value.updated_at,
        )
        return value

    async def get_build(self, build_id: UUID) -> Build:
        row = await self._pool().fetchrow(
            "SELECT data FROM builds WHERE id = $1",
            build_id,
        )
        if row is None:
            raise NotFoundError(f"build not found: {build_id}")
        return _model_from_json(Build, row["data"])

    async def list_builds(self) -> list[Build]:
        rows = await self._pool().fetch(
            "SELECT data FROM builds ORDER BY created_at DESC LIMIT 500"
        )
        return [_model_from_json(Build, row["data"]) for row in rows]

    async def rebind_jenkins_job(
        self,
        repository_id: UUID,
        old_job: str,
        new_job: str,
    ) -> None:
        await self._pool().execute(
            """
            UPDATE builds
            SET data = jsonb_set(data, '{jenkins_job}', to_jsonb($3::text), false)
            WHERE repository_id = $1
              AND data->>'jenkins_job' = $2
            """,
            repository_id,
            old_job,
            new_job,
        )

    async def record_webhook_once(
        self,
        value: WebhookDelivery,
    ) -> bool:
        row = await self._pool().fetchrow(
            """
            INSERT INTO webhook_deliveries
                (delivery_id, data, received_at)
            VALUES ($1, $2::jsonb, $3)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING delivery_id
            """,
            value.delivery_id,
            _json(value),
            value.received_at,
        )
        return row is not None

    async def list_webhooks(self) -> list[WebhookDelivery]:
        rows = await self._pool().fetch(
            "SELECT data FROM webhook_deliveries ORDER BY received_at DESC LIMIT 500"
        )
        return [_model_from_json(WebhookDelivery, row["data"]) for row in rows]

    async def save_extension_run(
        self,
        value: ExtensionRun,
    ) -> ExtensionRun:
        row = await self._pool().fetchrow(
            """
            INSERT INTO extension_runs
                (id, idempotency_key, data, created_at)
            VALUES ($1, $2, $3::jsonb, $4)
            ON CONFLICT (idempotency_key)
            DO UPDATE SET
                data = CASE
                    WHEN extension_runs.id = EXCLUDED.id
                        THEN EXCLUDED.data
                    ELSE extension_runs.data
                END,
                created_at = CASE
                    WHEN extension_runs.id = EXCLUDED.id
                        THEN EXCLUDED.created_at
                    ELSE extension_runs.created_at
                END
            RETURNING data
            """,
            value.id,
            value.idempotency_key,
            _json(value),
            value.created_at,
        )
        return _model_from_json(ExtensionRun, row["data"])

    async def get_extension_run(
        self,
        run_id: UUID,
    ) -> ExtensionRun:
        row = await self._pool().fetchrow(
            "SELECT data FROM extension_runs WHERE id = $1",
            run_id,
        )
        if row is None:
            raise NotFoundError(f"extension run not found: {run_id}")
        return _model_from_json(ExtensionRun, row["data"])

    async def get_extension_run_by_key(
        self,
        key: str,
    ) -> ExtensionRun | None:
        row = await self._pool().fetchrow(
            "SELECT data FROM extension_runs WHERE idempotency_key = $1",
            key,
        )
        return _model_from_json(ExtensionRun, row["data"]) if row else None

    async def list_extension_runs(
        self,
    ) -> list[ExtensionRun]:
        rows = await self._pool().fetch(
            "SELECT data FROM extension_runs ORDER BY created_at DESC LIMIT 500"
        )
        return [_model_from_json(ExtensionRun, row["data"]) for row in rows]

    async def record_audit(
        self,
        value: AuditRecord,
    ) -> None:
        await self._pool().execute(
            "INSERT INTO audit_records "
            "(id, actor, action, target, data, created_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
            value.id,
            value.actor,
            value.action,
            value.target,
            _json(value),
            value.created_at,
        )

    async def list_audit(self) -> list[AuditRecord]:
        rows = await self._pool().fetch(
            "SELECT data FROM audit_records ORDER BY created_at DESC LIMIT 1000"
        )
        return [_model_from_json(AuditRecord, row["data"]) for row in rows]

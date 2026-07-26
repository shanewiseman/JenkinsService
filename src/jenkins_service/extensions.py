from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .models import ExtensionManifest, ExtensionOutput


class ExtensionCatalog:
    def __init__(
        self,
        manifests: dict[str, ExtensionManifest],
        schemas: dict[str, tuple[dict[str, Any], dict[str, Any]]],
        allowlist: set[str],
    ) -> None:
        unknown = allowlist - manifests.keys()
        if unknown:
            raise ValueError(f"allowlisted extensions have no manifest: {sorted(unknown)}")
        self._manifests = {
            extension_id: manifest
            for extension_id, manifest in manifests.items()
            if extension_id in allowlist
        }
        self._schemas = {extension_id: schemas[extension_id] for extension_id in allowlist}

    @classmethod
    def from_directory(cls, path: Path, allowlist: set[str]) -> ExtensionCatalog:
        manifests: dict[str, ExtensionManifest] = {}
        schemas: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for manifest_path in sorted(path.glob("*/manifest.json")):
            manifest = ExtensionManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if manifest.id in manifests:
                raise ValueError(f"duplicate extension ID: {manifest.id}")
            manifests[manifest.id] = manifest
            input_schema = json.loads(
                manifest_path.with_name(manifest.input_schema).read_text(encoding="utf-8")
            )
            output_schema = json.loads(
                manifest_path.with_name(manifest.output_schema).read_text(encoding="utf-8")
            )
            Draft202012Validator.check_schema(input_schema)
            Draft202012Validator.check_schema(output_schema)
            schemas[manifest.id] = (input_schema, output_schema)
        return cls(manifests, schemas, allowlist)

    def get(self, extension_id: str) -> ExtensionManifest:
        try:
            return self._manifests[extension_id]
        except KeyError as exc:
            raise ValueError(f"extension is not allowlisted: {extension_id}") from exc

    def list(self) -> list[ExtensionManifest]:
        return list(self._manifests.values())

    def _validate(self, extension_id: str, value: Any, schema_index: int, label: str) -> None:
        self.get(extension_id)
        errors = sorted(
            Draft202012Validator(self._schemas[extension_id][schema_index]).iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            raise ValueError(f"extension {label} does not match schema: {errors[0].message}")

    def validate_input(self, extension_id: str, value: Any) -> None:
        self._validate(extension_id, value, 0, "input")

    def validate_output(self, extension_id: str, value: Any) -> None:
        self._validate(extension_id, value, 1, "output")


class ExtensionRunnerClient:
    def __init__(
        self,
        base_url: str,
        max_output_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_output_bytes = max_output_bytes
        self.client = client or httpx.AsyncClient(timeout=920)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def run(
        self,
        manifest: ExtensionManifest,
        action: str,
        payload: dict[str, Any],
    ) -> ExtensionOutput:
        if action not in manifest.actions:
            raise ValueError(f"extension {manifest.id} does not declare action {action}")
        response = await self.client.post(
            f"{self.base_url}/internal/v1/run",
            json={
                "manifest": manifest.model_dump(mode="json"),
                "action": action,
                "payload": payload,
            },
            timeout=manifest.timeout_seconds + 10,
        )
        response.raise_for_status()
        if len(response.content) > self.max_output_bytes:
            raise ValueError("extension output exceeds configured limit")
        try:
            return ExtensionOutput.model_validate_json(response.content)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ValueError("extension returned invalid output") from exc

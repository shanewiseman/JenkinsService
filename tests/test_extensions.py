from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jenkins_service.extension_runner import create_runner_app
from jenkins_service.extensions import ExtensionCatalog


def test_catalog_enforces_operator_allowlist() -> None:
    path = Path(__file__).parents[1] / "extensions"
    catalog = ExtensionCatalog.from_directory(path, {"reference-review"})
    assert catalog.get("reference-review").actions == ["review"]
    with pytest.raises(ValueError, match="not allowlisted"):
        catalog.get("unknown")
    catalog.validate_input(
        "reference-review",
        {
            "repository": {},
            "build_id": None,
            "inputs": {},
        },
    )
    catalog.validate_output(
        "reference-review",
        {
            "findings": [],
            "requested_github_actions": [],
        },
    )


def test_catalog_rejects_values_outside_extension_schemas() -> None:
    path = Path(__file__).parents[1] / "extensions"
    catalog = ExtensionCatalog.from_directory(path, {"reference-review"})
    with pytest.raises(ValueError, match="input"):
        catalog.validate_input("reference-review", {"inputs": {}})
    with pytest.raises(ValueError, match="output"):
        catalog.validate_output(
            "reference-review",
            {
                "findings": [],
                "requested_github_actions": [],
                "unexpected": True,
            },
        )


def test_catalog_rejects_missing_allowlisted_manifest() -> None:
    path = Path(__file__).parents[1] / "extensions"
    with pytest.raises(ValueError, match="no manifest"):
        ExtensionCatalog.from_directory(path, {"missing"})


def test_explicit_empty_runner_allowlist_does_not_fall_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).parents[1] / "extensions"
    monkeypatch.setenv("EXTENSION_ALLOWLIST", "missing")
    assert create_runner_app(catalog_path=path, allowlist=set()) is not None


def test_runner_returns_403_for_unallowlisted_extension() -> None:
    path = Path(__file__).parents[1] / "extensions"
    manifest = json.loads(
        (path / "reference-review" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["id"] = "unknown-extension"
    app = create_runner_app(
        catalog_path=path,
        allowlist={"reference-review"},
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/run",
            json={
                "manifest": manifest,
                "action": "review",
                "payload": {},
            },
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "extension is not allowlisted"}

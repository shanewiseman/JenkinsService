from __future__ import annotations

from pathlib import Path

import yaml

from jenkins_service.contract import validate_contract_content, validate_contract_file

ROOT = Path(__file__).parents[1]


def test_examples_are_valid() -> None:
    for path in sorted((ROOT / "examples").glob("pipeline*.yaml")):
        result = validate_contract_file(path)
        assert result.valid, (path, result.errors)


def test_missing_required_gate_is_rejected() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["steps"]["tests"] = []
    result = validate_contract_content(yaml.safe_dump(document))
    assert not result.valid
    assert any("tests" in error for error in result.errors)


def test_mutable_image_is_rejected() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["runtime"]["image"] = "python:3.12"
    result = validate_contract_content(yaml.safe_dump(document))
    assert not result.valid
    assert any("sha256" in error for error in result.errors)


def test_runtime_network_is_restricted_to_declared_modes() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["runtime"]["network"] = "host"
    result = validate_contract_content(yaml.safe_dump(document))
    assert not result.valid
    assert any("network" in error for error in result.errors)


def test_paths_secrets_mounts_and_duplicate_ids_are_rejected() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["runtime"]["environment"]["API_TOKEN"] = "bad"
    document["steps"]["tests"][0]["id"] = "lint"
    document["steps"]["tests"][0]["workingDirectory"] = "../outside"
    document["steps"]["tests"][0]["command"] = "docker run --privileged -v /:/host x"
    result = validate_contract_content(yaml.safe_dump(document))
    assert not result.valid
    combined = "\n".join(result.errors)
    assert "secret-like" in combined
    assert "duplicates step ID" in combined
    assert "relative POSIX path" in combined
    assert "forbidden fragment" in combined


def test_docker_client_environment_is_rejected_at_runtime_and_step_levels() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["runtime"]["environment"]["DOCKER_HOST"] = "tcp://docker:2376"
    document["steps"]["tests"][0].setdefault("environment", {})["DOCKER_TLS_VERIFY"] = "1"

    result = validate_contract_content(yaml.safe_dump(document))

    assert not result.valid
    combined = "\n".join(result.errors)
    assert "runtime.environment.DOCKER_HOST" in combined
    assert "steps.tests[0].environment.DOCKER_TLS_VERIFY" in combined
    assert combined.count("Docker client configuration is forbidden") == 2


def test_malformed_document_is_a_validation_result() -> None:
    result = validate_contract_content("steps: [")
    assert not result.valid
    assert result.errors[0].startswith("document parse failed")


def test_structurally_invalid_document_skips_semantic_validation() -> None:
    result = validate_contract_content("steps: []")
    assert not result.valid
    assert any(error.startswith("steps:") for error in result.errors)


def test_dogfood_contract_uses_bootstrap_only_egress_and_critical_review() -> None:
    path = ROOT / ".jenkins" / "pipeline.yaml"
    result = validate_contract_file(path)
    assert result.valid, result.errors
    document = yaml.safe_load(path.read_text())
    assert document["runtime"]["network"] == "none"
    steps = [step for group in document["steps"].values() for step in group]
    assert [step["id"] for step in steps if step.get("network") == "egress"] == [
        "dependency-bootstrap"
    ]
    assert document["review"]["criticalSeverities"] == ["critical"]


def test_review_policy_cannot_expand_the_blocking_severities() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["review"] = {"criticalSeverities": ["critical", "high"]}
    result = validate_contract_content(yaml.safe_dump(document))
    assert not result.valid
    assert any("criticalSeverities" in error for error in result.errors)


def test_review_policy_accepts_safe_literal_excluded_paths() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["review"] = {
        "excludedPaths": ["requirements.lock", "requirements-dev.lock"],
    }

    result = validate_contract_content(yaml.safe_dump(document))

    assert result.valid, result.errors


def test_review_policy_rejects_unsafe_or_duplicate_excluded_paths() -> None:
    document = yaml.safe_load((ROOT / "examples/pipeline.yaml").read_text())
    document["review"] = {
        "excludedPaths": ["../outside.lock", "requirements.lock", "requirements.lock"],
    }

    result = validate_contract_content(yaml.safe_dump(document))

    assert not result.valid
    combined = "\n".join(result.errors)
    assert "unique" in combined
    assert "relative POSIX path" in combined

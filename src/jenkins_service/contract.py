from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .models import ContractValidation

CONTRACT_VERSION = "ci.jenkinsservice.dev/v1"
IMAGE_DIGEST = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:+-]*@sha256:[a-f0-9]{64}$")
SECRET_NAME = re.compile(
    r"(?i)(secret|token|password|passwd|credential|private[_-]?key|api[_-]?key)"
)
FORBIDDEN_ENVIRONMENT_NAMES = frozenset(
    {
        "BUILDKIT_HOST",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
    }
)
FORBIDDEN_COMMAND_FRAGMENTS = (
    "/var/run/docker.sock",
    "docker.sock",
    "--privileged",
    "--mount",
    "-v /",
    "DOCKER_HOST",
    "DOCKER_CERT_PATH",
)


@lru_cache
def contract_schema() -> dict[str, Any]:
    resource = files("jenkins_service").joinpath("schemas/pipeline-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _relative_safe_path(value: str, field: str, errors: list[str]) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        errors.append(f"{field} must be a relative POSIX path without '..'")


def _environment_errors(
    environment: dict[Any, Any],
    field: str,
    errors: list[str],
) -> None:
    for name in environment:
        if not isinstance(name, str):
            continue
        if SECRET_NAME.search(name):
            errors.append(f"{field}.{name}: secret-like names are forbidden")
        if name.upper() in FORBIDDEN_ENVIRONMENT_NAMES:
            errors.append(f"{field}.{name}: Docker client configuration is forbidden")


def _semantic_errors(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    runtime_value = document.get("runtime", {})
    runtime = runtime_value if isinstance(runtime_value, dict) else {}
    image = runtime.get("image", "")
    if isinstance(image, str) and image and not IMAGE_DIGEST.fullmatch(image):
        errors.append("runtime.image must use an immutable @sha256 digest")

    environment_value = runtime.get("environment", {})
    environment = environment_value if isinstance(environment_value, dict) else {}
    _environment_errors(environment, "runtime.environment", errors)

    seen: set[str] = set()
    groups = ("standards", "tests", "custom")
    steps_value = document.get("steps", {})
    steps = steps_value if isinstance(steps_value, dict) else {}
    for group in groups:
        group_value = steps.get(group, [])
        group_steps = group_value if isinstance(group_value, list) else []
        for index, step in enumerate(group_steps):
            prefix = f"steps.{group}[{index}]"
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            if isinstance(step_id, str) and step_id in seen:
                errors.append(f"{prefix}.id duplicates step ID {step_id!r}")
            elif isinstance(step_id, str) and step_id:
                seen.add(step_id)
            working_directory = step.get("workingDirectory", ".")
            if isinstance(working_directory, str):
                _relative_safe_path(
                    working_directory,
                    f"{prefix}.workingDirectory",
                    errors,
                )
            command = step.get("command", [])
            command_text = (
                command
                if isinstance(command, str)
                else " ".join(item for item in command if isinstance(item, str))
                if isinstance(command, list)
                else ""
            )
            for fragment in FORBIDDEN_COMMAND_FRAGMENTS:
                if fragment.lower() in command_text.lower():
                    errors.append(f"{prefix}.command contains forbidden fragment {fragment!r}")
            step_environment_value = step.get("environment", {})
            step_environment = (
                step_environment_value if isinstance(step_environment_value, dict) else {}
            )
            _environment_errors(
                step_environment,
                f"{prefix}.environment",
                errors,
            )
            reports_value = step.get("reports", [])
            reports = reports_value if isinstance(reports_value, list) else []
            for report_index, report in enumerate(reports):
                if isinstance(report, dict) and isinstance(report.get("path"), str):
                    _relative_safe_path(
                        report["path"],
                        f"{prefix}.reports[{report_index}].path",
                        errors,
                    )
    artifacts_value = document.get("artifacts", [])
    artifacts = artifacts_value if isinstance(artifacts_value, list) else []
    for index, artifact in enumerate(artifacts):
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
            _relative_safe_path(artifact["path"], f"artifacts[{index}].path", errors)
    review_value = document.get("review", {})
    review = review_value if isinstance(review_value, dict) else {}
    excluded_paths_value = review.get("excludedPaths", [])
    excluded_paths = excluded_paths_value if isinstance(excluded_paths_value, list) else []
    for index, path in enumerate(excluded_paths):
        if isinstance(path, str):
            _relative_safe_path(path, f"review.excludedPaths[{index}]", errors)
    return errors


def validate_contract_content(content: str, format: str = "yaml") -> ContractValidation:
    try:
        raw = json.loads(content) if format == "json" else yaml.safe_load(content)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        return ContractValidation(valid=False, errors=[f"document parse failed: {exc}"])
    if not isinstance(raw, dict):
        return ContractValidation(valid=False, errors=["pipeline contract must be an object"])

    schema_errors = [
        f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in sorted(
            Draft202012Validator(contract_schema()).iter_errors(raw),
            key=lambda error: list(error.absolute_path),
        )
    ]
    errors = schema_errors + _semantic_errors(raw)
    return ContractValidation(
        valid=not errors,
        errors=errors,
        normalized=raw if not errors else None,
    )


def validate_contract_file(path: Path) -> ContractValidation:
    return validate_contract_content(
        path.read_text(encoding="utf-8"),
        format="json" if path.suffix == ".json" else "yaml",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a JenkinsService pipeline contract")
    parser.add_argument("path", type=Path)
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()
    result = validate_contract_file(args.path)
    if args.json_output:
        print(result.model_dump_json(indent=2))
    elif result.valid:
        print(f"{args.path}: valid {CONTRACT_VERSION}")
    else:
        for error in result.errors:
            print(f"{args.path}: {error}", file=sys.stderr)
    raise SystemExit(0 if result.valid else 1)


if __name__ == "__main__":
    main()

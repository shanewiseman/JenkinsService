from __future__ import annotations

from jenkins_service.registry import Exposure, OperationRegistry


def test_registry_has_unique_curated_operations() -> None:
    registry = OperationRegistry()
    ids = [operation.id for operation in registry.operations]
    assert len(ids) == len(set(ids))
    assert {
        "validate_pipeline_contract",
        "register_repository",
        "update_repository",
        "unregister_repository",
        "scan_repository",
        "trigger_pipeline",
        "retry_pipeline",
        "cancel_pipeline",
        "run_extension_action",
    }.issubset(ids)
    forbidden = {"script_console", "raw_jenkins", "credentials", "plugin_manager"}
    assert forbidden.isdisjoint(ids)


def test_only_mutations_are_tools() -> None:
    registry = OperationRegistry()
    for operation in registry.operations:
        if operation.exposure is Exposure.TOOL:
            assert operation.http_method in {"POST", "PATCH", "DELETE"}
        else:
            assert operation.http_method == "GET"
            assert operation.mcp_uri is not None

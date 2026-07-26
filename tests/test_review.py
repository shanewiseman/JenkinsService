from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jenkins_service.review import (
    DiffValidationError,
    OpenAIReviewClient,
    OpenAIReviewError,
    ReviewClientConfig,
    parse_unified_diff,
)
from jenkins_service.review_broker import create_review_broker_app

DIFF = """\
diff --git a/src/example.py b/src/example.py
index 1234567..7654321 100644
--- a/src/example.py
+++ b/src/example.py
@@ -1,2 +1,3 @@
 keep
-old
+dangerous = True
+new
"""


def _upstream(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(result),
                        }
                    ],
                }
            ],
        },
    )


def _result(severity: str = "low") -> dict[str, Any]:
    return {
        "summary": "One actionable issue.",
        "findings": [
            {
                "severity": severity,
                "title": "Unsafe behavior",
                "body": "Use the safe implementation.",
                "path": "src/example.py",
                "line": 2,
            }
        ],
    }


def _client(
    handler: httpx.AsyncBaseTransport | httpx.BaseTransport,
    *,
    attempts: int = 3,
) -> OpenAIReviewClient:
    config = ReviewClientConfig(
        max_attempts=attempts,
        retry_base_seconds=0,
        timeout_seconds=0.1,
    )
    return OpenAIReviewClient(
        "test-openai-key",
        config=config,
        client=httpx.AsyncClient(transport=handler),
    )


def test_unified_diff_accepts_only_added_right_side_lines() -> None:
    parsed = parse_unified_diff(DIFF)
    assert parsed.added_lines == {"src/example.py": frozenset({2, 3})}

    with pytest.raises(DiffValidationError, match="not an added line"):
        from jenkins_service.review import ReviewResult

        parsed.validate_result(
            ReviewResult.model_validate(
                {
                    **_result(),
                    "findings": [{**_result()["findings"][0], "line": 1}],
                }
            )
        )


@pytest.mark.asyncio
async def test_openai_success_uses_strict_responses_contract() -> None:
    request_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-openai-key"
        return _upstream(_result())

    client = _client(httpx.MockTransport(handler))
    result = await client.review(DIFF)
    await client.close()

    assert result.findings[0].line == 2
    body = request_bodies[0]
    assert body["model"] == "gpt-5.6-terra"
    assert body["store"] is False
    assert body["tools"] == []
    assert body["reasoning"] == {"effort": "medium"}
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["type"] == "json_schema"


def test_broker_marks_critical_finding_as_blocking(tmp_path: Path) -> None:
    key = tmp_path / "openai_api_key"
    token = tmp_path / "review_broker_token"
    key.write_text("openai-secret", encoding="utf-8")
    token.write_text("internal-secret", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return _upstream(_result("critical"))

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_review_broker_app(
        config=ReviewClientConfig(retry_base_seconds=0),
        http_client=upstream,
        api_key_path=key,
        broker_token_path=token,
    )
    payload = {
        "repository": "owner/repository",
        "pull_request": 17,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "diff": DIFF,
    }
    with TestClient(app) as client:
        unauthorized = client.post("/internal/v1/review", json=payload)
        response = client.post(
            "/internal/v1/review",
            headers={"Authorization": "Bearer internal-secret"},
            json=payload,
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["blocking"] is True
    assert response.json()["review"]["findings"][0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_openai_timeout_is_retried_then_exhausted() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(httpx.MockTransport(handler), attempts=2)
    with pytest.raises(OpenAIReviewError, match="after 2 attempts"):
        await client.review(DIFF)
    await client.close()
    assert attempts == 2


@pytest.mark.asyncio
async def test_malformed_output_is_retried_and_can_recover() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"output_text": "not-json"})
        return _upstream(_result())

    client = _client(httpx.MockTransport(handler))
    result = await client.review(DIFF)
    await client.close()
    assert result.summary == "One actionable issue."
    assert attempts == 2


@pytest.mark.asyncio
async def test_invalid_locations_exhaust_bounded_retries() -> None:
    attempts = 0
    invalid = _result()
    invalid["findings"][0]["line"] = 1

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _upstream(invalid)

    client = _client(httpx.MockTransport(handler), attempts=3)
    with pytest.raises(OpenAIReviewError, match="after 3 attempts"):
        await client.review(DIFF)
    await client.close()
    assert attempts == 3


@pytest.mark.asyncio
async def test_diff_and_output_are_byte_bounded() -> None:
    client = OpenAIReviewClient(
        "test-openai-key",
        config=ReviewClientConfig(max_diff_bytes=10, retry_base_seconds=0),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _upstream(_result()))
        ),
    )
    with pytest.raises(DiffValidationError, match="diff exceeds"):
        await client.review(DIFF)
    await client.close()

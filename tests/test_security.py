from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

from jenkins_service.models import Scope
from jenkins_service.security import (
    Redactor,
    SlidingWindowLimiter,
    TokenAuthenticator,
    hash_token,
    verify_webhook_signature,
)


def test_token_file_hashes_and_scope_implication(tmp_path: Path) -> None:
    secret_file = tmp_path / "tokens"
    secret_file.write_text(f"ops|operate|{hash_token('secret').hex()}\n", encoding="utf-8")
    authenticator = TokenAuthenticator.from_file(secret_file)
    principal = authenticator.authenticate("secret")
    assert principal is not None
    assert principal.permits(Scope.READ)
    assert principal.permits(Scope.OPERATE)
    assert not principal.permits(Scope.ADMIN)
    assert authenticator.authenticate("wrong") is None


def test_invalid_token_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tokens"
    path.write_text("broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        TokenAuthenticator.from_file(path)


def test_webhook_hmac_verification() -> None:
    body = b'{"ok":true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature("secret", body, f"sha256={digest}")
    assert not verify_webhook_signature("secret", body + b"x", f"sha256={digest}")
    assert not verify_webhook_signature("secret", body, None)


def test_sliding_window_rate_limit() -> None:
    limiter = SlidingWindowLimiter(2, 60)
    assert limiter.allow("client", now=0)
    assert limiter.allow("client", now=1)
    assert not limiter.allow("client", now=2)
    assert limiter.allow("client", now=61)


def test_log_redaction() -> None:
    redactor = Redactor([r"(?i)(token=)\S+"])
    assert redactor.redact("token=abc other") == "token=[REDACTED] other"


def test_log_redaction_without_capture_group() -> None:
    redactor = Redactor([r"secret-value"])
    assert redactor.redact("prefix secret-value suffix") == "prefix [REDACTED] suffix"

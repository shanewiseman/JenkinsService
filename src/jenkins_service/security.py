from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request, status

from .models import Principal, Scope


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class TokenAuthenticator:
    """Authenticate token digests loaded from a Docker secret.

    File format: one ``token_id|scope,scope|sha256_hex`` entry per line.
    Plaintext bearer tokens never need to be persisted by the service.
    """

    def __init__(self, entries: list[tuple[str, set[Scope], bytes]]) -> None:
        self._entries = entries

    @classmethod
    def from_file(cls, path: Path) -> TokenAuthenticator:
        entries: list[tuple[str, set[Scope], bytes]] = []
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                token_id, raw_scopes, digest_hex = line.split("|", 2)
                scopes = {Scope(value.strip()) for value in raw_scopes.split(",")}
                digest = bytes.fromhex(digest_hex)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid token secret entry on line {line_number}") from exc
            if len(digest) != hashlib.sha256().digest_size:
                raise ValueError(f"invalid SHA-256 digest on line {line_number}")
            entries.append((token_id, scopes, digest))
        if not entries:
            raise ValueError("API token secret contains no token digests")
        return cls(entries)

    def authenticate(self, token: str) -> Principal | None:
        candidate = hash_token(token)
        match: tuple[str, set[Scope]] | None = None
        # Always compare every entry so match position does not affect timing.
        for token_id, scopes, expected in self._entries:
            if hmac.compare_digest(candidate, expected):
                match = (token_id, scopes)
        if match is None:
            return None
        return Principal(token_id=match[0], scopes=match[1])


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": 'Bearer realm="jenkins-service"'},
        )
    return value


def verify_webhook_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        instant = time.monotonic() if now is None else now
        cutoff = instant - self.window_seconds
        bucket = self._requests[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(instant)
        return True


class Redactor:
    def __init__(self, patterns: list[str]) -> None:
        self.patterns = [re.compile(pattern) for pattern in patterns]

    def redact(self, value: str) -> str:
        for pattern in self.patterns:
            value = pattern.sub(
                lambda match: (
                    (match.group(1) if match.lastindex else "") + "[REDACTED]"
                ),
                value,
            )
        return value

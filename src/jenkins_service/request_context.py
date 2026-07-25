from __future__ import annotations

from contextvars import ContextVar

from .models import Principal

current_principal: ContextVar[Principal] = ContextVar("jenkinsservice_principal")
current_request_id: ContextVar[str] = ContextVar(
    "jenkinsservice_request_id",
    default="internal",
)
